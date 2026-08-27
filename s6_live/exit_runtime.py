"""S6 exit decisions turned into real SELLs, through the shared path.

What is shared and what is not
------------------------------
The DECISION is S6's: `s6_live.exit_policy` owns when an S6 position
leaves, and no other strategy's exit ever sees one. The SUBMISSION is
`s1_live.exit_runtime._submit_sell` with S6's store passed in -- the same
function S2 uses, carrying behaviours learned the hard way on S1: an
ambiguous send goes to SUBMISSION_UNKNOWN and is never auto-retried, a
rejection does not chase the price or enlarge the quantity, and the exit
intent ledger refuses a second order for a position that already has one
live.

A third copy of that would be a third idea of what is safe, and three
ideas diverge faster than two.

Fill synchronisation is not exit ownership
------------------------------------------
`sync_buy_fills` and `sync_sell_fills` are here but are deliberately NOT
gated by anything to do with who owns the exit. Conflating the two is
what cost S1 its bookkeeping once: an exit guard excluded a strategy
wholesale and took fill synchronisation with it, so the position stopped
being counted while still being held.

Exits are never gated by entry risk
-----------------------------------
No allocator, no position limit, no reconciliation check. A control that
also blocked liquidation would trap the account in the position it exists
to escape. Entry fail-closed and exit continuity are different rules.
"""

import logging
from typing import Any, Dict, List, Optional

from s1_live.exit_runtime import ExitOutcome, _submit_sell
from s6_live import exit_diagnostics, exit_policy, position_store

logger = logging.getLogger(__name__)

ACTION_HELD = "HELD"
ACTION_SOLD = "SOLD"
ACTION_BLOCKED = "BLOCKED"
ACTION_LATCHED = "LATCHED"

CLIENT_ORDER_PREFIX = "s6exit"


def _finite(value) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


#: Ledger statuses that mean the order will never fill, ever.
#:
#: A rejected order has no broker order id, so the fill lookup has
#: nothing to ask KIS about and answers "no fills yet" forever. The row
#: then sits at SUBMITTED, counts against the symbol lock, and blocks
#: that symbol for the rest of the session -- which is exactly what
#: happened to BTG, PBR and PTEN on 2026-08-27.
#:
#: A rejection is STRONGER evidence than an empty fill lookup, not
#: weaker: the broker positively refused the order. Only REJECTED and
#: CANCELLED qualify. UNKNOWN never does -- that is the state that
#: exists precisely because nobody knows.
_TERMINAL_LEDGER_STATUSES = ("REJECTED", "CANCELLED")


def _order_will_never_fill(conn, row):
    """True when the order ledger says this row's order was refused."""
    client_order_id = row.get("client_order_id")
    if not client_order_id:
        return False
    try:
        found = conn.execute(
            "SELECT status FROM kis_order_idempotency "
            "WHERE internal_order_id = ?", (client_order_id,)).fetchone()
    except Exception:  # noqa: BLE001 - an unreadable ledger is not
        # evidence of anything, and must never abandon a live row.
        logger.warning("S6 could not read the order ledger for %s",
                       client_order_id, exc_info=True)
        return False
    if found is None:
        return False
    status = str(found["status"] if hasattr(found, "keys") else found[0] or "")
    return status.upper() in _TERMINAL_LEDGER_STATUSES


