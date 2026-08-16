"""Thin facade over the existing slack_utils.py (spec §5: reuse existing
alert channel). The only addition is a KIS-specific message-formatting
helper so callers never hand-format a KIS order/reconciliation alert
inline -- keeps the actual wording centralized and testable.
"""

import slack_utils


def send_alert(message: str) -> bool:
    """Delegates entirely to the existing slack_utils.send_slack_alert()."""
    return slack_utils.send_slack_alert(message)


def format_order_blocked_message(*, symbol: str, side: str, reason: str) -> str:
    return f"*KIS order blocked*\n- Symbol: {symbol}\n- Side: {side}\n- Reason: {reason}"


def format_reconciliation_mismatch_message(*, mismatch_count: int, details: str) -> str:
    return f"*KIS reconciliation mismatch*\n- Count: {mismatch_count}\n- Details: {details}"


def format_unknown_order_message(*, internal_order_id: str, symbol: str) -> str:
    return (
        f"*KIS order status UNKNOWN*\n- internal_order_id: {internal_order_id}\n"
        f"- Symbol: {symbol}\n- Action: no automatic retry; awaiting reconciliation"
    )
