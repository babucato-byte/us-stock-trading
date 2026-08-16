"""Position lifecycle state machine (Stage 4, roadmap Phase 5).

Mirrors the fail-closed, explicit-transition-table conventions already used
in this project: `kill_switch_state.py` (VALID_STATES + a single
FAIL_CLOSED_STATE target for anything that can't be trusted) and
`strategy/status.py` (a module-level constant set plus a helper, no magic
strings sprinkled around call sites).

States (roadmap Phase 5, PROJECT_CONSTITUTION.md's position lifecycle
description):

    SETUP_DETECTED -> ARMED -> ENTRY_RESERVED -> ENTRY_SUBMITTED
        -> PARTIALLY_FILLED -> FILLED -> STOP_ACTIVE -> TARGET_1_ACTIVE
        -> PARTIAL_EXIT_SUBMITTED -> PARTIAL_EXITED -> TRAILING
        -> EXIT_SUBMITTED -> CLOSED

Exception states (reachable from many points in the chain, never guessed
into from a corrupted record -- see positions/store.py):

    REJECTED, CANCELLED, EXPIRED, UNKNOWN, MANUAL_REVIEW, RECOVERY_REQUIRED

`TRANSITIONS` is the single source of truth for which moves are legal.
`validate_transition()` / `transition()` raise `InvalidTransitionError`
rather than silently allowing an arbitrary jump -- a position's state is a
safety-relevant fact (e.g. "is a live stop currently protecting this
position"), so an accidental or malicious skip (e.g. straight from
ENTRY_SUBMITTED to CLOSED, skipping every fill/exit bookkeeping step) must
be a hard error, not a quiet no-op.
"""

SETUP_DETECTED = "SETUP_DETECTED"
ARMED = "ARMED"
ENTRY_RESERVED = "ENTRY_RESERVED"
ENTRY_SUBMITTED = "ENTRY_SUBMITTED"
PARTIALLY_FILLED = "PARTIALLY_FILLED"
FILLED = "FILLED"
STOP_ACTIVE = "STOP_ACTIVE"
TARGET_1_ACTIVE = "TARGET_1_ACTIVE"
PARTIAL_EXIT_SUBMITTED = "PARTIAL_EXIT_SUBMITTED"
PARTIAL_EXITED = "PARTIAL_EXITED"
TRAILING = "TRAILING"
EXIT_SUBMITTED = "EXIT_SUBMITTED"
CLOSED = "CLOSED"

# Exception states.
REJECTED = "REJECTED"
CANCELLED = "CANCELLED"
EXPIRED = "EXPIRED"
UNKNOWN = "UNKNOWN"
MANUAL_REVIEW = "MANUAL_REVIEW"
RECOVERY_REQUIRED = "RECOVERY_REQUIRED"

VALID_STATES = {
    SETUP_DETECTED, ARMED, ENTRY_RESERVED, ENTRY_SUBMITTED, PARTIALLY_FILLED,
    FILLED, STOP_ACTIVE, TARGET_1_ACTIVE, PARTIAL_EXIT_SUBMITTED,
    PARTIAL_EXITED, TRAILING, EXIT_SUBMITTED, CLOSED,
    REJECTED, CANCELLED, EXPIRED, UNKNOWN, MANUAL_REVIEW, RECOVERY_REQUIRED,
}

# Terminal states: no legal outgoing transition. A restart-recovery scan
# (positions/lifecycle.py's recover_on_restart) only ever needs to act on
# positions NOT in this set.
TERMINAL_STATES = {CLOSED, REJECTED, CANCELLED, EXPIRED}
NON_TERMINAL_STATES = VALID_STATES - TERMINAL_STATES

# Fail-closed target for a persisted record that cannot be trusted (missing
# fields, unparseable JSON, unknown state value, etc.) -- mirrors
# kill_switch_state.py's FAIL_CLOSED_STATE = MANUAL_REVIEW convention, but
# one notch more conservative: a corrupted *position* record might be
# hiding a live, unmanaged broker position, so it must never be mistaken
# for a healthy in-progress state (silently defaulting to, say,
# STOP_ACTIVE would imply a stop is actively protecting something we have
# no real information about).
FAIL_CLOSED_STATE = RECOVERY_REQUIRED


class InvalidTransitionError(Exception):
    """Raised when a transition is not present in TRANSITIONS."""


