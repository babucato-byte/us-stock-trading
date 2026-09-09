"""Slack notifications for the KIS live trading lifecycle.

Why this exists
---------------
An audit of the KIS live path found it Slack-silent: `kis_live_trading`,
`kis_position_manager`, `brokers/kis_broker*` and `live_pilot/armed` sent
nothing at all. This module became the one place lifecycle events are
worded and sent. It still is -- but the WORDING now lives in
`operations.slack_presentation` (Korean, display only) and this module
owns the POLICY: which event reaches a person, on which channel, and
which stays a log line.

The policy (2026-09)
--------------------
A normal BUY produced four or five Slack messages -- PREPARED, SUBMITTED,
ACCEPTED, then nothing about the fill because the S6 fill sync had no
hook -- and a normal SELL the same again. Nine lifecycles on 2026-09-08
produced about fifty messages and not one of them said "filled". So:

    INTERNAL_EVENTS   logged, never presented. Submitted / accepted /
                      prepared / pending / cancel-requested / exit-
                      triggered / partial fill. The durable audit trail
                      (order_state_events, shadow_audit_events) already
                      records every one of them; Slack does not need to.

    LIVE_TRADING      one final message per lifecycle: 매수 체결, 매도 체결,
                      주문 취소, 주문 실패, 주문 차단, plus session readiness.

    LIVE_ALERTS       conditions a person must act on: UNKNOWN, cancel
                      failure, mismatches, kill switch, HALT, watchdog.

    TRADING_REPORT    the daily summary only.

`notify()` is still called for its side effect only. It never raises,
never blocks, and its return value must never steer trading. That rule
is unchanged and tests/test_slack_failure_isolation.py pins it.

Secrets
-------
Every payload value goes through `execution.secret_redaction.redact_value`
and account numbers through `mask_account_number`. Raw KIS responses,
tokens, app keys and Authorization headers are never passed in.
"""

import contextvars
import logging
from datetime import datetime, timezone

from execution.secret_redaction import mask_account_number, redact_value

logger = logging.getLogger(__name__)

# -- session / candidate -------------------------------------------------
MARKET_START = "MARKET_START"
SESSION_BLOCKED = "SESSION_BLOCKED"
BUY_CANDIDATE_SELECTED = "BUY_CANDIDATE_SELECTED"

# -- order transport -----------------------------------------------------
LIVE_ORDER_PREPARED = "LIVE_ORDER_PREPARED"
ORDER_SUBMITTED = "ORDER_SUBMITTED"
ORDER_ACCEPTED = "ORDER_ACCEPTED"
ORDER_PENDING = "ORDER_PENDING"
ORDER_REJECTED = "ORDER_REJECTED"
ORDER_UNKNOWN = "ORDER_UNKNOWN"
ORDER_BLOCKED = "ORDER_BLOCKED"

# -- fills ---------------------------------------------------------------
PARTIAL_FILL = "PARTIAL_FILL"
FILL_COMPLETED = "FILL_COMPLETED"

# -- exit ----------------------------------------------------------------
EXIT_TRIGGERED = "EXIT_TRIGGERED"
SELL_SUBMITTED = "SELL_SUBMITTED"
SELL_FILLED = "SELL_FILLED"

# -- cancel --------------------------------------------------------------
CANCEL_REQUESTED = "CANCEL_REQUESTED"
CANCEL_COMPLETED = "CANCEL_COMPLETED"
CANCEL_FAILED = "CANCEL_FAILED"

# -- safety / faults -----------------------------------------------------
RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"
POSITION_MISMATCH = "POSITION_MISMATCH"
KIS_API_FAILURE = "KIS_API_FAILURE"
DB_FAILURE = "DB_FAILURE"
HALT_ACTIVATED = "HALT_ACTIVATED"
KILL_SWITCH_ACTIVATED = "KILL_SWITCH_ACTIVATED"
WATCHDOG_ESCALATED = "WATCHDOG_ESCALATED"

# -- end of day ----------------------------------------------------------
DAILY_SUMMARY = "DAILY_SUMMARY"

