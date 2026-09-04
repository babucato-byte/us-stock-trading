"""An S6 BUY does not rest indefinitely.

The problem this exists for
---------------------------
A limit BUY that is ACCEPTED but never filled holds S6's only position
slot, ties up the cash it reserved, and goes on representing a breakout
the scanner may have stopped emitting an hour ago. The first NORMAL_S6
order rested unfilled for thirty minutes with nothing in the system
willing to decide anything about it: the fill sync reported
STILL_UNCONFIRMED on every tick, forever, because "no fill yet" and "this
will never fill" look identical to it.

So an unfilled entry now has a deadline, and the deadline is short.

    SUBMITTED / ACCEPTED
      |- filled in full          -> OPEN, handed to the exit monitor
      |- filled in part          -> OPEN for the filled quantity,
      |                             remainder cancelled
      `- TTL expired or invalid  -> CANCEL_PENDING -> CANCELLED
                                    -> reconciled -> slot released

Never chase the price
---------------------
Nothing here raises a limit or makes an order marketable. A price that
did not fill is information about the candidate, not a number to adjust
until it works: the strategy picked its entry, and buying above it is a
different trade from the one that was evidenced. When an order times out
the answer is to cancel and re-evaluate from a FRESH candidate, which the
entry path does on its own next pass.

What may cancel before the TTL
------------------------------
A resting order whose reason has gone is worse than a slow one. The
candidate dropping out of the latest scan, going stale, having its
breakout invalidated, or the session ceasing to be orderable are all
grounds to cancel early -- but each must be POSITIVELY established.
"the candidate list is empty because a scan is running" is not
invalidation, it is ignorance, and this module refuses to act on it.

Cancels address the ORDER's session, not the clock's
----------------------------------------------------
An order placed through the daytime family lives on the daytime endpoint
and can only be cancelled there. `entry_session` is what the cancel route
is resolved from, never the session we happen to be in when the timeout
fires -- otherwise an order placed at 22:00 ET and cancelled at 10:00 ET
would be addressed to an endpoint that never saw it.

Fail closed, and never twice
----------------------------
A cancel is sent once. An ambiguous or timed-out cancel response leaves
the order UNKNOWN and stops everything: no second cancel, no replacement
BUY, and no slot release until reconciliation has settled what the broker
actually holds. The same applies to a fill that lands between the
decision and the transport -- the gate re-reads the open-order book, so a
filled order stops its own cancel.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from s6_live import position_store

logger = logging.getLogger(__name__)

#: How long an unfilled BUY may rest before it is cancelled.
#:
#: Three minutes. Short on purpose: S6 is a breakout strategy whose edge
#: decays with the move it is entering, so an entry that has not filled
#: in three minutes is usually entering something that has already
#: happened. This is a TRADING decision, not a technical timeout, which
#: is why it lives here next to the reasoning rather than in an env var.
BUY_FILL_TTL_SECONDS = 180

# -- outcomes ----------------------------------------------------------
ACTION_HELD = "HELD"
ACTION_OPENED = "OPENED"
ACTION_PARTIAL_CANCELLED = "PARTIAL_FILL_REMAINDER_CANCELLED"
ACTION_CANCEL_REQUESTED = "CANCEL_REQUESTED"
ACTION_CANCEL_UNKNOWN = "CANCEL_UNKNOWN"
ACTION_SKIPPED = "SKIPPED"

# -- why an order was cancelled ---------------------------------------
REASON_TTL = "BUY_FILL_TTL_EXPIRED"
REASON_CANDIDATE_GONE = "CANDIDATE_NO_LONGER_PUBLISHED"
REASON_SESSION_NOT_ORDERABLE = "ENTRY_SESSION_NO_LONGER_ORDERABLE"


class EntryTimeoutError(Exception):
    """A cancel could not be established one way or the other."""


def _now(now=None):
    return now or datetime.now(timezone.utc)


def _parse_stamp(stamp) -> Optional[datetime]:
    if not stamp:
        return None
    try:
        made = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001 - an unreadable stamp is not a timeout
        return None
    return made if made.tzinfo else made.replace(tzinfo=timezone.utc)


def accepted_at(conn, internal_order_id) -> Optional[datetime]:
    """When the BROKER accepted this order, from the durable event log.

    The FIRST transition into ACCEPTED, which `execution_engine` writes
    from the transport result -- so it is the moment KIS acknowledged the
    order, not the moment this process began preparing it.
    """
    if conn is None or not internal_order_id:
        return None
    try:
        row = conn.execute(
            "SELECT occurred_at FROM order_state_events "
            "WHERE internal_order_id = ? AND to_state = 'ACCEPTED' "
            "ORDER BY rowid ASC LIMIT 1", (internal_order_id,)).fetchone()
    except Exception:  # noqa: BLE001 - an unreadable event log establishes
        return None  # nothing, and establishing nothing is the point
    return _parse_stamp(row["occurred_at"] if row else None)


def _age_seconds(row, now, conn=None) -> Optional[float]:
    """How long this order has been RESTING AT THE BROKER.

    Anchored on the broker's acceptance, never on the cycle that prepared
    the order. `submitted_at` is when the position row was written, and
    the distance between the two is real work: the gate, the reconciliation
    snapshot, sizing and the transport all happen in between.

    SLGN on 2026-09-03 is what the difference costs. The position row was
    stamped 19:32:08 and KIS accepted at 19:33:27 -- 79 seconds later. At
    19:36:08 this read age=239.9s against a 180s TTL and cancelled an order
    that had actually rested for ~163s. It had already filled. Three shares
    were then lost to BUY_NEVER_FILLED, and 180 seconds had never meant
    180 seconds at the broker.

    Returns None when acceptance cannot be established, and the caller
    treats None as "no TTL expiry" -- the fail-closed direction. Expiring
    an order whose resting time is unknown is precisely the mistake above,
    and the other cancel reasons (candidate gone, session not orderable)
    still apply on their own evidence.
    """
    stamp = accepted_at(conn, row.get("client_order_id"))
    if stamp is None:
        logger.warning(
            "S6 %s %s: broker acceptance time unavailable; the BUY fill TTL "
            "is not applied to it this tick", row.get("position_id"),
            row.get("symbol"))
        return None
    return (now - stamp).total_seconds()


def _open_order_at_broker(broker, symbol):
    """KIS's own record of the resting order, or a sentinel.

    Returns the order dict when open, `False` when the book was read and
    the order is not in it, and `None` when the book could not be read at
    all. None is NOT False: an unreadable book means we do not know
    whether the order is open, and cancelling on "do not know" is how a
    filled order gets cancelled out from under a position that already
    exists.
    """
    try:
        rows = broker.get_open_orders() or ()
    except Exception:  # noqa: BLE001
        logger.warning("S6 entry timeout: open-order read failed; treating "
                       "the order's state as unknown", exc_info=True)
        return None
    wanted = str(symbol or "").upper()
    for row in rows:
        if str(row.get("pdno") or row.get("PDNO") or "").upper() == wanted:
            return row
    return False


def _still_open_at_broker(broker, symbol) -> Optional[bool]:
    found = _open_order_at_broker(broker, symbol)
    return None if found is None else bool(found)


def candidate_still_valid(source, symbol) -> Optional[bool]:
    """Is `symbol` still an S6 candidate? None when it cannot be told.

    A source that refuses -- mid-scan, unresolved store, wrong session --
    yields None, and None never cancels. "The candidate list is empty
    because a scan is running" is ignorance, not invalidation, and the
    two must not produce the same action.
    """
    if source is None:
        return None
    try:
        describe = source.describe() or {}
        if describe.get("refusal"):
            return None
        state = describe.get("scan_state") or {}
        if state.get("running") or not state.get("detectable", True):
            return None
        return str(symbol or "").upper() in {
            str(s).upper() for s in (source.symbols() or ())}
    except Exception:  # noqa: BLE001
        return None



def _ordered_price(row, open_order) -> float:
    """The price the resting order actually carries.

    KIS reports it as `ft_ord_unpr3`. Falling back to the stored entry
    price covers a partially filled row; a positive number is required
    either way, because `OrderIntent` refuses to describe an order whose
    price nobody can state.
    """
    for value in ((open_order or {}).get("ft_ord_unpr3"),
                  (open_order or {}).get("FT_ORD_UNPR3"),
                  row.get("entry_price")):
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if price > 0:
            return price
    raise EntryTimeoutError(
        f"no usable order price for {row.get('symbol')!r}; refusing to "
        "describe a cancel against a price nobody can state")


def _reconstruct_intent(conn, row, open_order=None):
    """The ORIGINAL order's intent, rebuilt from the durable ledger.

    `submit_cancel` transitions the row the idempotency ledger already
    tracks, so the internal_order_id must be the one that was submitted --
    a cancel is not a new order attempt and must not register as one.

    `session` is the ENTRY session, which decides the cancel's route
    family. Resolving it from the current clock instead would address a
    daytime order's cancel to the regular endpoint.
    """
    from domain.order_intent import OrderIntent
    from market_data.exchange_registry import build_kis_instrument

    client_order_id = row.get("client_order_id")
    ledger = conn.execute(
        "SELECT internal_order_id, signal_id, symbol, requested_quantity, "
        "broker_order_id, strategy_id, created_at "
        "FROM kis_order_idempotency WHERE internal_order_id = ?",
        (client_order_id,)).fetchone()
    if ledger is None:
        raise EntryTimeoutError(
            f"no ledger row for {client_order_id!r}; refusing to cancel an "
            "order this process cannot identify")

    symbol = ledger["symbol"]
    instrument, _record = build_kis_instrument(symbol)
    quantity = int(ledger["requested_quantity"] or 0)
    if quantity < 1:
        raise EntryTimeoutError(
            f"ledger quantity for {client_order_id!r} is {quantity!r}")

    created = ledger["created_at"]
    try:
        created_at = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        created_at = datetime.now(timezone.utc)

    intent = OrderIntent(
        internal_order_id=ledger["internal_order_id"],
        signal_id=ledger["signal_id"] or ledger["internal_order_id"],
        strategy_id=ledger["strategy_id"] or position_store.STRATEGY_ID,
        symbol=symbol, exchange=instrument.exchange, side="buy",
        quantity=quantity, order_type="limit",
        # Taken from KIS's own record of the resting order, because an
        # unfilled row has no entry price of its own -- and inventing one
        # would put a number in the audit trail that was never sent.
        # The cancel carries OVRS_ORD_UNPR="0" regardless (KIS's rule),
        # so this is the intent's record of what went out, not what goes
        # on the wire now.
        limit_price=_ordered_price(row, open_order),
        stop_price=None, target_price=None, created_at=created_at,
        session=row.get("entry_session") or None,
    )
    return intent, instrument, ledger["broker_order_id"]


def cancel_unfilled(conn, *, broker, row, reason, account_id, now=None) -> Dict[str, Any]:
    """Cancel one resting BUY through the sanctioned engine path.

    Refuses unless KIS's own book still shows the order open, re-read
    inside the gate so a fill landing between the decision and the
    transport stops it. Sent once: an ambiguous response leaves the order
    UNKNOWN for reconciliation and is never re-sent from here.
    """
    from execution import execution_engine, order_gate
    import shadow_audit

    current = _now(now)
    symbol = row["symbol"]
    position_id = row["position_id"]

    open_order = _open_order_at_broker(broker, symbol)
    if not open_order:
        return {"position_id": position_id, "symbol": symbol,
                "action": ACTION_SKIPPED, "reason": reason,
                "detail": ("open-order book unreadable" if open_order is None
                           else "no longer open at KIS")}

    intent, instrument, broker_order_id = _reconstruct_intent(
        conn, row, open_order=open_order)
    if not broker_order_id:
        return {"position_id": position_id, "symbol": symbol,
                "action": ACTION_SKIPPED, "reason": reason,
                "detail": "no broker order id to cancel"}

    def _cancel_ctx_builder(*_a, **_k):
        # Judged against the book as it is NOW, not the read above.
        still_open = _still_open_at_broker(broker, symbol)
        return order_gate.CancelGateContext(
            execution_broker="kis", broker_order_id=broker_order_id,
            is_actually_open=bool(still_open), kis_account_no=account_id,
            allowed_account_no=account_id, symbol=symbol,
            has_cancel_already_in_flight=False,
        )

    try:
        execution_engine.submit_cancel(
            order_intent=intent, broker_order_id=broker_order_id,
            cancel_gate_context_builder=_cancel_ctx_builder, conn=conn,
            broker=broker, instrument=instrument,
            audit_run_id=shadow_audit.new_run_id(), now=current,
        )
    except Exception as exc:  # noqa: BLE001
        # Never re-sent from here. The engine has already written the
        # durable state; reconciliation decides what the broker holds.
        logger.error("S6 entry cancel for %s ended unresolved (%s) -- not "
                     "retried; reconcile before any further entry",
                     symbol, type(exc).__name__, exc_info=True)
        return {"position_id": position_id, "symbol": symbol,
                "action": ACTION_CANCEL_UNKNOWN, "reason": reason,
                "detail": f"{type(exc).__name__}: {exc}"}

    position_store.abandon_submission(conn, position_id, reason=reason,
                                      now=current)
    logger.info("S6 entry cancelled: %s %s (%s)", position_id, symbol, reason)
    return {"position_id": position_id, "symbol": symbol,
            "action": ACTION_CANCEL_REQUESTED, "reason": reason,
            "broker_order_id": broker_order_id}


def evaluate(conn, *, broker, account_id, source=None, now=None,
             ttl_seconds=BUY_FILL_TTL_SECONDS,
             session_orderable=True) -> List[Dict[str, Any]]:
    """Decide what happens to every still-unfilled S6 BUY.

    Runs AFTER `sync_buy_fills`, so anything that filled -- in whole or
    in part -- has already been applied to the store. What reaches here
    is genuinely unfilled, and the only questions left are whether it has
    run out of time and whether its reason still holds.
    """
    current = _now(now)
    outcomes: List[Dict[str, Any]] = []

    for row in position_store.load_unconfirmed(conn) or ():
        position_id, symbol = row["position_id"], row["symbol"]
        age = _age_seconds(row, current, conn)

        # A partially filled row is already OPEN by the time it gets
        # here; what remains is to pull the unfilled remainder so the
        # slot is not held by a quantity nobody intends to acquire.
        filled = int(row.get("quantity") or 0)
        requested = None
        try:
            ledger = conn.execute(
                "SELECT requested_quantity FROM kis_order_idempotency "
                "WHERE internal_order_id = ?", (row.get("client_order_id"),)
            ).fetchone()
            requested = int(ledger["requested_quantity"] or 0) if ledger else None
        except Exception:  # noqa: BLE001
            requested = None

        reason = None
        if session_orderable is False:
            reason = REASON_SESSION_NOT_ORDERABLE
        elif candidate_still_valid(source, symbol) is False:
            reason = REASON_CANDIDATE_GONE
        elif age is not None and age >= ttl_seconds:
            reason = REASON_TTL

        if reason is None:
            outcomes.append({"position_id": position_id, "symbol": symbol,
                             "action": ACTION_HELD,
                             "age_seconds": age, "ttl_seconds": ttl_seconds})
            continue

        result = cancel_unfilled(conn, broker=broker, row=row, reason=reason,
                                 account_id=account_id, now=current)
        result["age_seconds"] = age
        if filled and requested and filled < requested:
            result["action"] = ACTION_PARTIAL_CANCELLED
            result["filled_quantity"] = filled
        outcomes.append(result)

    return outcomes


def entry_is_blocked(conn, *, broker) -> Optional[str]:
    """Why a NEW S6 BUY may not be placed yet, or None.

    A cancel is not finished when the request returns. Until the order is
    terminal in the ledger, gone from KIS's book, and the position store
    agrees, placing another BUY risks two live orders for one slot -- the
    duplicate this whole lifecycle exists to prevent.
    """
    live = [r for _pid, r in (position_store.load_live(conn) or ())]
    if live:
        return f"S6 already holds a position: {[r['symbol'] for r in live]}"

    unconfirmed = list(position_store.load_unconfirmed(conn) or ())
    if unconfirmed:
        return ("an S6 entry is still unconfirmed: "
                f"{[r['symbol'] for r in unconfirmed]}")

    try:
        open_orders = broker.get_open_orders() or ()
    except Exception as exc:  # noqa: BLE001 - unreadable is not empty
        return f"KIS open orders unreadable ({type(exc).__name__})"
    if open_orders:
        return (f"{len(open_orders)} order(s) still open at KIS")

    return None
