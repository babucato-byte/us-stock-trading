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

from execution.order_state_machine import (
    OrderStateTransitionError, reconcile_unknown, transition,
)


@dataclass(frozen=True)
class ReconciliationOutcome:
    internal_order_id: str
    resolved: bool
    confirmed_status: Optional[str]
    reason: str


def _fill_evidence(broker_order_id, kis_fills, requested_quantity):
    """What KIS's fill history says about one order.

    Returns `(matched, status, reason)`. `status` is None whenever the
    evidence does not establish a state, and `reason` then says why --
    the callers below turn that into `resolved=False` rather than
    inventing a status.

    Extracted so the UNKNOWN path and the ACCEPTED settlement path read
    the SAME evidence the same way. They used to be one function serving
    one caller; a second caller copying this arithmetic is how a
    partial fill starts being classified two different ways.
    """
    cumulative = 0.0
    matched = False
    for fill in kis_fills:
        if fill.get("ODNO") != broker_order_id and fill.get("odno") != broker_order_id:
            continue
        matched = True
        raw_qty = fill.get("ft_ccld_qty") or fill.get("FT_CCLD_QTY") or 0
        try:
            event_qty = float(raw_qty)
        except (TypeError, ValueError):
            return True, None, (
                f"KIS fill row for {broker_order_id!r} has a non-numeric filled quantity "
                f"{raw_qty!r} -- refusing to guess this order's real state"
            )
        if event_qty > 0:
            cumulative += event_qty
    if not matched:
        return False, None, ""
    if cumulative <= 0:
        return True, None, (
            f"KIS fill rows exist for {broker_order_id!r} but sum to zero filled quantity "
            "-- the order's real state is not confirmed"
        )
    if requested_quantity is None:
        return True, None, (
            f"KIS reports {cumulative!r} filled for {broker_order_id!r} but the "
            "originally requested quantity was never recorded -- cannot tell a partial "
            "fill from a full fill"
        )
    try:
        requested = float(requested_quantity)
    except (TypeError, ValueError):
        return True, None, f"recorded requested quantity {requested_quantity!r} is not numeric"
    if cumulative > requested:
        return True, None, (
            f"KIS cumulative filled quantity {cumulative!r} for {broker_order_id!r} "
            f"exceeds the requested quantity {requested!r} -- data integrity error, "
            "refusing to resolve"
        )
    status = "FILLED" if cumulative >= requested else "PARTIALLY_FILLED"
    return True, status, (
        f"matched KIS fill history: cumulative {cumulative!r} of {requested!r} requested"
    )


def _listed_open(broker_order_id, kis_open_orders):
    return any(
        order.get("ODNO") == broker_order_id or order.get("odno") == broker_order_id
        for order in kis_open_orders
    )


def settle_live_order(internal_order_id: str, broker_order_id: Optional[str],
                      current_status: str, kis_open_orders: List[dict],
                      kis_fills: List[dict], requested_quantity=None
                      ) -> ReconciliationOutcome:
    """Advance an order this codebase still believes is working at KIS.

    The gap this closes
    -------------------
    `reconcile_unknown_order` only ever looked at UNKNOWN rows, and
    nothing anywhere moved an ACCEPTED row to FILLED. So an accepted
    live order stayed ACCEPTED permanently: it held an entry slot for
    ever (execution/entry_limits.py counts non-terminal rows as in
    flight) and, from the next session onward, made the reconciliation
    snapshot permanently dirty, which blocks every BUY for every
    strategy. The position projection was updated from the fill; the
    idempotency ledger was not, and the ledger is what the gates read.

    Only KIS's own fill history can resolve it, and only in the
    direction the evidence supports:

        still listed open at KIS        -> unresolved, stays as it is
        fills sum to the full quantity  -> FILLED
        fills sum to less               -> PARTIALLY_FILLED
        no fills, not open              -> unresolved (never "cancelled")

    The last line is the important one. An order KIS neither lists as
    open nor reports a fill for is exactly the state a human must look
    at; guessing CANCELLED here would clear the very mismatch that is
    supposed to stop trading.
    """
    if not broker_order_id:
        return ReconciliationOutcome(
            internal_order_id=internal_order_id, resolved=False, confirmed_status=None,
            reason="no broker_order_id was ever recorded -- cannot look this order up at KIS",
        )
    if _listed_open(broker_order_id, kis_open_orders):
        return ReconciliationOutcome(
            internal_order_id=internal_order_id, resolved=False, confirmed_status=None,
            reason=f"KIS still lists {broker_order_id!r} as an open order -- nothing to settle",
        )
    matched, status, reason = _fill_evidence(
        broker_order_id, kis_fills, requested_quantity)
    if not matched:
        return ReconciliationOutcome(
            internal_order_id=internal_order_id, resolved=False, confirmed_status=None,
            reason=(
                f"broker_order_id {broker_order_id!r} appears in neither KIS's open orders "
                "nor its fill history for the window read -- a human must confirm this order"
            ),
        )
    if status is None:
        return ReconciliationOutcome(
            internal_order_id=internal_order_id, resolved=False,
            confirmed_status=None, reason=reason,
        )
    # The ledger's own state machine decides whether this is legal. A row
    # that is already FILLED, or that KIS says filled less than it has
    # already recorded, must raise rather than be written over.
    try:
        transition(current_status, status)
    except OrderStateTransitionError as exc:
        return ReconciliationOutcome(
            internal_order_id=internal_order_id, resolved=False, confirmed_status=None,
            reason=f"KIS evidence says {status} but the ledger cannot move there: {exc}",
        )
    return ReconciliationOutcome(
        internal_order_id=internal_order_id, resolved=True,
        confirmed_status=status, reason=reason,
    )


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
    matched, confirmed, reason = _fill_evidence(
        broker_order_id, kis_fills, requested_quantity)
    if matched:
        if confirmed is None:
            return ReconciliationOutcome(
                internal_order_id=internal_order_id, resolved=False,
                confirmed_status=None, reason=reason,
            )
        try:
            status = reconcile_unknown(confirmed)
        except OrderStateTransitionError as exc:
            return ReconciliationOutcome(
                internal_order_id=internal_order_id, resolved=False, confirmed_status=None,
                reason=f"KIS fill row found but resolved status is invalid: {exc}",
            )
        return ReconciliationOutcome(
            internal_order_id=internal_order_id, resolved=True,
            confirmed_status=status, reason=reason,
        )
    if _listed_open(broker_order_id, kis_open_orders):
        status = reconcile_unknown("ACCEPTED")
        return ReconciliationOutcome(
            internal_order_id=internal_order_id, resolved=True, confirmed_status=status,
            reason="matched a KIS open-order row -- order was accepted, still unfilled",
        )
    return ReconciliationOutcome(
        internal_order_id=internal_order_id, resolved=False, confirmed_status=None,
        reason=f"broker_order_id {broker_order_id!r} not found in KIS open orders or fill history",
    )