# Adjacency table: state -> set of states it may legally move to next.
#
# Design notes:
#  - PARTIALLY_FILLED -> PARTIALLY_FILLED (self-loop) is legal: successive
#    partial fills on the same entry order are additional observations of
#    the same logical state, not a new state.
#  - STOP_ACTIVE / TARGET_1_ACTIVE both have a direct edge to EXIT_SUBMITTED
#    so a time-stop, EOD forced close, or strategy invalidation can force a
#    full exit before target_1 is ever reached -- exits are not required to
#    pass through the partial-exit stages.
#  - UNKNOWN and UNKNOWN's exits mirror `_STATUS_PROGRESS_RANK`'s treatment
#    in paper_strategy_order.py: it means "broker responded with something
#    we don't recognize", which can still resolve forward into a normal
#    fill state once reconciled, or sideways into MANUAL_REVIEW/
#    RECOVERY_REQUIRED if it can't be resolved.
#  - MANUAL_REVIEW / RECOVERY_REQUIRED are deliberately under-connected:
#    the only way out is an operator-driven resolution (recorded here as a
#    transition to whatever the reconciled truth turned out to be, or to
#    CLOSED once a human confirms the position is actually flat). Nothing
#    silently promotes itself out of these states.
TRANSITIONS = {
    SETUP_DETECTED: {ARMED, REJECTED},
    ARMED: {ENTRY_RESERVED, REJECTED},
    ENTRY_RESERVED: {ENTRY_SUBMITTED, REJECTED, CANCELLED, MANUAL_REVIEW},
    ENTRY_SUBMITTED: {
        PARTIALLY_FILLED, FILLED, REJECTED, CANCELLED, EXPIRED,
        UNKNOWN, MANUAL_REVIEW, RECOVERY_REQUIRED,
    },
    PARTIALLY_FILLED: {
        PARTIALLY_FILLED, FILLED, CANCELLED, UNKNOWN, MANUAL_REVIEW, RECOVERY_REQUIRED,
    },
    FILLED: {STOP_ACTIVE},
    STOP_ACTIVE: {TARGET_1_ACTIVE, EXIT_SUBMITTED, MANUAL_REVIEW, RECOVERY_REQUIRED},
    TARGET_1_ACTIVE: {PARTIAL_EXIT_SUBMITTED, EXIT_SUBMITTED, MANUAL_REVIEW, RECOVERY_REQUIRED},
    PARTIAL_EXIT_SUBMITTED: {PARTIAL_EXITED, MANUAL_REVIEW, RECOVERY_REQUIRED},
    PARTIAL_EXITED: {TRAILING, EXIT_SUBMITTED, MANUAL_REVIEW, RECOVERY_REQUIRED},
    TRAILING: {EXIT_SUBMITTED, MANUAL_REVIEW, RECOVERY_REQUIRED},
    EXIT_SUBMITTED: {CLOSED, UNKNOWN, MANUAL_REVIEW, RECOVERY_REQUIRED},
    UNKNOWN: {MANUAL_REVIEW, RECOVERY_REQUIRED, PARTIALLY_FILLED, FILLED, CLOSED},
    MANUAL_REVIEW: {RECOVERY_REQUIRED, CLOSED},
    RECOVERY_REQUIRED: {
        MANUAL_REVIEW, CLOSED, ENTRY_SUBMITTED, PARTIALLY_FILLED, FILLED, STOP_ACTIVE,
    },
    CLOSED: set(),
    REJECTED: set(),
    CANCELLED: set(),
    EXPIRED: set(),
}

assert set(TRANSITIONS) == VALID_STATES, "TRANSITIONS must cover every valid state"


def is_valid_state(value) -> bool:
    return isinstance(value, str) and value in VALID_STATES


def validate_transition(from_state: str, to_state: str) -> None:
    """Raise InvalidTransitionError unless from_state -> to_state is legal.

    Both states must themselves be valid (an already-corrupted from_state
    is never a valid starting point for a "legal" transition -- callers
    holding a RECOVERY_REQUIRED record must resolve it via the
    RECOVERY_REQUIRED transitions explicitly, never sidestep the check)."""
    if not is_valid_state(from_state):
        raise InvalidTransitionError(f"Unknown from_state: {from_state!r}")
    if not is_valid_state(to_state):
        raise InvalidTransitionError(f"Unknown to_state: {to_state!r}")
    if to_state not in TRANSITIONS[from_state]:
        raise InvalidTransitionError(
            f"Illegal position state transition: {from_state!r} -> {to_state!r}"
        )
