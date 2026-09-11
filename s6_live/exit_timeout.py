"""An S6 protective SELL does not rest indefinitely either.

The gap this closes
--------------------
`exit_runtime.recover_dead_exits()` already handles a SELL that KIS
itself makes disappear from the open-order book (expired, cancelled by
the broker, etc.) -- but only once it is gone. A SELL that stays
genuinely OPEN at KIS, unfilled, has no path back at all:
`sync_sell_fills` reports SELL_FILL_REPORTS_ZERO and stops,
`recover_dead_exits` requires the fill inquiry to report the order
TERMINAL (which it never will while KIS still lists it open) and
reports SELL_NOT_TERMINAL, and `reconcile_unconfirmed_exits` requires
the broker to show NO open order in the symbol and reports
BROKER_HAS_OPEN_ORDER. All three correctly do nothing, forever, and
the position sits exposed with its protective exit never actually
resting a chance to work.

Production evidence, 2026-09-10: SCL exit-intent `s6exit-SCL-
d7d50d9650ae` (broker order 0000001958) sat ACCEPTED, zero filled, KIS
still holding 2 shares open, for 6+ hours -- HARD_RISK_CAP/RANGE_REENTRY
both true the whole time, with no code path anywhere that would ever
act on it.

    SUBMITTED / ACCEPTED, still open at KIS
      |- filled                  -> the ordinary fill-sync path owns it
      |- timeout, broker confirms still open, cancel succeeds
      |                          -> intent aborted, position released
      |                             to EXIT_PENDING; the EXISTING,
      |                             UNCHANGED retry_latched_exits()
      |                             submits exactly one replacement on
      |                             its own next tick -- no new
      |                             replacement-submission code here
      `- cancel ambiguous/unconfirmed -> left exactly as entry_timeout
                                          leaves a BUY: UNKNOWN, not
                                          retried, reconciliation owns it

Never a blind timer
--------------------
A timeout alone must never manufacture a second live order. Every
branch below re-reads the broker's own open-order book -- once before
attempting a cancel (a fill landing in between stops it), and once
again after the cancel response, before the position is ever released
for retry. Only a broker-CONFIRMED cancel releases the latch; an
ambiguous response leaves the position exactly where entry_timeout
leaves an ambiguous BUY cancel: unresolved, for reconciliation.

The latch itself
-----------------
`s6_positions.exit_submitted` means "there may be a live SELL that
must not be duplicated", not "never attempt another SELL for this
position again". This module is the one thing that may clear it before
a fill does, and only after the broker has confirmed the old order is
actually gone.

Reusing entry_timeout.py's already-tested primitives
------------------------------------------------------
`accepted_at`, `_open_order_at_broker` and `_still_open_at_broker` are
symbol/side-agnostic -- they read `order_state_events`/the live broker
book by internal_order_id or symbol, nothing about a BUY. Reused as-is
rather than duplicated, so the two lifecycles cannot drift apart on
what "still open" or "when was it accepted" means.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from s6_live import position_store
from s6_live.entry_timeout import (
    accepted_at,
    _open_order_at_broker,
    _still_open_at_broker,
)

logger = logging.getLogger(__name__)

#: How long an unfilled protective SELL may rest before it is reassessed.
#: Age is never, by itself, a cancel instruction.
#:
#: Longer than the BUY TTL (180s) on purpose: a SELL earns no edge from
#: acting fast the way a breakout entry does, and cancelling too eagerly
#: against a resting-but-progressing order (thin liquidity, not a
#: fault) would itself manufacture unnecessary broker traffic. Ten
#: minutes is well past ordinary fill latency and well short of "the
#: position sits exposed all day".
SELL_REASSESSMENT_SECONDS = 600
# Backward-compatible import name for callers and release tooling.  Its
# semantic name is deliberately no longer "timeout".
SELL_FILL_TIMEOUT_SECONDS = SELL_REASSESSMENT_SECONDS

# -- outcomes ------------------------------------------------------------
ACTION_HELD = "HELD"
ACTION_CANCEL_REQUESTED = "CANCEL_REQUESTED"
ACTION_CANCEL_UNKNOWN = "CANCEL_UNKNOWN"
ACTION_SKIPPED = "SKIPPED"
ACTION_RELEASED_FOR_RETRY = "RELEASED_FOR_RETRY"
KEEP_ORDER = "KEEP_ORDER"
REPRICE_ORDER = "REPRICE_ORDER"
PARTIAL_FILL_MANAGE = "PARTIAL_FILL_MANAGE"
TERMINAL_RECONCILE = "TERMINAL_RECONCILE"
LIQUIDITY_DRY = "LIQUIDITY_DRY"
BROKER_UNKNOWN = "BROKER_UNKNOWN"
EXIT_ESCALATION_REQUIRED = "EXIT_ESCALATION_REQUIRED"

REASON_TIMEOUT = "SELL_REASSESSMENT_DUE"

# RIG had three blind cancel/replacement cycles before its fourth order
# filled; SCL established that an OPEN thin-market order can be valid for
# hours.  Four completed reprices is therefore a conservative provisional
# ceiling, not a liquidity or price threshold.  Reaching it stops churn.
MAX_SELL_REPRICES = 4


class ExitTimeoutError(Exception):
    """A cancel could not be established one way or the other."""


def _alert_cancel_unknown(conn, symbol, position_id, detail) -> None:
    """stock-live-alerts (§12): an ambiguous/failed cancel or a cancel
    that landed but did not actually clear KIS's book is exactly the
    "cancel failure"/"UNKNOWN broker state" case an operator must not
    have to find by reading logs. Deduped on position_id so a repeated
    tick over the same unresolved cancel sends this once, not every
    minute -- never a routine-polling spam. Never raises.
    """
    try:
        from operations import live_notifications as ln
        ln.notify(ln.SELL_CANCEL_UNRESOLVED,
                 {"symbol": symbol, "position_id": position_id, "detail": detail},
                 dedupe_conn=conn, dedupe_subject=position_id,
                 dedupe_version=ln.SELL_CANCEL_UNRESOLVED)
    except Exception:  # noqa: BLE001
        logger.error("could not alert on stale-SELL cancel failure",
                     exc_info=True)


def _alert_stuck_timeout(conn, symbol, position_id, age_seconds) -> None:
    """stock-live-alerts (§12): the moment a SELL is FOUND stuck beyond
    the timeout, before this module even attempts a cancel -- an
    operator should see this as soon as it is known, not only if the
    subsequent cancel also goes wrong. Deduped on position_id, same
    reasoning as `_alert_cancel_unknown`."""
    try:
        from operations import live_notifications as ln
        ln.notify(ln.SELL_STUCK_TIMEOUT,
                 {"symbol": symbol, "position_id": position_id,
                  "age_seconds": round(age_seconds, 0)},
                 dedupe_conn=conn, dedupe_subject=position_id,
                 dedupe_version=ln.SELL_STUCK_TIMEOUT)
    except Exception:  # noqa: BLE001
        logger.error("could not alert on stuck SELL timeout", exc_info=True)


def _alert_reassessment_escalation(conn, symbol, position_id, retry_count) -> None:
    """One operator notice once bounded reprice history is exhausted."""
    try:
        from operations import live_notifications as ln
        ln.notify(ln.WATCHDOG_ESCALATED,
                  {"symbol": symbol, "position_id": position_id,
                   "sell_retry_count": retry_count,
                   "reason": "EXIT_ESCALATION_REQUIRED"},
                  dedupe_conn=conn, dedupe_subject=position_id,
                  dedupe_version="EXIT_ESCALATION_REQUIRED")
    except Exception:  # noqa: BLE001
        logger.error("could not alert on SELL reassessment escalation",
                     exc_info=True)


def _now(now=None):
    return now or datetime.now(timezone.utc)


def _active_exit_client_order_id(conn, position_id) -> Optional[str]:
    """The CURRENT exit intent's own order id -- never
    `s6_positions.client_order_id`, which is the position's BUY order
    and never changes once the position opens."""
    from state_store import exit_intent_ledger as eil

    intent = eil.get_active_intent(conn, position_id)
    return intent.get("client_order_id") if intent else None


def sell_age_seconds(conn, position_id, now) -> Optional[float]:
    """How long the CURRENT resting SELL has been accepted at the
    broker. None when no active intent exists or acceptance cannot be
    established -- the fail-closed direction, same as entry_timeout's
    _age_seconds: an order whose resting time is unknown is never
    timed out on a guess."""
    client_order_id = _active_exit_client_order_id(conn, position_id)
    if not client_order_id:
        return None
    stamp = accepted_at(conn, client_order_id)
    if stamp is None:
        return None
    return (now - stamp).total_seconds()


def _sell_price(open_order) -> float:
    """The price the resting SELL actually carries, read from KIS's own
    record of it. Unlike entry_timeout's `_ordered_price`, there is no
    fallback to a stored strategy price here -- this is only ever
    called with a freshly-read, genuinely open order, and a BUY's entry
    price would be the wrong number to fall back to for a SELL."""
    for key in ("ft_ord_unpr3", "FT_ORD_UNPR3"):
        try:
            price = float((open_order or {}).get(key))
        except (TypeError, ValueError):
            continue
        if price > 0:
            return price
    raise ExitTimeoutError(
        "no usable resting-order price from KIS; refusing to describe "
        "a cancel against a price nobody can state")


def _reconstruct_sell_intent(conn, position_id, row, open_order):
    """The ORIGINAL SELL's intent, rebuilt from the durable ledger.

    `session` is resolved from the ORDER's own acceptance timestamp,
    not from the clock the cancel happens to run on -- mirroring
    entry_timeout.py's "cancels address the order's session, not the
    clock's". The exit path does not persist a submission-time session
    field the way a BUY's `entry_session` is (`s6_positions.exit_session`
    is populated only on settlement), so it is recomputed from the same
    deterministic, time-of-day-based function every submission already
    goes through -- `session_capability.route_session`, given the
    order's own KIS acceptance time.
    """
    from domain.order_intent import OrderIntent
    from market_data.exchange_registry import build_kis_instrument
    from config import session_capability

    client_order_id = _active_exit_client_order_id(conn, position_id)
    if not client_order_id:
        raise ExitTimeoutError(
            f"no active exit intent for position {position_id!r}; "
            "refusing to cancel an order this process cannot identify")

    ledger = conn.execute(
        "SELECT internal_order_id, signal_id, symbol, requested_quantity, "
        "broker_order_id, strategy_id, created_at "
        "FROM kis_order_idempotency WHERE internal_order_id = ?",
        (client_order_id,)).fetchone()
    if ledger is None:
        raise ExitTimeoutError(
            f"no ledger row for {client_order_id!r}; refusing to cancel an "
            "order this process cannot identify")

    symbol = ledger["symbol"]
    instrument, _record = build_kis_instrument(symbol)
    quantity = int(ledger["requested_quantity"] or 0)
    if quantity < 1:
        raise ExitTimeoutError(
            f"ledger quantity for {client_order_id!r} is {quantity!r}")

    created = ledger["created_at"]
    try:
        created_at = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        created_at = datetime.now(timezone.utc)

    accepted = accepted_at(conn, client_order_id) or created_at
    submission_session = session_capability.route_session(now=accepted)

    intent = OrderIntent(
        internal_order_id=ledger["internal_order_id"],
        signal_id=ledger["signal_id"] or ledger["internal_order_id"],
        strategy_id=ledger["strategy_id"] or row.get("strategy_id") or position_store.STRATEGY_ID,
        symbol=symbol, exchange=instrument.exchange, side="sell",
        quantity=quantity, order_type="limit",
        limit_price=_sell_price(open_order),
        stop_price=None, target_price=None, created_at=created_at,
        session=submission_session,
    )
    return intent, instrument, ledger["broker_order_id"]


def cancel_stale_sell(conn, *, broker, row, reason, account_id, now=None) -> Dict[str, Any]:
    """Cancel one resting SELL through the sanctioned engine path, then
    -- ONLY once the broker has confirmed the cancel -- release the
    position through the EXISTING, unchanged `_abort_intent` +
    `position_store.release_dead_exit` pair, so `retry_latched_exits`
    (also unchanged) submits exactly one replacement on its own next
    tick. No new replacement-submission code exists in this module.
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

    try:
        intent, instrument, broker_order_id = _reconstruct_sell_intent(
            conn, position_id, row, open_order)
    except ExitTimeoutError as exc:
        return {"position_id": position_id, "symbol": symbol,
                "action": ACTION_SKIPPED, "reason": reason, "detail": str(exc)}
    if not broker_order_id:
        return {"position_id": position_id, "symbol": symbol,
                "action": ACTION_SKIPPED, "reason": reason,
                "detail": "no broker order id to cancel"}

    def _cancel_ctx_builder(*_a, **_k):
        # Judged against the book as it is NOW, not the read above --
        # a fill landing between the decision and the transport stops
        # this cancel, exactly as entry_timeout's does for a BUY.
        still_open = _still_open_at_broker(broker, symbol)
        return order_gate.CancelGateContext(
            execution_broker="kis", broker_order_id=broker_order_id,
            is_actually_open=bool(still_open), kis_account_no=account_id,
            allowed_account_no=account_id, symbol=symbol,
            has_cancel_already_in_flight=False,
        )

    from operations import live_notifications

    try:
        with live_notifications.cancel_context(
                reason=reason, strategy_id=row.get("strategy_id")):
            execution_engine.submit_cancel(
                order_intent=intent, broker_order_id=broker_order_id,
                cancel_gate_context_builder=_cancel_ctx_builder, conn=conn,
                broker=broker, instrument=instrument,
                audit_run_id=shadow_audit.new_run_id(), now=current,
            )
    except Exception as exc:  # noqa: BLE001
        # Never re-sent from here, never released from here. The engine
        # has already written the durable state; reconciliation decides
        # what the broker holds -- same contract as entry_timeout.
        logger.error("S6 exit cancel for %s ended unresolved (%s) -- not "
                     "retried; reconcile before any further exit attempt",
                     symbol, type(exc).__name__, exc_info=True)
        _alert_cancel_unknown(conn, symbol, position_id,
                              f"{type(exc).__name__}: {exc}")
        return {"position_id": position_id, "symbol": symbol,
                "action": ACTION_CANCEL_UNKNOWN, "reason": reason,
                "detail": f"{type(exc).__name__}: {exc}"}

    # Confirm CANCELLED before releasing anything -- re-read the book
    # once more, independent of what submit_cancel itself concluded.
    still_open_after = _still_open_at_broker(broker, symbol)
    if still_open_after is not False:
        logger.error(
            "S6 exit cancel for %s returned but the broker still (or "
            "unreadably) shows it open -- NOT releasing for retry; "
            "reconciliation must settle this before any replacement SELL",
            symbol)
        _alert_cancel_unknown(
            conn, symbol, position_id,
            "cancel accepted but order still appears open at KIS")
        return {"position_id": position_id, "symbol": symbol,
                "action": ACTION_CANCEL_UNKNOWN, "reason": reason,
                "detail": "cancel accepted but order still appears open"}

    from s6_live.exit_runtime import _abort_intent

    _abort_intent(conn, position_id)
    released = position_store.release_dead_exit(
        conn, position_id, reason=row.get("exit_reason"), now=current)
    logger.warning(
        "S6 stale SELL cancelled and confirmed: %s %s broker_order_id=%s "
        "-- released to EXIT_PENDING for retry (%s)",
        position_id, symbol, broker_order_id, reason)
    return {"position_id": position_id, "symbol": symbol,
            "action": ACTION_RELEASED_FOR_RETRY, "reason": reason,
            "broker_order_id": broker_order_id, "released": bool(released)}