def sync_buy_fills(conn, *, fills_for, now=None) -> List[Dict[str, Any]]:
    """SUBMITTED -> OPEN from the broker's own fills.

    `fills_for(row)` returns {"filled_quantity", "average_fill_price",
    "venue", "order_id"} or None. Cumulative quantity is what is applied,
    so a fill seen twice is a no-op and a stale smaller one is ignored --
    both decided in the store, not here.

    An order the broker reports as never filled is ABANDONED rather than
    left SUBMITTED forever: a row that can never resolve is
    indistinguishable from one still in flight, and the position limit
    counts both.
    """
    # SUBMITTED rows AND already-open ones. A BUY that fills in two
    # parts reaches OPEN on the first fill and then stops being
    # "unconfirmed", so scanning only SUBMITTED would leave the position
    # permanently short of what the account actually holds -- and
    # reconciliation would report the difference as an unattributable
    # broker holding. `apply_fill` compares CUMULATIVE quantity, so
    # re-reading a completed fill is a no-op.
    pending = list(position_store.load_unconfirmed(conn))
    pending += [row for _pid, row in position_store.load_live(conn)
                if not row.get("exit_submitted")]

    applied = []
    for row in pending:
        pid, symbol = row["position_id"], row["symbol"]
        # Asked BEFORE the broker lookup, because for a refused order
        # there is nothing to look up: no broker order id was ever
        # issued, so the lookup returns "no fills yet" every time and the
        # row can never reach a terminal state on its own.
        if (row.get("status") == position_store.SUBMITTED
                and _order_will_never_fill(conn, row)):
            position_store.abandon_submission(
                conn, pid, reason="BUY_NEVER_FILLED", now=now)
            logger.info("S6 abandoned %s (%s): the order ledger reports it "
                        "was refused, so it can never fill", pid, symbol)
            applied.append({"position_id": pid, "symbol": symbol,
                            "status": "ABANDONED"})
            continue
        try:
            fill = fills_for(row)
        except Exception as exc:  # noqa: BLE001 - one symbol's lookup
            # failing must not cost the others their synchronisation.
            logger.error("S6 BUY fill lookup failed for %s", symbol,
                         exc_info=True)
            applied.append({"position_id": pid, "symbol": symbol,
                            "error": str(exc)})
            continue

        if not fill:
            applied.append({"position_id": pid, "symbol": symbol,
                            "status": "STILL_UNCONFIRMED"})
            continue

        quantity = fill.get("filled_quantity")
        if not quantity:
            # Explicitly reported as unfilled -- different from "no
            # answer yet", and the only case safe to abandon. Only a
            # SUBMITTED row can be abandoned: an OPEN position holds
            # shares, and "no fill rows" for it means the lookup found
            # nothing, never that the shares are not there.
            if fill.get("terminal") and row.get("status") == position_store.SUBMITTED:
                position_store.abandon_submission(
                    conn, pid, reason="BUY_NEVER_FILLED", now=now)
                applied.append({"position_id": pid, "symbol": symbol,
                                "status": "ABANDONED"})
            continue

        changed = position_store.apply_fill(
            conn, pid, filled_quantity=quantity,
            average_fill_price=fill.get("average_fill_price"),
            venue=fill.get("venue"), entry_order_id=fill.get("order_id"),
            now=now)
        applied.append({"position_id": pid, "symbol": symbol,
                        "status": "OPENED" if changed else "NO_CHANGE",
                        "filled_quantity": quantity})
    return applied


def _session_name(session):
    """A session object, a plain string, or None -- all acceptable."""
    if session is None:
        return None
    return getattr(session, "name", None) or (
        str(session) if isinstance(session, str) else None)


def _record_broker_fill_time(conn, position_id, broker_timestamp):
    """KIS's own execution time, alongside the tick-stamped close.

    Research bookkeeping only, and never fatal: the position is already
    closed by the time this runs.
    """
    try:
        from post_exit import tracker

        tracker.record_broker_fill_time(
            conn, position_id=position_id, broker_timestamp=broker_timestamp)
    except Exception:  # noqa: BLE001
        logger.debug("broker fill time not recorded for %s", position_id,
                     exc_info=True)


