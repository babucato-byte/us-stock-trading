"""Stage 4 (roadmap Phase 5): position lifecycle state machine tests.

No network, no real files -- positions/states.py is pure logic (a constant
set + an adjacency table), so these are plain unit tests.
"""
import pytest

from positions import states


def test_valid_states_count_matches_spec():
    # 13 lifecycle states + 6 exception states, per the roadmap directive.
    assert len(states.VALID_STATES) == 19


def test_transitions_cover_every_valid_state():
    assert set(states.TRANSITIONS) == states.VALID_STATES


def test_is_valid_state():
    assert states.is_valid_state(states.SETUP_DETECTED)
    assert states.is_valid_state(states.RECOVERY_REQUIRED)
    assert not states.is_valid_state("NOT_A_REAL_STATE")
    assert not states.is_valid_state(None)
    assert not states.is_valid_state(123)


@pytest.mark.parametrize("from_state,to_state", [
    (states.SETUP_DETECTED, states.ARMED),
    (states.ARMED, states.ENTRY_RESERVED),
    (states.ENTRY_RESERVED, states.ENTRY_SUBMITTED),
    (states.ENTRY_SUBMITTED, states.PARTIALLY_FILLED),
    (states.ENTRY_SUBMITTED, states.FILLED),
    (states.PARTIALLY_FILLED, states.PARTIALLY_FILLED),
    (states.PARTIALLY_FILLED, states.FILLED),
    (states.FILLED, states.STOP_ACTIVE),
    (states.STOP_ACTIVE, states.TARGET_1_ACTIVE),
    (states.STOP_ACTIVE, states.EXIT_SUBMITTED),
    (states.TARGET_1_ACTIVE, states.PARTIAL_EXIT_SUBMITTED),
    (states.TARGET_1_ACTIVE, states.EXIT_SUBMITTED),
    (states.PARTIAL_EXIT_SUBMITTED, states.PARTIAL_EXITED),
    (states.PARTIAL_EXITED, states.TRAILING),
    (states.PARTIAL_EXITED, states.EXIT_SUBMITTED),
    (states.TRAILING, states.EXIT_SUBMITTED),
    (states.EXIT_SUBMITTED, states.CLOSED),
    (states.RECOVERY_REQUIRED, states.MANUAL_REVIEW),
    (states.RECOVERY_REQUIRED, states.CLOSED),
    (states.MANUAL_REVIEW, states.CLOSED),
])
def test_legal_transitions_do_not_raise(from_state, to_state):
    states.validate_transition(from_state, to_state)


@pytest.mark.parametrize("from_state,to_state", [
    (states.ENTRY_SUBMITTED, states.CLOSED),  # skipping every fill/exit step
    (states.SETUP_DETECTED, states.FILLED),
    (states.CLOSED, states.ARMED),  # terminal state has no outgoing edge
    (states.REJECTED, states.ENTRY_SUBMITTED),
    (states.FILLED, states.EXIT_SUBMITTED),  # must pass through STOP_ACTIVE
])
def test_illegal_transitions_raise(from_state, to_state):
    with pytest.raises(states.InvalidTransitionError):
        states.validate_transition(from_state, to_state)


def test_validate_transition_rejects_unknown_from_or_to_state():
    with pytest.raises(states.InvalidTransitionError):
        states.validate_transition("GARBAGE", states.ARMED)
    with pytest.raises(states.InvalidTransitionError):
        states.validate_transition(states.ARMED, "GARBAGE")


def test_terminal_states_have_no_outgoing_transitions():
    for state in states.TERMINAL_STATES:
        assert states.TRANSITIONS[state] == set()


def test_fail_closed_state_is_recovery_required():
    assert states.FAIL_CLOSED_STATE == states.RECOVERY_REQUIRED
    assert states.FAIL_CLOSED_STATE in states.NON_TERMINAL_STATES
