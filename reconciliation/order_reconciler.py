"""Compares internal order/idempotency records against KIS's own open-
order and fill history (spec §16), and is the only module allowed to
resolve an UNKNOWN order via
execution/order_state_machine.reconcile_unknown() (spec §9's required
processing order: ambiguous response -> no resubmission -> query KIS
order history -> cross-check account/open-orders/fills -> confirm state
-> block new buys if confirmation fails).
"""

from dataclasses import dataclass
from typing import List, Optional

from execution.order_state_machine import OrderStateTransitionError, reconcile_unknown


@dataclass(frozen=True)
class ReconciliationOutcome:
    internal_order_id: str
    resolved: bool
    confirmed_status: Optional[str]
    reason: str


def reconcile_unknown_order(internal_order_id: str, broker_order_id: Optional[str], kis_open_orders: List[dict], kis_fills: List[dict]) -> ReconciliationOutcome:
    """`kis_open_orders`/`kis_fills` are the raw dict rows KISBroker.
    get_open_orders()/get_fills() return (KIS's own field names -- this
    function only reads `ODNO`, the order-id field verified from the
    official KIS examples). If `broker_order_id` is None (the order's
    KIS-side id was never even learned -- an ambiguous failure before
    any response body was read), this can never resolve to anything but
    `resolved=False`: there is nothing to look up. Callers must keep
    treating the order as blocking (has_unknown_order=True) until a
    human confirms it via KIS's own order history UI/support channel."""
    if not broker_order_id:
        return ReconciliationOutcome(
            internal_order_id=internal_order_id, resolved=False, confirmed_status=None,
            reason="no broker_order_id was ever recorded -- cannot look this order up at KIS",
        )
    for fill in kis_fills:
        if fill.get("ODNO") == broker_order_id or fill.get("odno") == broker_order_id:
            filled_qty = fill.get("ft_ccld_qty") or fill.get("FT_CCLD_QTY")
            try:
                confirmed = "FILLED" if filled_qty and float(filled_qty) > 0 else "CANCELLED"
            except (TypeError, ValueError):
                confirmed = "FILLED"
            try:
                status = reconcile_unknown(confirmed)
            except OrderStateTransitionError as exc:
                return ReconciliationOutcome(
                    internal_order_id=internal_order_id, resolved=False, confirmed_status=None,
                    reason=f"KIS fill row found but resolved status is invalid: {exc}",
                )
            return ReconciliationOutcome(
                internal_order_id=internal_order_id, resolved=True, confirmed_status=status,
                reason="matched a KIS fill history row",
            )
    for order in kis_open_orders:
        if order.get("ODNO") == broker_order_id or order.get("odno") == broker_order_id:
            status = reconcile_unknown("ACCEPTED")
            return ReconciliationOutcome(
                internal_order_id=internal_order_id, resolved=True, confirmed_status=status,
                reason="matched a KIS open-order row -- order was accepted, still unfilled",
            )
    return ReconciliationOutcome(
        internal_order_id=internal_order_id, resolved=False, confirmed_status=None,
        reason=f"broker_order_id {broker_order_id!r} not found in KIS open orders or fill history",
    )