def _settle_intent(conn, position_id, sold, *, done):
    """Close the exit intent out once the fill that answers it is in.

    The position book and the intent ledger are two records of one exit
    and only the position book was being closed. The intent stayed
    non-terminal forever -- which reads downstream as an exit still in
    flight, one of the ambiguities that fails closed, on a position whose
    shares are already gone.

    Never fatal: the fill is applied and the position is closed by the
    time this runs, and losing the ledger's copy of that must not undo
    it. A missing or already-terminal intent is simply nothing to do.
    """
    from state_store import exit_intent_ledger as eil

    try:
        intent = eil.get_active_intent(conn, position_id)
        if not intent:
            return
        if done:
            eil.mark_confirmed(conn, intent["intent_id"],
                               confirmed_filled_qty=sold)
        else:
            eil.update_progress(conn, intent["intent_id"], sold)
    except Exception:  # noqa: BLE001
        logger.warning("S6 could not settle the exit intent for %s; the "
                       "position itself is closed", position_id,
                       exc_info=True)


def sync_sell_fills(conn, *, fills_for, session=None, now=None) -> List[Dict[str, Any]]:
    """EXIT_SUBMITTED -> CLOSED, or a reduced position on a partial.

    A partial SELL leaves the remainder OPEN and still managed. Closing
    on a partial would orphan shares the broker still holds -- the
    position would vanish from the strategy while the account kept the
    risk.
    """
    results = []
    for pid, row in position_store.load_live(conn):
        if not row.get("exit_submitted"):
            continue
        symbol = row["symbol"]
        try:
            fill = fills_for(row)
        except Exception as exc:  # noqa: BLE001
            logger.error("S6 SELL fill lookup failed for %s", symbol,
                         exc_info=True)
            results.append({"position_id": pid, "symbol": symbol,
                            "error": str(exc)})
            continue
        if not fill:
            results.append({"position_id": pid, "symbol": symbol,
                            "status": "AWAITING_SELL_FILL"})
            continue

        sold = int(fill.get("filled_quantity") or 0)
        held = int(row.get("quantity") or 0)
        if sold <= 0:
            continue
        if sold >= held:
            # The broker's own average fill, carried through instead of
            # discarded: it is the only price at which the trade actually
            # ended, and nothing downstream can recover it later.
            # The session is recorded by the tick that saw the fill --
            # a REGULAR entry closed in AFTER_HOURS is a different trade
            # from one closed in REGULAR, and deriving it later from
            # `closed_at` guesses at what this moment already knows.
            position_store.close_position(
                conn, pid, reason=row.get("exit_reason"),
                exit_price=fill.get("average_fill_price"),
                exit_session=_session_name(session), now=now)
            _settle_intent(conn, pid, sold, done=True)
            _record_broker_fill_time(conn, pid, fill.get("broker_timestamp"))
            results.append({"position_id": pid, "symbol": symbol,
                            "status": "CLOSED", "sold": sold,
                            "exit_price": fill.get("average_fill_price")})
        else:
            remaining = held - sold
            conn.execute(
                "UPDATE s6_positions SET quantity = ?, updated_at = ? "
                "WHERE position_id = ?",
                (remaining, position_store._now(now), pid))
            conn.commit()
            _settle_intent(conn, pid, sold, done=False)
            results.append({"position_id": pid, "symbol": symbol,
                            "status": "PARTIALLY_SOLD", "sold": sold,
                            "remaining": remaining})
    return results


