"""Order state machine -- the 11-state model from
domain/execution_event.py's VALID_STATUSES, plus the explicit transition
graph and the UNKNOWN-never-auto-retries rule (spec §9).

This module contains NO I/O -- it is pure state-transition logic that
execution/execution_engine.py drives. Reconciling an UNKNOWN record back
to a real terminal state is reconciliation/order_reconciler.py's job,
not this module's; this module only enforces which transitions are even
legal and refuses to let a caller skip that step.
"""

from domain.execution_event import VALID_STATUSES

# Every legal (from, to) transition. UNKNOWN can be reached from any
# non-terminal state (an ambiguous broker response can happen at any
# stage) but can only ever be LEFT via RECONCILING (not a state itself --
# see reconciliation/order_reconciler.py) to one of the terminal states
# below, via reconcile_unknown() -- never silently re-approved for a
# fresh SUBMITTING attempt.
_TRANSITIONS = {
    "CREATED": {"VALIDATING", "REJECTED"},
    "VALIDATING": {"APPROVED", "REJECTED"},
    "APPROVED": {"SUBMITTING", "REJECTED"},
    "SUBMITTING": {"ACCEPTED", "REJECTED", "UNKNOWN"},
    "ACCEPTED": {"PARTIALLY_FILLED", "FILLED", "CANCEL_PENDING", "UNKNOWN"},
    "PARTIALLY_FILLED": {"PARTIALLY_FILLED", "FILLED", "CANCEL_PENDING", "UNKNOWN"},
    "CANCEL_PENDING": {"CANCELLED", "PARTIALLY_FILLED", "FILLED", "UNKNOWN"},
    "CANCELLED": set(),
    "FILLED": set(),
    "REJECTED": set(),
    # UNKNOWN is left ONLY by reconcile_unknown() below, which requires
    # the caller to have independently confirmed the real KIS state --
    # never a plain transition() call.
    "UNKNOWN": set(),
}

TERMINAL_STATES = frozenset({"CANCELLED", "FILLED", "REJECTED"})


class OrderStateTransitionError(Exception):
    """Raised on any illegal transition attempt. Callers must treat this
    as a hard stop -- never silently coerce to a different state."""


def is_terminal(status):
    if status not in VALID_STATUSES:
        raise OrderStateTransitionError(f"unknown status {status!r}")
    return status in TERMINAL_STATES


def transition(current_status, new_status):
    """Returns `new_status` if the transition is legal, else raises.
    Pure function -- callers persist the result themselves (this module
    has no I/O)."""
    if current_status not in VALID_STATUSES:
        raise OrderStateTransitionError(f"unknown current_status {current_status!r}")
    if new_status not in VALID_STATUSES:
        raise OrderStateTransitionError(f"unknown new_status {new_status!r}")
    allowed = _TRANSITIONS[current_status]
    if new_status not in allowed:
        raise OrderStateTransitionError(
            f"illegal transition {current_status!r} -> {new_status!r} "
            f"(allowed from {current_status!r}: {sorted(allowed) or 'none (terminal)'})"
        )
    return new_status


def reconcile_unknown(confirmed_status):
    """The ONLY legal way out of UNKNOWN. `confirmed_status` must be a
    terminal or ACCEPTED/PARTIALLY_FILLED status independently confirmed
    against KIS's own order/fill history (reconciliation/order_reconciler.py)
    -- this function does not itself query KIS, it only validates that
    the caller is resolving to a legal post-reconciliation state and is
    not, itself, re-entering SUBMITTING (which would imply a retry
    submission, explicitly forbidden by spec §9/§30 for an UNKNOWN
    order)."""
    if confirmed_status not in VALID_STATUSES:
        raise OrderStateTransitionError(f"unknown confirmed_status {confirmed_status!r}")
    if confirmed_status in ("CREATED", "VALIDATING", "APPROVED", "SUBMITTING", "UNKNOWN"):
        raise OrderStateTransitionError(
            f"reconcile_unknown() cannot resolve to {confirmed_status!r} -- an UNKNOWN order "
            "may only be reconciled to a state KIS itself confirms it actually reached "
            "(ACCEPTED/PARTIALLY_FILLED/FILLED/CANCELLED/REJECTED/CANCEL_PENDING), never "
            "re-submitted"
        )
    return confirmed_status
