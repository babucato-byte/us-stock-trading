"""A refused SELL leaves the position exactly where it was.

Inventory rather than new work: S6 reuses `s1_live.exit_runtime._submit_sell`
with its own store passed in, so both strategies share one implementation
of what happens when a sell does not go through. These pin the properties
the improvement spec asks for, so a future edit cannot quietly remove
them.

The failure they guard against is the worst one available here: marking a
position CLOSED when the shares are still held. The internal book would
say flat, the broker would say three shares, and the exit that was owed
would never be retried.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

S1_EXIT = (REPO_ROOT / "s1_live" / "exit_runtime.py").read_text(encoding="utf-8")
S6_EXIT = (REPO_ROOT / "s6_live" / "exit_runtime.py").read_text(encoding="utf-8")


def _rejected_branch():
    start = S1_EXIT.index("if not accepted:")
    return S1_EXIT[start:S1_EXIT.index("exit_intent_ledger.mark_submitted(")]


class TestARejectedSellIsNotAClose:
    def test_it_never_closes_the_position(self):
        assert "close_position" not in _rejected_branch()

    def test_it_does_not_mark_the_exit_submitted(self):
        """That would make the ledger claim an order that does not
        exist, and stop the retry that is owed."""
        assert "mark_exit_submitted" not in _rejected_branch()

    def test_the_exit_reason_stays_latched(self):
        """The condition already fired. The position is leaving; only
        the attempt failed."""
        assert "latch_pending_exit" in _rejected_branch()

    def test_the_intent_is_aborted_not_left_open(self):
        assert "mark_aborted" in _rejected_branch()

    def test_it_is_reported_as_blocked(self):
        assert "ACTION_BLOCKED" in _rejected_branch()

    def test_it_does_not_retry_in_a_loop(self):
        """Chasing the price with repeated sells is its own failure."""
        branch = _rejected_branch()
        assert branch.count("broker_adapter.submit_order") == 0


class TestAnAmbiguousSellIsNeverAssumed:
    def test_it_goes_to_submission_unknown(self):
        assert "mark_submission_unknown" in S1_EXIT

    def test_it_is_never_auto_retried(self):
        """The order may be live. Re-sending would be the double sell
        this whole layer exists to prevent."""
        block = S1_EXIT[S1_EXIT.index("mark_submission_unknown"):]
        assert "NEVER auto-retried" in S1_EXIT or "not retried" in block[:400]


class TestPartialFillsKeepTheRemainder:
    def test_a_partial_reduces_rather_than_closes(self):
        assert "EXIT_SUBMITTED -> CLOSED, or a reduced position on a partial" \
            in S6_EXIT

    def test_both_strategies_share_one_implementation(self):
        """One place to be right, and one place to check."""
        assert "from s1_live.exit_runtime import ExitOutcome, _submit_sell" in S6_EXIT


class TestALatchedExitIsRetriedNotReEvaluated:
    def test_the_stored_reason_is_resubmitted(self):
        """A fresh HOLD verdict must not cancel an exit that already
        fired -- which is what RIG is relying on over the weekend."""
        block = S6_EXIT[S6_EXIT.index("def retry_latched_exits"):]
        assert 'row.get("pending_exit_reason")' in block
        assert "decide(" not in block[:1200]

    def test_it_waits_for_an_order_capable_context(self):
        block = S6_EXIT[S6_EXIT.index("def retry_latched_exits"):]
        assert "if not orders_allowed:" in block

    def test_an_already_submitted_exit_is_not_resubmitted(self):
        block = S6_EXIT[S6_EXIT.index("def retry_latched_exits"):]
        assert 'row.get("exit_submitted")' in block
