"""Stage 4 (roadmap Phase 5): position lifecycle state machine tests.

No network, no real files -- positions/states.py is pure logic (a constant
set + an adjacency table), so these are plain unit tests.
"""
import pytest

from positions import states


def test_valid_states_count_matches_spec():
    # 13 lifecycle states + 6 exception states, per the roadmap directive,
    # plus EXTERNALLY_CLOSED -- added when the operator sold TX by hand and
    # the machine had no honest terminal state for shares it did not sell.
    assert len(states.VALID_STATES) == 20


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


class TestExternallyClosed:
    """Terminal, and deliberately not CLOSED.

    CLOSED asserts this system sold and knows what the shares fetched.
    When someone closes a position elsewhere, reusing CLOSED would put an
    exit this system never made into the strategy's realized record --
    and, with no price to record, one that reads as a scratch.
    """

    def test_it_is_terminal(self):
        assert states.EXTERNALLY_CLOSED in states.TERMINAL_STATES
        assert states.TRANSITIONS[states.EXTERNALLY_CLOSED] == set()

    def test_only_states_that_actually_hold_shares_can_reach_it(self):
        reachable = {name for name, targets in states.TRANSITIONS.items()
                     if states.EXTERNALLY_CLOSED in targets}
        assert reachable == {
            states.PARTIALLY_FILLED, states.FILLED, states.STOP_ACTIVE,
            states.TARGET_1_ACTIVE, states.PARTIAL_EXITED, states.TRAILING,
        }

    def test_a_position_that_never_filled_cannot_be_externally_closed(self):
        for origin in (states.SETUP_DETECTED, states.ARMED,
                       states.ENTRY_RESERVED, states.ENTRY_SUBMITTED):
            with pytest.raises(states.InvalidTransitionError):
                states.validate_transition(origin, states.EXTERNALLY_CLOSED)

    def test_an_exit_in_flight_cannot_be_externally_closed(self):
        """It settles to CLOSED with a real fill price instead."""
        with pytest.raises(states.InvalidTransitionError):
            states.validate_transition(states.EXIT_SUBMITTED,
                                       states.EXTERNALLY_CLOSED)

    def test_a_state_a_human_is_adjudicating_cannot_be_externally_closed(self):
        for origin in (states.MANUAL_REVIEW, states.RECOVERY_REQUIRED):
            with pytest.raises(states.InvalidTransitionError):
                states.validate_transition(origin, states.EXTERNALLY_CLOSED)