def _number(row, *names):
    for name in names:
        try:
            value = float((row or {}).get(name))
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return value
    return None


def _latest_exit_snapshot(conn, position_id):
    """Local Phase-1/2 evidence only; never a new market-data request."""
    try:
        from s6_live import exit_snapshot
        return exit_snapshot.last_snapshot(conn, position_id) or {}
    except Exception:  # noqa: BLE001
        return {}


def _prior_reassessment(conn, position_id):
    try:
        row = conn.execute(
            "SELECT * FROM s6_sell_reassessments WHERE position_id = ? "
            "ORDER BY reassessment_id DESC LIMIT 1", (position_id,)).fetchone()
        return dict(row) if row else {}
    except Exception:  # noqa: BLE001 - an unavailable history must not cancel
        return {}


def _broker_open_order(broker, *, symbol, broker_order_id):
    """Read the authoritative book once and match the actual order id.

    Returning ``None`` means unreadable, ``False`` means read but no current
    order.  The latter is reconciliation evidence, not permission to retry.
    """
    try:
        rows = broker.get_open_orders() or ()
    except Exception:  # noqa: BLE001
        logger.warning("S6 sell reassessment: broker open-order read failed",
                       exc_info=True)
        return None
    wanted_symbol = str(symbol or "").upper()
    wanted_id = str(broker_order_id or "")
    for item in rows:
        item_id = str(item.get("odno") or item.get("ODNO") or "")
        item_symbol = str(item.get("pdno") or item.get("PDNO") or "").upper()
        if wanted_id and item_id == wanted_id:
            return item
        if not wanted_id and item_symbol == wanted_symbol:
            return item
    return False


