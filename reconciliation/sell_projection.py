"""Repair a SELL order projection only from completed exit evidence.

The position and exit-intent ledgers record a confirmed full SELL before
the idempotency row is useful to the rest of the execution system.  This
module keeps that projection in step without treating elapsed time or an
absent order as evidence of a fill.
"""

import logging

from execution import order_repository
from execution.order_repository import FatalRepositoryConnectionError
from execution.order_state_machine import OrderStateTransitionError

logger = logging.getLogger(__name__)


def settle_confirmed_sell(conn, *, client_order_id, broker_order_id,
                          confirmed_filled_qty, expected_quantity,
                          event_type, evidence, now=None):
    """Advance one non-terminal SELL projection when a full fill is proven.

    `confirmed_filled_qty` comes either directly from the broker fill
    synchronizer or from a durable CONFIRMED exit intent that has also
    been cross-checked against a zero broker position.  Partial fills and
    missing quantities intentionally do nothing.
    """
    if not client_order_id or expected_quantity is None:
        return None
    try:
        if float(confirmed_filled_qty) < float(expected_quantity):
            return None
    except (TypeError, ValueError):
        return None

    record = order_repository.load(conn, client_order_id)
    if record is None or record.side.lower() != "sell":
        return None
    if broker_order_id and record.broker_order_id and record.broker_order_id != broker_order_id:
        logger.warning("SELL projection %s has a different broker order id; leaving it unchanged",
                       client_order_id)
        return None
    if record.state == "FILLED":
        return None
    try:
        return order_repository.advance(
            conn, record, "FILLED", event_type=event_type,
            event_payload={"evidence": evidence}, now=now,
            via_reconciliation=(record.state == "UNKNOWN"),
        )
    except FatalRepositoryConnectionError:
        raise
    except (order_repository.OrderRepositoryError, OrderStateTransitionError) as exc:
        # The position close is already authoritative.  A projection
        # conflict must be re-read, never solved with another broker call.
        logger.warning("could not settle SELL projection %s: %s", client_order_id, exc)
        return None


def reconcile_closed_sell_projections(conn, *, snapshot, now=None):
    """Repair stale SELL rows only for closed, confirmed, zero-broker-qty exits.

    The snapshot is a fresh KIS read.  Its explicit zero quantity is
    required: a missing or unreadable broker fact never terminalizes a
    projection merely because the local records are old.
    """
    rows = conn.execute(
        "SELECT p.position_id, p.symbol, p.status AS position_status, "
        "i.client_order_id, i.broker_order_id, i.requested_qty, "
        "i.confirmed_filled_qty, i.state AS intent_state "
        "FROM s6_positions p JOIN exit_intents i ON i.position_id = p.position_id "
        "WHERE p.status = 'CLOSED' AND i.state = 'CONFIRMED'"
    ).fetchall()
    repaired = []
    for row in rows:
        if snapshot.confirmed_broker_quantity(row["symbol"]) != 0:
            continue
        record = settle_confirmed_sell(
            conn, client_order_id=row["client_order_id"],
            broker_order_id=row["broker_order_id"],
            confirmed_filled_qty=row["confirmed_filled_qty"],
            expected_quantity=row["requested_qty"],
            event_type="SELL_PROJECTION_RECONCILED",
            evidence="closed_position_confirmed_exit_zero_broker_quantity", now=now,
        )
        if record is not None and record.state == "FILLED":
            repaired.append(record.internal_order_id)
    return repaired
