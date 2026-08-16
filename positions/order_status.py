"""CODEX-023: classify a broker order-status string into a fill state.

Order *acceptance* and order *fill* are different facts. A broker
returning HTTP 200/201 for a submit_order() call only means the order was
accepted for routing -- it says nothing about whether any shares actually
traded. Treating acceptance as fill is exactly the bug this module exists
to make structurally impossible: every caller that needs to know "did
this order actually fill" must go through classify_broker_order_status(),
which only ever answers FILLED/PARTIALLY_FILLED for the two Alpaca order
statuses that mean shares actually traded, and NOT_FILLED for every
interim status. Anything not on either list is UNKNOWN -- fail-closed,
never guessed to be one or the other.
"""

NOT_FILLED_STATUSES = {
    "accepted", "new", "pending_new", "pending_replace",
    "pending_cancel", "calculated", "held", "suspended",
}
PARTIALLY_FILLED_STATUSES = {"partially_filled"}
FILLED_STATUSES = {"filled"}

FILL_STATE_NOT_FILLED = "NOT_FILLED"
FILL_STATE_PARTIALLY_FILLED = "PARTIALLY_FILLED"
FILL_STATE_FILLED = "FILLED"
FILL_STATE_UNKNOWN = "UNKNOWN"


def classify_broker_order_status(status):
    """Returns one of FILL_STATE_NOT_FILLED / FILL_STATE_PARTIALLY_FILLED /
    FILL_STATE_FILLED / FILL_STATE_UNKNOWN. Never raises -- an
    unrecognized or malformed status is UNKNOWN, not an exception, so
    callers can uniformly route UNKNOWN to a fail-closed
    MANUAL_REVIEW/RECONCILIATION_REQUIRED outcome rather than crashing."""
    if not isinstance(status, str):
        return FILL_STATE_UNKNOWN
    normalized = status.strip().lower()
    if normalized in FILLED_STATUSES:
        return FILL_STATE_FILLED
    if normalized in PARTIALLY_FILLED_STATUSES:
        return FILL_STATE_PARTIALLY_FILLED
    if normalized in NOT_FILLED_STATUSES:
        return FILL_STATE_NOT_FILLED
    return FILL_STATE_UNKNOWN


def _to_finite_float(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):  # NaN/Infinity
        return None
    return result


def extract_order_info(response_or_data):
    """Pull {status, filled_qty, filled_avg_price, order_id} out of a
    BrokerResponse (or a plain dict, for tests that construct broker
    payloads directly) without ever raising -- a missing/non-dict/
    malformed payload yields all-None fields, which
    classify_broker_order_status(None) correctly reports as UNKNOWN."""
    data = getattr(response_or_data, "data", response_or_data)
    if not isinstance(data, dict):
        return {"status": None, "filled_qty": None, "filled_avg_price": None, "order_id": None}
    return {
        "status": data.get("status"),
        "filled_qty": _to_finite_float(data.get("filled_qty")),
        "filled_avg_price": _to_finite_float(data.get("filled_avg_price")),
        "order_id": data.get("id"),
    }