def _persist_reassessment(conn, record):
    """Append a management fact. Failure is fail-closed: no action changes."""
    columns = (
        "position_id", "symbol", "exit_intent_id", "broker_order_id",
        "broker_status", "submitted_at", "order_age_seconds", "original_qty",
        "filled_qty", "remaining_qty", "order_type", "order_price",
        "last_trade_price", "recent_volume_5m", "recent_volume_10m",
        "recent_volume_15m", "dollar_volume_5m", "nonzero_bar_count",
        "data_age_seconds", "session", "exit_reason", "exit_priority",
        "sell_retry_count", "reassessment_count", "last_reprice_at",
        "previous_order_id", "previous_order_price", "decision",
        "decision_reason", "evaluated_at",
    )
    try:
        conn.execute(
            f"INSERT INTO s6_sell_reassessments ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            tuple(record.get(c) for c in columns))
        conn.commit()
    except Exception:  # noqa: BLE001
        logger.warning("S6 sell reassessment persist failed for %s",
                       record.get("position_id"), exc_info=True)


def _decision_for_open(*, order, snapshot, retry_count):
    """Classify an OPEN order without manufacturing market certainty.

    Phase 1/2 stores last-trade and liquidity observations, but no reliable
    executable bid/ask.  Therefore an open order is kept unless a future
    broker adapter provides explicit, authoritative ``reprice_proven``
    evidence.  This intentionally makes price movement alone insufficient.
    """
    filled = _number(order, "ft_ccld_qty", "FT_CCLD_QTY") or 0
    remaining = _number(order, "nccs_qty", "NCCS_QTY")
    original = _number(order, "ft_ord_qty", "FT_ORD_QTY")
    if remaining is None and original is not None:
        remaining = max(0, original - filled)
    if filled > 0 and (remaining is None or remaining > 0):
        return PARTIAL_FILL_MANAGE, "BROKER_PARTIAL_FILL"
    if str(order.get("cancelable") or order.get("CANCELABLE") or "").upper() in {"N", "FALSE", "0"}:
        return KEEP_ORDER, "OPEN_NOT_CANCELABLE"
    if retry_count >= MAX_SELL_REPRICES:
        return EXIT_ESCALATION_REQUIRED, "REPRICE_LIMIT_REACHED"
    if bool(order.get("reprice_proven")):
        return REPRICE_ORDER, "AUTHORITATIVE_NONCOMPETITIVE_EVIDENCE"
    if str(snapshot.get("liquidity_state") or "").upper() == "CRITICAL":
        return LIQUIDITY_DRY, "THIN_LIQUIDITY_KEEP_VALID_ORDER"
    return KEEP_ORDER, "NO_RELIABLE_REPRICE_EVIDENCE"


def reassess_sell(conn, *, broker, row, account_id, now, age_seconds):
    """Broker-first Phase-3 decision for one due protective SELL.

    The only cancel path remains ``cancel_stale_sell`` and is reached only
    after an explicit REPRICE decision.  A terminal/missing broker order is
    left for existing fill/recovery/reconciliation stages; no latch is
    released from this function on inference alone.
    """
    from state_store import exit_intent_ledger as eil

    position_id, symbol = row["position_id"], row["symbol"]
    intent = eil.get_active_intent(conn, position_id) or {}
    prior = _prior_reassessment(conn, position_id)
    snapshot = _latest_exit_snapshot(conn, position_id)
    broker_order_id = intent.get("broker_order_id")
    open_order = _broker_open_order(broker, symbol=symbol,
                                    broker_order_id=broker_order_id)
    retry_count = int(prior.get("sell_retry_count") or 0)
    reassessment_count = int(prior.get("reassessment_count") or 0) + 1
    if open_order is None:
        decision, reason, broker_status = BROKER_UNKNOWN, "OPEN_ORDER_BOOK_UNREADABLE", "UNKNOWN"
    elif open_order is False:
        decision, reason, broker_status = TERMINAL_RECONCILE, "ORDER_NOT_IN_OPEN_BOOK", "UNKNOWN_TERMINAL"
    else:
        decision, reason = _decision_for_open(order=open_order, snapshot=snapshot,
                                               retry_count=retry_count)
        broker_status = "OPEN_CANCELABLE"

    order_price = _sell_price(open_order) if open_order else None
    original_qty = _number(open_order, "ft_ord_qty", "FT_ORD_QTY") if open_order else None
    filled_qty = _number(open_order, "ft_ccld_qty", "FT_CCLD_QTY") if open_order else None
    remaining_qty = _number(open_order, "nccs_qty", "NCCS_QTY") if open_order else None
    record = {
        "position_id": position_id, "symbol": symbol,
        "exit_intent_id": intent.get("intent_id"), "broker_order_id": broker_order_id,
        "broker_status": broker_status,
        "submitted_at": ((accepted_at(conn, intent.get("client_order_id")) or now).isoformat()),
        "order_age_seconds": age_seconds, "original_qty": original_qty,
        "filled_qty": filled_qty, "remaining_qty": remaining_qty,
        "order_type": "limit" if open_order else None, "order_price": order_price,
        "last_trade_price": snapshot.get("current_price"),
        "recent_volume_5m": snapshot.get("recent_volume_5m"),
        "recent_volume_10m": snapshot.get("recent_volume_10m"),
        "recent_volume_15m": snapshot.get("recent_volume_15m"),
        "dollar_volume_5m": snapshot.get("dollar_volume_5m"),
        "nonzero_bar_count": snapshot.get("nonzero_bar_count"),
        "data_age_seconds": snapshot.get("data_age_seconds"),
        "session": snapshot.get("session"), "exit_reason": row.get("exit_reason"),
        "exit_priority": snapshot.get("current_exit_priority"),
        "sell_retry_count": retry_count, "reassessment_count": reassessment_count,
        "last_reprice_at": prior.get("last_reprice_at"),
        "previous_order_id": prior.get("previous_order_id"),
        "previous_order_price": prior.get("previous_order_price"),
        "decision": decision, "decision_reason": reason, "evaluated_at": now.isoformat(),
    }
    _persist_reassessment(conn, record)
    if decision == EXIT_ESCALATION_REQUIRED:
        _alert_reassessment_escalation(conn, symbol, position_id, retry_count)
    result = {"position_id": position_id, "symbol": symbol, "action": decision,
              "reason": reason, "broker_order_id": broker_order_id,
              "age_seconds": age_seconds, "remaining_qty": remaining_qty,
              "sell_retry_count": retry_count}
    if decision != REPRICE_ORDER:
        return result

    # A reprice uses the old sanctioned cancel/release path.  The prior
    # record remains the durable lineage; a second record marks the confirmed
    # retry only after that path reports an actual release.
    cancelled = cancel_stale_sell(conn, broker=broker, row=row,
                                  reason="SELL_REPRICE_CONFIRMED", account_id=account_id,
                                  now=now)
    if cancelled.get("action") == ACTION_RELEASED_FOR_RETRY:
        record.update({"sell_retry_count": retry_count + 1,
                       "last_reprice_at": now.isoformat(),
                       "previous_order_id": broker_order_id,
                       "previous_order_price": order_price,
                       "decision": REPRICE_ORDER,
                       "decision_reason": "CANCELLED_CONFIRMED_RELEASED_FOR_ONE_RETRY",
                       "evaluated_at": now.isoformat()})
        _persist_reassessment(conn, record)
    result.update(cancelled)
    result["action"] = REPRICE_ORDER
    return result


def evaluate(conn, *, broker, account_id, now=None,
             timeout_seconds=SELL_REASSESSMENT_SECONDS) -> List[Dict[str, Any]]:
    """Reassess, never blindly cancel, an aged still-submitted S6 SELL."""
    current = _now(now)
    outcomes: List[Dict[str, Any]] = []

    for position_id, row in position_store.load_live(conn):
        if row.get("status") != position_store.EXIT_SUBMITTED:
            continue
        if not row.get("exit_submitted"):
            continue
        symbol = row["symbol"]
        age = sell_age_seconds(conn, position_id, current)

        if age is None or age < timeout_seconds:
            outcomes.append({"position_id": position_id, "symbol": symbol,
                             "action": ACTION_HELD, "age_seconds": age,
                             "timeout_seconds": timeout_seconds})
            continue

        outcomes.append(reassess_sell(
            conn, broker=broker, row=row, account_id=account_id,
            now=current, age_seconds=age))
    return outcomes