EVENTS = frozenset({
    MARKET_START, SESSION_BLOCKED, BUY_CANDIDATE_SELECTED,
    LIVE_ORDER_PREPARED, ORDER_SUBMITTED, ORDER_ACCEPTED, ORDER_PENDING,
    ORDER_REJECTED, ORDER_UNKNOWN, ORDER_BLOCKED,
    PARTIAL_FILL, FILL_COMPLETED,
    EXIT_TRIGGERED, SELL_SUBMITTED, SELL_FILLED,
    CANCEL_REQUESTED, CANCEL_COMPLETED, CANCEL_FAILED,
    RECONCILIATION_MISMATCH, POSITION_MISMATCH, KIS_API_FAILURE, DB_FAILURE,
    HALT_ACTIVATED, KILL_SWITCH_ACTIVATED, WATCHDOG_ESCALATED,
    DAILY_SUMMARY,
})

#: Events an operator must not be able to miss. They go to the alert
#: channel and carry the 🚨 marker.
URGENT_EVENTS = frozenset({
    ORDER_UNKNOWN, CANCEL_FAILED, RECONCILIATION_MISMATCH,
    POSITION_MISMATCH, KIS_API_FAILURE, DB_FAILURE, HALT_ACTIVATED,
    KILL_SWITCH_ACTIVATED, WATCHDOG_ESCALATED,
})

#: Intermediate states of a lifecycle that already has a final message.
#: Logged at INFO, never sent. The durable audit trail keeps them.
INTERNAL_EVENTS = frozenset({
    BUY_CANDIDATE_SELECTED, LIVE_ORDER_PREPARED, ORDER_SUBMITTED,
    ORDER_ACCEPTED, ORDER_PENDING, SELL_SUBMITTED, EXIT_TRIGGERED,
    CANCEL_REQUESTED, PARTIAL_FILL,
})

#: The final, human-facing lifecycle messages.
LIVE_TRADING_EVENTS = frozenset({
    MARKET_START, SESSION_BLOCKED, FILL_COMPLETED, SELL_FILLED,
    CANCEL_COMPLETED, ORDER_REJECTED, ORDER_BLOCKED,
})

REPORT_EVENTS = frozenset({DAILY_SUMMARY})

# The two lines ORDER_UNKNOWN must always carry. An UNKNOWN order may be
# live at the broker; the one thing that must never be inferred from the
# message is that retrying is acceptable.
UNKNOWN_RETRY_LINE = "RETRY=BLOCKED"
UNKNOWN_RECONCILIATION_LINE = "RECONCILIATION_REQUIRED=true"

_TEST_PREFIX = "[TEST]"

# A validation order is REAL money placed to prove a route works, not a
# simulation. It must not be filed under [TEST] and must not read as
# ordinary strategy traffic either, because nobody's strategy chose it.
_VALIDATION_PREFIX = "[VALIDATION]"

#: Kept for callers and tests that import them. Messages no longer carry
#: the English prefix: the channels are role-specific, and the Korean
#: title says what happened.
KIS_LIVE_PREFIX = "[KIS LIVE]"
KIS_LIVE_CRITICAL_PREFIX = "[KIS LIVE][CRITICAL]"

#: How many times the same symbol may be rejected by the broker in one
#: process before the rejection is ALSO escalated to the alert channel.
REPEATED_REJECTION_THRESHOLD = 2
_rejections_seen = {}

#: Extra facts a caller higher in the stack knows about a cancel that the
#: engine, which emits CANCEL_COMPLETED, does not: the reason and any
#: filled quantity. Set with `cancel_context()`; read only by `notify()`.
_cancel_context = contextvars.ContextVar("live_notifications_cancel_context",
                                         default=None)


class cancel_context:
    """`with cancel_context(reason="BUY_FILL_TTL_EXPIRED", filled_quantity=0):`
    around a cancel so the one cancel message can say why."""

    def __init__(self, **facts):
        self._facts = {k: v for k, v in facts.items() if v is not None}
        self._token = None

    def __enter__(self):
        self._token = _cancel_context.set(dict(self._facts))
        return self

    def __exit__(self, *exc):
        try:
            _cancel_context.reset(self._token)
        except Exception:  # noqa: BLE001 - never let bookkeeping raise
            _cancel_context.set(None)
        return False


