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


def reconcile_unknown_order(internal_order_id: str, broker_order_id: Optional[str], kis_open_orders: List[dict], kis_fills: List[dict], requested_quantity=None) -> ReconciliationOutcome:
    """`kis_open_orders`/`kis_fills` are the raw dict rows KISBroker.
    get_open_orders()/get_fills() return (KIS's own field names -- this
    function only reads `ODNO`, the order-id field verified from the
    official KIS examples). If `broker_order_id` is None (the order's
    KIS-side id was never even learned -- an ambiguous failure before
    any response body was read), this can never resolve to anything but
    `resolved=False`: there is nothing to look up. Callers must keep
    treating the order as blocking (has_unknown_order=True) until a
    human confirms it via KIS's own order history UI/support channel.

    CODEX-044 (partial-fill misclassification): `ft_ccld_qty` rows are
    per-execution-event, not cumulative, so "a fill row exists with a
    positive quantity" does NOT mean the order is FILLED -- exactly the
    bug CODEX-045 fixed on the normal sell lifecycle and which survived
    here on the UNKNOWN-resolution path. Every matching row is summed
    and compared against `requested_quantity`:

        cumulative == 0                       -> unresolved (nothing confirmed)
        0 < cumulative < requested            -> PARTIALLY_FILLED
        cumulative == requested               -> FILLED
        cumulative  > requested               -> unresolved, data-integrity error
        requested unknown (None)              -> unresolved -- never guess FILLED

    An order resolved to PARTIALLY_FILLED is no longer UNKNOWN (its
    real state IS known), which is why resolving it correctly, rather
    than over-confidently as FILLED, is what keeps the block honest."""
    if not broker_order_id:
        return ReconciliationOutcome(
            internal_order_id=internal_order_id, resolved=False, confirmed_status=None,
            reason="no broker_order_id was ever recorded -- cannot look this order up at KIS",
        )
    cumulative_filled = 0.0
    matched_any_fill = False
    for fill in kis_fills:
        if fill.get("ODNO") != broker_order_id and fill.get("odno") != broker_order_id:
            continue
        matched_any_fill = True
        raw_qty = fill.get("ft_ccld_qty") or fill.get("FT_CCLD_QTY") or 0
        try:
            event_qty = float(raw_qty)
        except (TypeError, ValueError):
            return ReconciliationOutcome(
                internal_order_id=internal_order_id, resolved=False, confirmed_status=None,
                reason=(
                    f"KIS fill row for {broker_order_id!r} has a non-numeric filled quantity "
                    f"{raw_qty!r} -- refusing to guess this order's real state"
                ),
            )
        if event_qty > 0:
            cumulative_filled += event_qty
    if matched_any_fill:
        if cumulative_filled <= 0:
            return ReconciliationOutcome(
                internal_order_id=internal_order_id, resolved=False, confirmed_status=None,
                reason=(
                    f"KIS fill rows exist for {broker_order_id!r} but sum to zero filled quantity "
                    "-- the order's real state is not confirmed"
                ),
            )
        if requested_quantity is None:
            return ReconciliationOutcome(
                internal_order_id=internal_order_id, resolved=False, confirmed_status=None,
                reason=(
                    f"KIS reports {cumulative_filled!r} filled for {broker_order_id!r} but the "
                    "originally requested quantity was never recorded -- cannot tell a partial "
                    "fill from a full fill"
                ),
            )
        try:
            requested = float(requested_quantity)
        except (TypeError, ValueError):
            return ReconciliationOutcome(
                internal_order_id=internal_order_id, resolved=False, confirmed_status=None,
                reason=f"recorded requested quantity {requested_quantity!r} is not numeric",
            )
        if cumulative_filled > requested:
            return ReconciliationOutcome(
                internal_order_id=internal_order_id, resolved=False, confirmed_status=None,
                reason=(
                    f"KIS cumulative filled quantity {cumulative_filled!r} for {broker_order_id!r} "
                    f"exceeds the requested quantity {requested!r} -- data integrity error, "
                    "refusing to resolve"
                ),
            )
        confirmed = "FILLED" if cumulative_filled >= requested else "PARTIALLY_FILLED"
        try:
            status = reconcile_unknown(confirmed)
        except OrderStateTransitionError as exc:
            return ReconciliationOutcome(
                internal_order_id=internal_order_id, resolved=False, confirmed_status=None,
                reason=f"KIS fill row found but resolved status is invalid: {exc}",
            )
        return ReconciliationOutcome(
            internal_order_id=internal_order_id, resolved=True, confirmed_status=status,
            reason=(
                f"matched KIS fill history: cumulative {cumulative_filled!r} of "
                f"{requested!r} requested"
            ),
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
