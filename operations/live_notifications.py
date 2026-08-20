"""Slack notifications for the KIS live trading lifecycle.

Why this exists
---------------
An audit of the KIS live path found it Slack-silent: `kis_live_trading`,
`kis_position_manager`, `brokers/kis_broker*` and `live_pilot/armed` sent
nothing at all. `operations/alerts.py` already had UNKNOWN and
reconciliation formatters, and no KIS caller invoked them. A first real
order would have submitted, filled, partially filled, gone UNKNOWN or
been cancelled without a single message.

This module is the one place those events are worded and sent.

Channel
-------
KIS live traffic has its own two webhooks and shares nothing with Alpaca:

    KIS_LIVE_SLACK_WEBHOOK_URL         routine lifecycle
    KIS_LIVE_SLACK_ALERT_WEBHOOK_URL   urgent (see URGENT_EVENTS)

There is no fallback to `SLACK_WEBHOOK_URL` / `SLACK_ALERT_WEBHOOK_URL`.
Those carry Alpaca paper fills and scanner output; a real-money order in
that stream is an order nobody notices, and a fallback would let an
unconfigured deployment place one while looking correctly notified.
Unset webhooks are a readiness blocker instead
(KIS_LIVE_NOTIFICATION_NOT_CONFIGURED), and every message is prefixed
`[KIS LIVE]` -- `[KIS LIVE][CRITICAL]` when urgent -- so the distinction
survives even if the channels are later merged by someone else.

`notification_health.send_with_health_tracking` remains the existing
delivery-failure bookkeeping.

The rule that matters
---------------------
**A notification can never change what the trading system does.** Not by
raising, not by returning False, not by being slow. `notify()` catches
everything -- including bugs in this module's own formatting -- and
returns a bool that callers are expected to ignore. The specific hazard
being closed: a Slack failure must not cause a transport call to be
retried, because the order may already be live at the broker.

That is why `notify()` is called for its side effect only and never
appears in a condition, a retry loop, or an `except` that would alter
control flow. tests/test_live_notifications.py pins that.

Ordering
--------
`LIVE_ORDER_PREPARED` is emitted immediately before the broker call and
every post-transport event after it, so the message sequence is a
truthful record of when the wire was touched. If the process dies between
PREPARED and SUBMITTED, the absence of SUBMITTED is itself the signal
that an order may be in flight -- which is exactly what the durable
UNKNOWN state says too.

Secrets
-------
Every payload value goes through `execution.secret_redaction.redact_value`,
and account numbers through `mask_account_number`. Raw KIS responses,
tokens, app keys and Authorization headers are never passed in; the
redactor is the backstop, not the plan.
"""

import logging

from execution.secret_redaction import mask_account_number, redact_value

logger = logging.getLogger(__name__)

# -- session / candidate -------------------------------------------------
MARKET_START = "MARKET_START"
BUY_CANDIDATE_SELECTED = "BUY_CANDIDATE_SELECTED"

# -- order transport -----------------------------------------------------
LIVE_ORDER_PREPARED = "LIVE_ORDER_PREPARED"
ORDER_SUBMITTED = "ORDER_SUBMITTED"
ORDER_ACCEPTED = "ORDER_ACCEPTED"
ORDER_PENDING = "ORDER_PENDING"
ORDER_REJECTED = "ORDER_REJECTED"
ORDER_UNKNOWN = "ORDER_UNKNOWN"

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

# -- end of day ----------------------------------------------------------
DAILY_SUMMARY = "DAILY_SUMMARY"

EVENTS = frozenset({
    MARKET_START, BUY_CANDIDATE_SELECTED,
    LIVE_ORDER_PREPARED, ORDER_SUBMITTED, ORDER_ACCEPTED, ORDER_PENDING,
    ORDER_REJECTED, ORDER_UNKNOWN,
    PARTIAL_FILL, FILL_COMPLETED,
    EXIT_TRIGGERED, SELL_SUBMITTED, SELL_FILLED,
    CANCEL_REQUESTED, CANCEL_COMPLETED, CANCEL_FAILED,
    RECONCILIATION_MISMATCH, POSITION_MISMATCH, KIS_API_FAILURE, DB_FAILURE,
    HALT_ACTIVATED, KILL_SWITCH_ACTIVATED,
    DAILY_SUMMARY,
})

# Events an operator must not be able to miss in a busy channel.
URGENT_EVENTS = frozenset({
    ORDER_UNKNOWN, ORDER_REJECTED, CANCEL_FAILED, RECONCILIATION_MISMATCH,
    POSITION_MISMATCH, KIS_API_FAILURE, DB_FAILURE, HALT_ACTIVATED,
    KILL_SWITCH_ACTIVATED,
})