def channel_for(event):
    """The channel ROLE an event is presented on, or None for internal."""
    from operations import slack_presentation as sp

    if event in INTERNAL_EVENTS:
        return None
    if event in URGENT_EVENTS:
        return sp.LIVE_ALERTS
    if event in REPORT_EVENTS:
        return sp.TRADING_REPORT
    if event in LIVE_TRADING_EVENTS:
        return sp.LIVE_TRADING
    return None


def is_internal(event) -> bool:
    return event in INTERNAL_EVENTS


def _format(event, fields, *, test=False, validation=False):
    """The complete Korean message for one event."""
    from operations import slack_presentation as sp

    if validation:
        prefix = _VALIDATION_PREFIX + " "
    elif test:
        prefix = _TEST_PREFIX + " "
    else:
        prefix = ""
    fields = dict(fields or {})

    if event == FILL_COMPLETED:
        body = (sp.sell_filled(fields) if str(fields.get("side") or "").lower() == "sell"
                else sp.buy_filled(fields))
    elif event == SELL_FILLED:
        body = sp.sell_filled(fields)
    elif event == CANCEL_COMPLETED:
        body = sp.order_cancelled(fields)
    elif event == ORDER_REJECTED:
        body = sp.order_failed(fields)
    elif event == ORDER_BLOCKED:
        body = sp.order_blocked(fields)
    elif event in (MARKET_START, SESSION_BLOCKED):
        body = sp.session_ready(fields)
    elif event == DAILY_SUMMARY:
        body = fields.get("text") or _generic("일일 거래 요약", fields)
    elif event in URGENT_EVENTS:
        body = sp.critical(event, fields)
        if event == ORDER_UNKNOWN:
            body += f"\n{UNKNOWN_RETRY_LINE}\n{UNKNOWN_RECONCILIATION_LINE}"
    else:
        body = _generic(event, fields)
    return prefix + body


def _generic(title, fields):
    lines = [f"[{title}]"]
    for key, value in (fields or {}).items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def _sender_for(event):
    """The slack_utils sender for an event's channel role.

    Never an Alpaca/paper webhook: those carry paper fills and scanner
    chatter, and a real-money message there is a message nobody sees.
    An unconfigured role webhook makes slack_utils refuse to send rather
    than pick another channel.
    """
    import slack_utils
    from operations import slack_presentation as sp

    role = channel_for(event)
    if role == sp.LIVE_ALERTS:
        return slack_utils.send_kis_live_alert
    if role == sp.TRADING_REPORT:
        return slack_utils.send_trading_report_message
    return slack_utils.send_kis_live_message


def _dedupe_claim(conn, *, event, fields, subject, state_version):
    """One message per (event, symbol, subject, version) via the durable
    notification ledger. Returns (claimed, key). Errs toward sending: a
    ledger that cannot be read must not silence a fill."""
    if conn is None:
        return True, None
    try:
        from operations import notification_ledger as ledger

        symbol = (fields or {}).get("symbol")
        key = ledger.key_for(event, symbol=symbol, subject_id=subject,
                             state_version=state_version)
        claimed = bool(ledger.claim(conn, key, event_type=event, symbol=symbol,
                                    subject_id=subject, state_version=state_version,
                                    channel=channel_for(event)))
        return claimed, key
    except Exception:  # noqa: BLE001
        logger.warning("notification ledger unavailable; sending anyway", exc_info=True)
        return True, None


def _release_claim(conn, key) -> None:
    """A claim is a promise to deliver. If Slack refused, give the claim
    back so the next tick can say it -- otherwise one webhook hiccup
    would swallow a fill message permanently."""
    if conn is None or key is None:
        return
    try:
        from operations import notification_ledger as ledger

        ledger.release(conn, key)
    except Exception:  # noqa: BLE001
        logger.warning("could not release notification claim %s", key, exc_info=True)