def evaluate_position(conn, *, broker_adapter, position_id, row,
                      features=None, current_price=None, session=None,
                      now=None, orders_allowed=True,
                      emergency=False) -> ExitOutcome:
    """Decide, and submit if the decision is SELL.

    The observation is recorded BEFORE the decision, so the peaks the
    decision reads include this tick. Asking first would judge a position
    against a peak it had already exceeded.
    """
    symbol = row["symbol"]
    position_store.observe(
        conn, position_id, price=current_price,
        volume_expansion=_finite(getattr(features, "volume_expansion", None)),
        now=now)
    refreshed = position_store.load(conn, position_id) or row

    state = position_store.to_state(refreshed)
    decision = exit_policy.decide(
        state, current_price=current_price,
        features=features, session=session, now=now, emergency=emergency)

    # Every tick records what each rule answered, including the ones that
    # could not answer at all. A HOLD that was really "three rules had no
    # VWAP to read" must not look like a calm market -- see
    # s6_live/exit_diagnostics.py.
    diagnostics = exit_diagnostics.evaluate(
        state, features=features, price=exit_policy._price_of(
            features, current_price),
        session=session, now=now, decision=decision)
    if diagnostics.get("unavailable_rules"):
        logger.warning(
            "S6 %s: %d exit rule(s) could not be evaluated this tick: %s",
            symbol, len(diagnostics["unavailable_rules"]),
            ", ".join(diagnostics["unavailable_rules"]))

    if not decision.sells:
        # The diagnostics ARE the detail. An empty string here is what
        # made the DT hold unexplainable after the fact.
        return ExitOutcome(position_id, symbol, ACTION_HELD,
                           decision.reason, diagnostics)

    if not orders_allowed:
        # Latched, never dropped. A session that cannot place orders is a
        # reason to wait, not a reason to forget the position should be
        # leaving -- and §7 requires the retry on the next window.
        position_store.latch_pending_exit(conn, position_id, decision.reason,
                                          now=now)
        return ExitOutcome(position_id, symbol, ACTION_LATCHED,
                           decision.reason, "session does not permit orders")

    return _submit_sell(conn, broker_adapter=broker_adapter,
                        position_id=position_id, row=refreshed,
                        reason=decision.reason, now=now,
                        store=position_store, prefix=CLIENT_ORDER_PREFIX)


def run_exits(conn, *, broker_adapter, features_fn, price_fn, session=None,
              now=None, orders_allowed=True, emergency=False
              ) -> List[Dict[str, Any]]:
    """Every held S6 position, evaluated once.

    One position's failure does not cost the others theirs, and a failure
    is reported rather than dropped -- an exit that was never evaluated
    looks exactly like one that decided to hold.
    """
    outcomes = []
    for position_id, row in position_store.load_live(conn):
        symbol = row["symbol"]
        try:
            outcome = evaluate_position(
                conn, broker_adapter=broker_adapter, position_id=position_id,
                row=row, features=features_fn(symbol),
                current_price=price_fn(symbol), session=session, now=now,
                orders_allowed=orders_allowed, emergency=emergency)
            outcomes.append(outcome.as_dict())
        except Exception as exc:  # noqa: BLE001
            logger.error("S6 exit evaluation failed for %s", symbol,
                         exc_info=True)
            outcomes.append(ExitOutcome(position_id, symbol, ACTION_BLOCKED,
                                        None, f"evaluation failed: {exc}"
                                        ).as_dict())
    return outcomes


def retry_latched_exits(conn, *, broker_adapter, session=None, now=None,
                        orders_allowed=True) -> List[Dict[str, Any]]:
    """Re-submit exits latched when orders were not permitted.

    §7's requirement: a SELL that could not be sent is retried in the
    next execution window rather than waiting for the exit condition to
    re-trigger -- the condition already fired, and the position is
    already leaving.
    """
    if not orders_allowed:
        return []
    outcomes = []
    for position_id, row in position_store.load_live(conn):
        if row.get("status") != position_store.EXIT_PENDING:
            continue
        if row.get("exit_submitted"):
            continue
        reason = row.get("pending_exit_reason") or "SESSION_EXIT"
        try:
            outcome = _submit_sell(
                conn, broker_adapter=broker_adapter, position_id=position_id,
                row=row, reason=reason, now=now, store=position_store,
                prefix=CLIENT_ORDER_PREFIX)
            outcomes.append(outcome.as_dict())
        except Exception as exc:  # noqa: BLE001
            logger.error("S6 latched exit retry failed for %s", row["symbol"],
                         exc_info=True)
            outcomes.append({"position_id": position_id,
                             "symbol": row["symbol"],
                             "action": ACTION_BLOCKED, "detail": str(exc)})
    return outcomes
