"""TCN-02A: one name for what a reconciliation result means.

The snapshot (`reconciliation/snapshot.py`) carries four booleans and a
timestamp, and `build_snapshot` raises when a read fails. Every caller
that needed to know "what kind of dirty is this" had to re-derive it
from those pieces, and the execution policy could not tell a broker that
did not answer from a broker that answered with a disagreement. Those
call for opposite responses on the SELL side: an unreadable broker is a
reason to wait, a disagreement about another symbol is not a reason to
keep holding this one.

This module is a value object and a classifier. It is NOT a system
state machine: it has no transitions, no persistence, and it decides
nothing about orders. What each state permits is spelled out by the
policy sets at the bottom and enforced by `execution/order_gate.py`.

States, from most to least severe:

    BROKER_READ_FAILURE    a KIS read failed; nothing was observed
    LOCAL_STATE_FAILURE    the internal ledger/book could not be read
    STALE_RECONCILIATION   the snapshot is older than the TTL, or from
                           the future, or absent
    SUBMISSION_UNKNOWN     an order on the account is still UNKNOWN
    POSITION_MISMATCH      internal and KIS positions disagree
    ORDER_MISMATCH         open orders or fills disagree
    CLEAN                  everything agreed, recently

A snapshot can be in several of the dirty states at once; `states`
carries all of them and `primary` the most severe, so a log line has one
word and a policy has the full picture.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import FrozenSet, Optional, Tuple

CLEAN = "CLEAN"
POSITION_MISMATCH = "POSITION_MISMATCH"
ORDER_MISMATCH = "ORDER_MISMATCH"
BROKER_READ_FAILURE = "BROKER_READ_FAILURE"
SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
LOCAL_STATE_FAILURE = "LOCAL_STATE_FAILURE"
STALE_RECONCILIATION = "STALE_RECONCILIATION"

#: Most severe first. `primary` is the first of these that applies.
SEVERITY_ORDER: Tuple[str, ...] = (
    BROKER_READ_FAILURE,
    LOCAL_STATE_FAILURE,
    STALE_RECONCILIATION,
    SUBMISSION_UNKNOWN,
    POSITION_MISMATCH,
    ORDER_MISMATCH,
    CLEAN,
)

ALL_STATES: FrozenSet[str] = frozenset(SEVERITY_ORDER)

#: A NEW BUY is refused in every state but CLEAN. Unchanged from before
#: this module existed; written down so a test can pin it.
BUY_BLOCKING_STATES: FrozenSet[str] = ALL_STATES - {CLEAN}

#: A SELL is refused outright in these states: there is no evidence to
#: weigh, because nothing was observed (or what was observed is too old
#: to be called an observation).
SELL_HARD_BLOCK_STATES: FrozenSet[str] = frozenset({
    BROKER_READ_FAILURE, LOCAL_STATE_FAILURE, STALE_RECONCILIATION,
})

#: A SELL in these states is refused UNLESS the protective-exit evidence
#: in `execution/sell_safe_evidence.py` is complete for the specific
#: position being sold. The disagreement is real and blocks new buys;
#: it does not by itself prove that this position cannot be safely
#: reduced.
SELL_EVIDENCE_STATES: FrozenSet[str] = frozenset({
    POSITION_MISMATCH, ORDER_MISMATCH, SUBMISSION_UNKNOWN,
})


@dataclass(frozen=True)
class ReconciliationClassification:
    primary: str
    states: FrozenSet[str]
    detail: Tuple[str, ...] = ()

    def is_clean(self) -> bool:
        return self.primary == CLEAN

    def blocks_buy(self) -> bool:
        return self.primary in BUY_BLOCKING_STATES

    def hard_blocks_sell(self) -> bool:
        return bool(self.states & SELL_HARD_BLOCK_STATES)

    def sell_needs_evidence(self) -> bool:
        return (not self.hard_blocks_sell()
                and bool(self.states & SELL_EVIDENCE_STATES))


def _classification(states, detail):
    found = frozenset(states) or frozenset({CLEAN})
    primary = next(s for s in SEVERITY_ORDER if s in found)
    return ReconciliationClassification(
        primary=primary, states=found, detail=tuple(detail))


def classify_failure(exc) -> ReconciliationClassification:
    """The state a `build_snapshot` failure represents.

    `ReconciliationLocalStateError` (a subclass of the unavailable error)
    is the internal ledger failing to read; anything else raised by the
    snapshot builder is a broker read that did not answer.
    """
    from reconciliation import snapshot as snap

    if isinstance(exc, snap.ReconciliationLocalStateError):
        return _classification({LOCAL_STATE_FAILURE}, [str(exc)])
    if isinstance(exc, snap.ReconciliationUnavailableError):
        return _classification({BROKER_READ_FAILURE}, [str(exc)])
    # A failure the builder did not classify. Treated as the local side,
    # which is the more conservative of the two for a SELL (both hard
    # block) and identical for a BUY.
    return _classification({LOCAL_STATE_FAILURE},
                           [f"{type(exc).__name__}: {exc}"])


def classify_snapshot(snapshot, *, account_id=None, now=None,
                      max_age=None) -> ReconciliationClassification:
    """Every state a snapshot is in, judged exactly as `verify_snapshot`
    judges it. The two must not disagree: this reads the same fields
    and applies the same TTL.

    A missing or foreign snapshot is LOCAL_STATE_FAILURE rather than
    "clean by absence" -- there is nothing to weigh, and a policy that
    treats nothing as evidence is the defect CODEX-044 removed.
    """
    from reconciliation import snapshot as snap

    if not isinstance(snapshot, snap.ReconciliationSnapshot):
        return _classification({LOCAL_STATE_FAILURE},
                               ["no ReconciliationSnapshot supplied"])
    if account_id is not None and (snapshot.account_id or "") != (account_id or ""):
        return _classification(
            {LOCAL_STATE_FAILURE},
            ["reconciliation snapshot was taken for a different account"])

    states = set()
    detail = []

    checked_at = snapshot.checked_at
    current = now or datetime.now(timezone.utc)
    if checked_at is None:
        states.add(STALE_RECONCILIATION)
        detail.append("snapshot has no checked_at timestamp")
    else:
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        age = (current - checked_at).total_seconds()
        limit = max_age if max_age is not None else snap.max_age_seconds()
        if age < 0:
            states.add(STALE_RECONCILIATION)
            detail.append("snapshot is timestamped in the future")
        elif age > limit:
            states.add(STALE_RECONCILIATION)
            detail.append(f"snapshot is {age:.1f}s old, limit {limit}s")

    if snapshot.has_unknown_orders:
        states.add(SUBMISSION_UNKNOWN)
    if not snapshot.positions_match:
        states.add(POSITION_MISMATCH)
    if not snapshot.open_orders_match or not snapshot.fills_match:
        states.add(ORDER_MISMATCH)
    detail.extend(snapshot.detail or ())
    return _classification(states, detail)
