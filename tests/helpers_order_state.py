"""Shared test helper: drive a KIS order row to a target state THROUGH
the real compare-and-set repository (CODEX-047).

There is deliberately no `update_status()` to shortcut with any more, and
that is the point -- a test that wants an order in UNKNOWN must walk the
same legal transitions production code walks, so the fixture itself
proves the path is reachable.
"""

from execution import idempotency, order_repository

PATHS = {
    "VALIDATING": ["VALIDATING"],
    "APPROVED": ["VALIDATING", "APPROVED"],
    "SUBMITTING": ["VALIDATING", "APPROVED", "SUBMITTING"],
    "ACCEPTED": ["VALIDATING", "APPROVED", "SUBMITTING", "ACCEPTED"],
    "REJECTED": ["VALIDATING", "REJECTED"],
    "UNKNOWN": ["VALIDATING", "APPROVED", "SUBMITTING", "UNKNOWN"],
    "PARTIALLY_FILLED": ["VALIDATING", "APPROVED", "SUBMITTING", "ACCEPTED", "PARTIALLY_FILLED"],
    "FILLED": ["VALIDATING", "APPROVED", "SUBMITTING", "ACCEPTED", "FILLED"],
    "CANCEL_PENDING": ["VALIDATING", "APPROVED", "SUBMITTING", "ACCEPTED", "CANCEL_PENDING"],
}


def drive_to(conn, order_id, target, *, broker_order_id=None):
    record = order_repository.load(conn, order_id)
    steps = PATHS[target]
    for index, state in enumerate(steps):
        record = order_repository.advance(
            conn, record, state, event_type="TEST_DRIVE",
            broker_order_id=broker_order_id if index == len(steps) - 1 else None,
        )
    return record


def register_and_drive(conn, *, internal_order_id, signal_id, symbol, side, trading_date,
                        target, broker_order_id=None, requested_quantity=None):
    idempotency.register(
        conn, internal_order_id=internal_order_id, signal_id=signal_id, symbol=symbol,
        side=side, trading_date=trading_date, requested_quantity=requested_quantity,
    )
    return drive_to(conn, internal_order_id, target, broker_order_id=broker_order_id)
