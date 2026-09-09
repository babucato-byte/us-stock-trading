"""Operator alerts for faults in the KIS live path.

`send_alert` used to delegate to `slack_utils.send_slack_alert`, the
Alpaca paper alert webhook. Every caller of this function reports a real
fault on the real account -- a cancel whose final state could not be
persisted, a fatal connection fault, a fail-stop, a rejected token cache
-- and those belong on stock-live-alerts with the other conditions a
person must act on, not in the paper stream.

The message body is left as the caller wrote it: the `- key: value`
lines are the identifiers an operator greps for. A Korean headline is
put in front of it by `slack_presentation.legacy_alert`.

The three `format_*` helpers are kept for their callers and tests.
"""
import slack_utils
from operations import slack_presentation


def send_alert(message: str) -> bool:
    """A fault alert on the live-alerts channel. Never raises."""
    try:
        text = slack_presentation.legacy_alert(message)
    except Exception:  # noqa: BLE001 - a headline must never cost the alert
        text = message
    return slack_utils.send_kis_live_alert(text)


def format_order_blocked_message(*, symbol: str, side: str, reason: str) -> str:
    return f"*KIS order blocked*\n- Symbol: {symbol}\n- Side: {side}\n- Reason: {reason}"


def format_reconciliation_mismatch_message(*, mismatch_count: int, details: str) -> str:
    return f"*KIS reconciliation mismatch*\n- Count: {mismatch_count}\n- Details: {details}"


def format_unknown_order_message(*, internal_order_id: str, symbol: str) -> str:
    return (
        f"*KIS order status UNKNOWN*\n- internal_order_id: {internal_order_id}\n"
        f"- Symbol: {symbol}\n- Action: no automatic retry; awaiting reconciliation"
    )