# The two lines ORDER_UNKNOWN must always carry. An UNKNOWN order may be
# live at the broker; the one thing that must never be inferred from the
# message is that retrying is acceptable.
UNKNOWN_RETRY_LINE = "RETRY=BLOCKED"
UNKNOWN_RECONCILIATION_LINE = "RECONCILIATION_REQUIRED=true"

_TEST_PREFIX = "[TEST]"

# Every message this module produces is real money on a real account, and
# it shares an operator's screen with Alpaca paper traffic. The prefix is
# how that distinction survives a glance at a phone notification.
KIS_LIVE_PREFIX = "[KIS LIVE]"
KIS_LIVE_CRITICAL_PREFIX = "[KIS LIVE][CRITICAL]"


#: Which lifecycle events are ALSO mirrored into #scanner-monitor, and
#: under which tag. The KIS live channels keep receiving everything they
#: received before -- this is a copy, never a reroute.
#:
#: The map is deliberately partial. MARKET_START, BUY_CANDIDATE_SELECTED,
#: LIVE_ORDER_PREPARED, ORDER_PENDING and the CANCEL_* pair stay off the
#: monitor: it is the channel an operator reads to see what the system
#: did, and a per-symbol running commentary of every intermediate state
#: is what makes such a channel stop being read. An unmapped event
#: mirrors nowhere rather than defaulting into a catch-all tag.
#:
#: A submit is tagged by SIDE, not by event name: ORDER_SUBMITTED carries
#: both entries and exits, and filing a sell under [LIVE BUY] would make
#: the channel lie about the direction of a real order.
_MONITOR_TAGS = {
    ORDER_SUBMITTED: None,   # resolved from the side field
    ORDER_ACCEPTED: None,
    PARTIAL_FILL: "LIVE FILL",
    FILL_COMPLETED: "LIVE FILL",
    EXIT_TRIGGERED: "LIVE SELL",
    SELL_SUBMITTED: "LIVE SELL",
    SELL_FILLED: "LIVE SELL",
    RECONCILIATION_MISMATCH: "RECONCILIATION",
    POSITION_MISMATCH: "RECONCILIATION",
    ORDER_REJECTED: "RISK",
    ORDER_UNKNOWN: "RISK",
    CANCEL_FAILED: "RISK",
    KIS_API_FAILURE: "RISK",
    DB_FAILURE: "RISK",
    HALT_ACTIVATED: "RISK",
    KILL_SWITCH_ACTIVATED: "RISK",
    DAILY_SUMMARY: "DAILY SUMMARY",
}


def monitor_tag_for(event, fields=None):
    """The #scanner-monitor tag for a lifecycle event, or None to skip."""
    if event not in _MONITOR_TAGS:
        return None
    tag = _MONITOR_TAGS[event]
    if tag is not None:
        return tag
    side = str((fields or {}).get("side") or "").strip().lower()
    return "LIVE SELL" if side == "sell" else "LIVE BUY"


def _mirror_to_monitor(event, fields):
    """Copy a lifecycle event into the unified monitor channel.

    Structurally incapable of affecting the order path: it is called after
    the KIS delivery has already happened, its result is discarded, and it
    catches everything. It also stays outside `notification_health` -- the
    monitor is a second channel, and a monitor outage must not count
    against the counter that escalates the kill switch.
    """
    try:
        tag = monitor_tag_for(event, fields)
        if not tag:
            return
        from scanners.notify import monitor

        body = "\n".join([f"Event: {event}"]
                         + [f"{key}: {value}" for key, value in (fields or {}).items()])
        monitor.notify_tagged(tag, body)
    except Exception:  # noqa: BLE001 - a monitor must never reach the order path
        logger.warning("live notification could not be mirrored to the monitor",
                       exc_info=True)


def _format(event, fields, *, test=False):
    """`[KIS LIVE] [EVENT]` headline plus one `- key: value` line per field.

    Field ORDER is the caller's; dicts preserve insertion order, and the
    payload contracts put the operationally important values first.
    """
    prefix = f"{_TEST_PREFIX}" if test else ""
    is_urgent = event in URGENT_EVENTS
    tag = KIS_LIVE_CRITICAL_PREFIX if is_urgent else KIS_LIVE_PREFIX
    urgent = ":rotating_light: " if is_urgent else ""
    lines = [f"{prefix}{tag} {urgent}*[{event}]*"]
    for key, value in (fields or {}).items():
        lines.append(f"- {key}: {value}")
    if event == ORDER_UNKNOWN:
        # Appended here rather than trusted to each caller: a caller that
        # forgot them would produce a message that reads like an ordinary
        # failure an operator might retry.
        lines.append(f"- {UNKNOWN_RETRY_LINE}")
        lines.append(f"- {UNKNOWN_RECONCILIATION_LINE}")
    return "\n".join(lines)


