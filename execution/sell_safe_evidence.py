"""TCN-02A: the evidence a protective EXIT needs when reconciliation is
not clean.

The problem this solves
-----------------------
The sell gate ran the identical reconciliation policy the buy gate runs:
any disagreement anywhere on the account blocked the order. That is the
right rule for a NEW BUY -- an account whose exposure is uncertain must
not take on more. Applied to an EXIT it traps the account in exactly the
position it is trying to leave: a broker holding nobody attributed, or
an S6 row closed by hand, blocked every protective sell on every other
symbol until a human intervened.

The rule this replaces it with is not "sells bypass reconciliation".
It is: a sell may proceed under a dirty reconciliation ONLY when every
piece of evidence below says, about THIS position specifically, that
the broker holds it and nothing else is already selling it. Any missing
piece refuses the order, and the refusal names the piece.

What counts as evidence
-----------------------
    1. the reconciliation itself is usable -- observed, recent, for this
       account; a failed or stale read is not evidence of anything
    2. the local position is fill-backed OPEN (or latched EXIT_PENDING
       from OPEN): a real entry price, a real quantity, no exit in flight
    3. the remaining quantity is a definite positive integer
    4. no sell is already pending: no open order for the symbol at the
       broker, no other active exit intent for the position
    5. no order for this symbol is in UNKNOWN, and no exit intent for
       this position is awaiting reconciliation
    6. the reconciliation snapshot found nothing ambiguous about this
       symbol's ORDERS (an untracked open order, a live order the broker
       has no record of, a fill that contradicts the ledger)
    7. the broker's position in the symbol was read successfully, twice
       and consistently: once by the snapshot, once by the caller
    8. that quantity is positive -- a broker that says 0 is a reason to
       retire the row, never to send a sell
    9. the sell quantity does not exceed min(local remaining, broker
       confirmed)

A position mismatch on the symbol itself (local 2, broker 1) is NOT a
refusal on its own: rule 9 caps the order at what both sides agree is
held. What it refuses is any ambiguity about whether an order is already
working the position, because that is the duplicate-sell risk.

This module is pure. It reads nothing; the caller collects and it
judges, exactly like `execution/order_gate.py`.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from reconciliation import state as reconciliation_state

from domain.position_evidence import (  # noqa: F401 - re-exported
    FILL_BACKED_STATUSES,
    LocalPositionEvidence,
)

PERMITTED = "PROTECTIVE_EXIT_EVIDENCE_OK"
NOT_SUPPLIED = "EVIDENCE_NOT_SUPPLIED"
RECONCILIATION_NOT_USABLE = "EVIDENCE_RECONCILIATION_NOT_USABLE"
LOCAL_NOT_FILL_BACKED = "EVIDENCE_LOCAL_NOT_FILL_BACKED"
SELL_ALREADY_IN_FLIGHT = "EVIDENCE_SELL_ALREADY_IN_FLIGHT"
REMAINING_QTY_UNCLEAR = "EVIDENCE_REMAINING_QTY_UNCLEAR"
SELL_ALREADY_PENDING = "EVIDENCE_SELL_ALREADY_PENDING"
SUBMISSION_UNKNOWN_FOR_SYMBOL = "EVIDENCE_SUBMISSION_UNKNOWN_FOR_SYMBOL"
ORDER_STATE_AMBIGUOUS_FOR_SYMBOL = "EVIDENCE_ORDER_STATE_AMBIGUOUS_FOR_SYMBOL"
BROKER_QTY_UNCONFIRMED = "EVIDENCE_BROKER_QTY_UNCONFIRMED"
BROKER_REPORTS_FLAT = "EVIDENCE_BROKER_REPORTS_FLAT"
QTY_EXCEEDS_CONFIRMED = "EVIDENCE_QTY_EXCEEDS_CONFIRMED"


def _int_or_none(value) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


@dataclass(frozen=True)
class SellSafeEvidence:
    """Everything the gate weighs, collected by the caller."""

    local: Optional[LocalPositionEvidence]
    #: The caller's OWN read of the broker position succeeded.
    broker_position_read_ok: bool
    #: The quantity that read reported for the symbol (0 when absent).
    broker_position_quantity: Optional[int]
    #: Any open order for the symbol at the broker, either side.
    broker_open_order_for_symbol: bool
    #: UNKNOWN orders for this symbol in the idempotency ledger.
    unknown_orders_for_symbol: int
    #: Non-terminal exit intents for this position OTHER than the one
    #: the caller just reserved for this attempt.
    other_active_exit_intents: int
    collected_at: Optional[datetime] = None


@dataclass(frozen=True)
class ProtectiveExitVerdict:
    permitted: bool
    reason_code: str
    detail: str
    #: The most that could be sold on this evidence, when the refusal
    #: was only about quantity. Informational: nothing here resizes an
    #: order.
    max_quantity: Optional[int] = None


def _refuse(code, detail, max_quantity=None):
    return ProtectiveExitVerdict(False, code, detail, max_quantity)


def evaluate_protective_exit(*, snapshot, symbol, quantity, evidence,
                             now=None, account_id=None) -> ProtectiveExitVerdict:
    """Judge one sell against the evidence. Pure; raises nothing."""
    if evidence is None:
        return _refuse(NOT_SUPPLIED,
                       "no protective-exit evidence was supplied; the strict "
                       "reconciliation policy applies")

    name = str(symbol or "").upper()
    classification = reconciliation_state.classify_snapshot(
        snapshot, account_id=account_id, now=now)
    if classification.hard_blocks_sell():
        return _refuse(RECONCILIATION_NOT_USABLE,
                       f"reconciliation is {classification.primary}; nothing "
                       "recent was observed to weigh")
    if getattr(snapshot, "symbol", None) not in (None, name):
        return _refuse(RECONCILIATION_NOT_USABLE,
                       f"reconciliation snapshot is for {snapshot.symbol!r}, "
                       f"not {name!r}")

    local = evidence.local
    if local is None or not local.fill_backed:
        return _refuse(LOCAL_NOT_FILL_BACKED,
                       "the local position is not a fill-backed OPEN row "
                       f"(status={getattr(local, 'status', None)!r}, "
                       f"entry_price={getattr(local, 'entry_price', None)!r}, "
                       f"quantity={getattr(local, 'remaining_quantity', None)!r})")
    if local.exit_submitted:
        return _refuse(SELL_ALREADY_IN_FLIGHT,
                       "the local position already has an exit submitted")

    remaining = local.remaining_quantity
    if remaining is None or remaining < 1:
        return _refuse(REMAINING_QTY_UNCLEAR,
                       f"remaining quantity {remaining!r} is not a positive integer")

    if evidence.broker_open_order_for_symbol:
        return _refuse(SELL_ALREADY_PENDING,
                       f"the broker already has an open order for {name}")
    if (evidence.other_active_exit_intents or 0) > 0:
        return _refuse(SELL_ALREADY_PENDING,
                       f"{evidence.other_active_exit_intents} other exit "
                       "intent(s) are still active for this position")

    if (evidence.unknown_orders_for_symbol or 0) > 0 \
            or name in (getattr(snapshot, "unknown_order_symbols", None) or ()):
        return _refuse(SUBMISSION_UNKNOWN_FOR_SYMBOL,
                       f"an order for {name} is still UNKNOWN; a sell now "
                       "could be the second one")

    if name in (getattr(snapshot, "order_dirty_symbols", None) or ()):
        return _refuse(ORDER_STATE_AMBIGUOUS_FOR_SYMBOL,
                       f"reconciliation found an order-level disagreement "
                       f"about {name} itself")

    confirmed = snapshot.confirmed_broker_quantity(name) \
        if hasattr(snapshot, "confirmed_broker_quantity") else None
    caller_qty = _int_or_none(evidence.broker_position_quantity)
    if not evidence.broker_position_read_ok or confirmed is None or caller_qty is None:
        return _refuse(BROKER_QTY_UNCONFIRMED,
                       "the broker position was not confirmed by a successful "
                       "read (snapshot="
                       f"{confirmed!r}, caller={caller_qty!r})")
    if confirmed != caller_qty:
        return _refuse(BROKER_QTY_UNCONFIRMED,
                       f"two broker reads disagree about {name}: snapshot="
                       f"{confirmed}, caller={caller_qty}; the position is moving")
    if confirmed <= 0:
        return _refuse(BROKER_REPORTS_FLAT,
                       f"the broker explicitly reports no position in {name}; "
                       "this is an external-close candidate, not a sell")

    sellable = min(remaining, confirmed)
    requested = _int_or_none(quantity)
    if requested is None or requested < 1 or requested > sellable:
        return _refuse(QTY_EXCEEDS_CONFIRMED,
                       f"sell quantity {quantity!r} exceeds min(local remaining "
                       f"{remaining}, broker confirmed {confirmed}) = {sellable}",
                       max_quantity=sellable)

    return ProtectiveExitVerdict(
        True, PERMITTED,
        f"{name}: local {remaining} fill-backed, broker confirmed {confirmed}, "
        f"no pending sell, no UNKNOWN order; reconciliation "
        f"{classification.primary} elsewhere on the account",
        max_quantity=sellable)
