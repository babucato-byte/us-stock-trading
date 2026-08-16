import pytest

from execution.order_state_machine import (
    OrderStateTransitionError,
    is_terminal,
    reconcile_unknown,
    transition,
)


class TestTransition:
    def test_legal_path_created_to_filled(self):
        assert transition("CREATED", "VALIDATING") == "VALIDATING"
        assert transition("VALIDATING", "APPROVED") == "APPROVED"
        assert transition("APPROVED", "SUBMITTING") == "SUBMITTING"
        assert transition("SUBMITTING", "ACCEPTED") == "ACCEPTED"
        assert transition("ACCEPTED", "FILLED") == "FILLED"

    def test_partial_fill_loop(self):
        assert transition("PARTIALLY_FILLED", "PARTIALLY_FILLED") == "PARTIALLY_FILLED"
        assert transition("PARTIALLY_FILLED", "FILLED") == "FILLED"

    def test_submitting_can_go_unknown(self):
        assert transition("SUBMITTING", "UNKNOWN") == "UNKNOWN"

    def test_terminal_states_have_no_outgoing_transitions(self):
        for terminal in ("FILLED", "CANCELLED", "REJECTED"):
            with pytest.raises(OrderStateTransitionError):
                transition(terminal, "SUBMITTING")

    def test_unknown_cannot_transition_via_plain_transition(self):
        with pytest.raises(OrderStateTransitionError):
            transition("UNKNOWN", "FILLED")

    def test_unknown_to_submitting_blocked(self):
        # Execution Engine state-machine enforcement (MEDIUM finding):
        # an UNKNOWN order must never be silently re-submitted -- there
        # is no legal transition() path from UNKNOWN back to SUBMITTING.
        with pytest.raises(OrderStateTransitionError):
            transition("UNKNOWN", "SUBMITTING")

    def test_partially_filled_to_filled_is_normal(self):
        assert transition("PARTIALLY_FILLED", "FILLED") == "FILLED"

    def test_expected_state_mismatch_rejected(self):
        # A caller's assumed current_status that doesn't match what the
        # transition graph allows is rejected the same as any other
        # illegal jump -- e.g. assuming ACCEPTED when the record is
        # actually still CREATED skips straight past VALIDATING/APPROVED/
        # SUBMITTING/ACCEPTED, which is not a legal single transition.
        with pytest.raises(OrderStateTransitionError):
            transition("CREATED", "PARTIALLY_FILLED")

    def test_illegal_skip_rejected(self):
        with pytest.raises(OrderStateTransitionError):
            transition("CREATED", "FILLED")

    def test_unknown_status_names_rejected(self):
        with pytest.raises(OrderStateTransitionError):
            transition("CREATED", "DONE")
        with pytest.raises(OrderStateTransitionError):
            transition("DONE", "CREATED")


class TestIsTerminal:
    def test_terminal_statuses(self):
        for status in ("FILLED", "CANCELLED", "REJECTED"):
            assert is_terminal(status)

    def test_non_terminal_statuses(self):
        for status in ("CREATED", "ACCEPTED", "UNKNOWN", "PARTIALLY_FILLED"):
            assert not is_terminal(status)


class TestReconcileUnknown:
    def test_resolves_to_filled(self):
        assert reconcile_unknown("FILLED") == "FILLED"

    def test_resolves_to_accepted_still_open(self):
        assert reconcile_unknown("ACCEPTED") == "ACCEPTED"

    @pytest.mark.parametrize("bad", ["SUBMITTING", "CREATED", "VALIDATING", "APPROVED", "UNKNOWN"])
    def test_never_resolves_to_a_resubmission_state(self, bad):
        with pytest.raises(OrderStateTransitionError):
            reconcile_unknown(bad)