def _escalate_repeated_rejection(fields, *, send_fn=None):
    """A second broker rejection of the same symbol in one process is a
    pattern, not an incident; it is also told to the alert channel."""
    from operations import slack_presentation as sp

    symbol = str((fields or {}).get("symbol") or "?")
    seen = _rejections_seen.get(symbol, 0) + 1
    _rejections_seen[symbol] = seen
    if seen < REPEATED_REJECTION_THRESHOLD:
        return
    try:
        import slack_utils

        payload = dict(fields or {})
        payload["repeat_count"] = seen
        message = sp.critical("ORDER_REJECTED_REPEATED", payload)
        (send_fn or slack_utils.send_kis_live_alert)(message)
    except Exception:  # noqa: BLE001
        logger.warning("could not escalate a repeated rejection", exc_info=True)


def notify(event, fields=None, *, test=False, validation=False,
           send_fn=None, track_health=True, dedupe_conn=None,
           dedupe_subject=None, dedupe_version=None):
    """Send one lifecycle event. Never raises. Return value is delivery
    status only and must not influence trading.

    Internal events return False without sending: they are logged so the
    process log still shows the sequence. `dedupe_conn` (a state-store
    connection) claims the message in the notification ledger first, so a
    fill observed by two ticks is announced once.
    """
    try:
        if event not in EVENTS:
            logger.error("unknown live notification event %r; not sent", event)
            return False
        safe = redact_value(dict(fields or {}))
        if event == CANCEL_COMPLETED:
            context = _cancel_context.get()
            if context:
                for key, value in context.items():
                    safe.setdefault(key, value)
        if event in INTERNAL_EVENTS:
            logger.info("live event %s (internal, not presented): %s", event,
                        {k: safe[k] for k in ("symbol", "side", "state", "broker_order_id")
                         if k in safe})
            return False
        if event == ORDER_BLOCKED:
            from operations import slack_presentation as sp

            code = safe.get("reason_code") or sp.block_code_for(safe.get("reason"))
            safe.setdefault("reason_code", code)
            if code in sp.SILENT_BLOCK_CODES:
                logger.info("live event ORDER_BLOCKED %s %s (silent code)",
                            safe.get("symbol"), code)
                return False
            if dedupe_version is None:
                dedupe_version = f"{code}:{datetime.now(timezone.utc).date().isoformat()}"
        claimed, claim_key = _dedupe_claim(dedupe_conn, event=event, fields=safe,
                                           subject=dedupe_subject,
                                           state_version=dedupe_version)
        if not claimed:
            logger.info("live event %s for %s already announced", event, safe.get("symbol"))
            return False
        message = _format(event, safe, test=test, validation=validation)
    except Exception:  # noqa: BLE001 -- a formatting bug must not reach trading
        logger.exception("could not format live notification %s", event)
        return False

    sender = send_fn or _sender_for(event)

    if event == ORDER_REJECTED:
        _escalate_repeated_rejection(safe, send_fn=None if send_fn is None else send_fn)

    delivered = False
    if not track_health:
        # Deliberately outside notification_health. The one caller that
        # needs this is the kill-switch escalation: notification_health
        # counts consecutive Slack failures and escalates the kill switch
        # when they cross a threshold, so a message ANNOUNCING that
        # escalation, sent through the same tracker, feeds the counter
        # that produced it. Delivery is still best-effort and never raises.
        try:
            delivered = bool(sender(message))
        except Exception:  # noqa: BLE001
            logger.exception("live notification %s could not be delivered", event)
    else:
        from notification_health import send_with_health_tracking

        try:
            delivered = bool(send_with_health_tracking(sender, message))
        except Exception:  # noqa: BLE001 -- belt and braces; the helper already swallows
            logger.exception("live notification %s could not be delivered", event)
    if not delivered:
        _release_claim(dedupe_conn, claim_key)
    return delivered


def account_field(account_no):
    """Last four digits only, for the one place an account is worth
    naming at all."""
    return mask_account_number(account_no)


# ---------------------------------------------------------------------
# Payload builders. Each returns an ordered dict for `notify(fields=...)`.
# They exist so the required fields are stated once and a caller cannot
# quietly drop one.
# ---------------------------------------------------------------------