def _sender_for(event):
    """Urgent events go to the KIS live ALERT webhook, routine lifecycle
    events to the KIS live general one.

    Both are KIS-live-only channels (`KIS_LIVE_SLACK_ALERT_WEBHOOK_URL` /
    `KIS_LIVE_SLACK_WEBHOOK_URL`) and neither falls back to the Alpaca
    pair. Two independent reasons:

    * Volume. The Alpaca webhooks carry paper fills and scanner output.
      An UNKNOWN on a real order has to be the loudest thing in its
      channel, not the fortieth message that minute.
    * Honesty. A fallback would let an unconfigured deployment place a
      real order and appear to have notified. Instead the missing
      webhook is a readiness blocker
      (KIS_LIVE_NOTIFICATION_NOT_CONFIGURED) and, if reached anyway,
      `slack_utils` refuses to send rather than picking another channel.

    The urgent/routine split matters for the same reason it always did:
    an alert channel filled with PREPARED/SUBMITTED/ACCEPTED for every
    routine order is an alert channel nobody reads.
    """
    import slack_utils

    if event in URGENT_EVENTS:
        return slack_utils.send_kis_live_alert

    return slack_utils.send_kis_live_message


def notify(event, fields=None, *, test=False, send_fn=None, track_health=True):
    """Send one lifecycle event. Never raises. Return value is delivery
    status only and must not influence trading.

    A caller that branches on this return value is a bug -- an order's
    fate cannot depend on whether Slack answered.
    """
    try:
        if event not in EVENTS:
            logger.error("unknown live notification event %r; not sent", event)
            return False
        safe = redact_value(dict(fields or {}))
        message = _format(event, safe, test=test)
    except Exception:  # noqa: BLE001 -- a formatting bug must not reach trading
        logger.exception("could not format live notification %s", event)
        return False

    sender = send_fn or _sender_for(event)

    # The monitor copy is sent regardless of how the KIS delivery goes.
    # It is a second, independent channel: if the KIS webhook is down, the
    # monitor line is the only record an operator gets, so gating it on
    # the primary send would lose exactly the message that mattered most.
    _mirror_to_monitor(event, safe)

    if not track_health:
        # Deliberately outside notification_health. The one caller that
        # needs this is the kill-switch escalation: notification_health
        # counts consecutive Slack failures and escalates the kill switch
        # when they cross a threshold, so a message ANNOUNCING that
        # escalation, sent through the same tracker, feeds the counter
        # that produced it. A failing Slack would then keep re-escalating
        # itself. Delivery is still best-effort and still never raises.
        try:
            return bool(sender(message))
        except Exception:  # noqa: BLE001
            logger.exception("live notification %s could not be delivered", event)
            return False

    from notification_health import send_with_health_tracking

    try:
        return bool(send_with_health_tracking(sender, message))
    except Exception:  # noqa: BLE001 -- belt and braces; the helper already swallows
        logger.exception("live notification %s could not be delivered", event)
        return False


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


def fill_completed_fields(*, symbol, filled_qty, fill_price, position_qty, average_cost):
    return {
        "symbol": symbol,
        "filled_qty": filled_qty,
        "fill_price": fill_price,
        "position_qty": position_qty,
        "average_cost": average_cost,
    }


def exit_triggered_fields(*, symbol, reason, position_qty, current_price, average_cost):
    return {
        "symbol": symbol,
        "reason": reason,
        "position_qty": position_qty,
        "current_price": current_price,
        "average_cost": average_cost,
    }


def sell_filled_fields(*, symbol, qty, fill_price, realized_pnl, realized_pnl_pct,
                       position_after=None):
    fields = {
        "symbol": symbol,
        "qty": qty,
        "fill_price": fill_price,
        "realized_pnl": realized_pnl,
        "realized_pnl_pct": realized_pnl_pct,
    }
    if position_after is not None:
        fields["position_after"] = position_after
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
                         blocked_candidates, errors, unknown_count):
    return {
        "entries": entries,
        "exits": exits,
        "fills": fills,
        "realized_pnl": realized_pnl,
        "positions": positions,
        "blocked_candidates": blocked_candidates,
        "errors": errors,
        "unknown_count": unknown_count,
    }