def order_prepared_fields(*, symbol, side, quantity, limit_price, cash_result,
                          positions_used, positions_max, daily_entries_used,
                          daily_entries_max, reconciliation, kill_switch,
                          live_allowlist, mode):
    notional = None
    try:
        notional = round(float(quantity) * float(limit_price), 2)
    except (TypeError, ValueError):
        notional = "unavailable"
    return {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "limit_price": limit_price,
        "estimated_notional": notional,
        "cash_result": cash_result,
        "positions": f"{positions_used}/{positions_max}",
        "daily_entries": f"{daily_entries_used}/{daily_entries_max}",
        "reconciliation": reconciliation,
        "kill_switch": kill_switch,
        "live_allowlist": live_allowlist,
        "mode": mode,
    }


def order_submitted_fields(*, symbol, side, quantity, limit_price,
                           broker_order_id=None, state=None):
    fields = {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "limit_price": limit_price,
    }
    if broker_order_id:
        fields["broker_order_id"] = broker_order_id
    fields["state"] = state
    return fields


def partial_fill_fields(*, symbol, filled_qty, remaining_qty, average_fill_price):
    return {
        "symbol": symbol,
        "filled_qty": filled_qty,
        "remaining_qty": remaining_qty,
        "average_fill_price": average_fill_price,
    }


def fill_completed_fields(*, symbol, filled_qty, fill_price, position_qty, average_cost,
                          strategy_id=None, session=None, broker_order_id=None):
    fields = {
        "symbol": symbol,
        "filled_qty": filled_qty,
        "fill_price": fill_price,
        "position_qty": position_qty,
        "average_cost": average_cost,
    }
    if strategy_id is not None:
        fields["strategy_id"] = strategy_id
    if session is not None:
        fields["session"] = session
    if broker_order_id:
        fields["broker_order_id"] = broker_order_id
    return fields


def exit_triggered_fields(*, symbol, reason, position_qty, current_price, average_cost):
    return {
        "symbol": symbol,
        "reason": reason,
        "position_qty": position_qty,
        "current_price": current_price,
        "average_cost": average_cost,
    }


def sell_filled_fields(*, symbol, qty, fill_price, realized_pnl, realized_pnl_pct,
                       position_after=None, reason=None, strategy_id=None,
                       session=None, average_buy_price=None):
    fields = {
        "symbol": symbol,
        "qty": qty,
        "fill_price": fill_price,
        "realized_pnl": realized_pnl,
        "realized_pnl_pct": realized_pnl_pct,
    }
    if position_after is not None:
        fields["position_after"] = position_after
    if reason is not None:
        fields["reason"] = reason
    if strategy_id is not None:
        fields["strategy_id"] = strategy_id
    if session is not None:
        fields["session"] = session
    if average_buy_price is not None:
        fields["average_buy_price"] = average_buy_price
    return fields


def order_blocked_fields(*, symbol, reason_code, detail=None, side="buy",
                         strategy_id=None, session=None):
    fields = {"symbol": symbol, "side": side, "reason_code": reason_code}
    if detail is not None:
        fields["detail"] = detail
    if strategy_id is not None:
        fields["strategy_id"] = strategy_id
    if session is not None:
        fields["session"] = session
    return fields


def unknown_order_fields(*, symbol, side, quantity=None, limit_price=None,
                         broker_order_id=None, internal_order_id=None,
                         durable_state=None):
    """RETRY=BLOCKED / RECONCILIATION_REQUIRED are appended by _format()."""
    fields = {"symbol": symbol, "side": side}
    if quantity is not None:
        fields["quantity"] = quantity
    if limit_price is not None:
        fields["limit_price"] = limit_price
    fields["broker_order_id"] = broker_order_id or "unknown"
    if internal_order_id:
        fields["idempotency_key"] = internal_order_id
    fields["durable_state"] = durable_state or "UNKNOWN"
    return fields


def daily_summary_fields(*, entries, exits, fills, realized_pnl, positions,
                         blocked_candidates, errors, unknown_count, text=None):
    fields = {
        "entries": entries,
        "exits": exits,
        "fills": fills,
        "realized_pnl": realized_pnl,
        "positions": positions,
        "blocked_candidates": blocked_candidates,
        "errors": errors,
        "unknown_count": unknown_count,
    }
    if text:
        fields["text"] = text
    return fields
