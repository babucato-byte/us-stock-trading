"""One vocabulary, and the distinction it exists to protect.

The scanner, the trading runtime and Slack each had their own words for
a candidate, and they did not mean the same thing. A Slack line saying
"실거래 가능 0" was counting SCANNED rows that no execution gate had ever
been asked about -- on a day a real BUY filled. A count that can be zero
while a fill happens is not a count of anything.

READY_TO_BUY is the STRATEGY saying yes. EXECUTABLE is the ACCOUNT also
saying yes. Reporting either as the other misleads in a different
direction, and this file pins both.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from domain import candidate_state as cs  # noqa: E402
from s6_live import precision_watch as pw  # noqa: E402


class TestTheModelIsComplete:
    def test_every_state_the_spec_names_exists(self):
        for name in ("SCANNED", "WATCHING", "READY_TO_BUY", "EXECUTABLE",
                     "BUY_SUBMITTED", "OPEN", "INVALIDATED", "BLOCKED"):
            assert getattr(cs, name) == name

    def test_all_lists_them_once(self):
        assert len(cs.ALL) == len(set(cs.ALL)) == 8

    def test_the_progression_is_coarse_to_committed(self):
        assert cs.ORDER == (cs.SCANNED, cs.WATCHING, cs.READY_TO_BUY,
                            cs.EXECUTABLE, cs.BUY_SUBMITTED, cs.OPEN)

    def test_committed_states_are_where_money_is_at_risk(self):
        assert cs.COMMITTED == {cs.BUY_SUBMITTED, cs.OPEN}
        assert not (cs.COMMITTED & cs.PRE_ORDER)


class TestReadyIsNotExecutable:
    """§6 -- the distinction the whole model exists for."""

    def test_only_executable_is_tradeable(self):
        assert cs.is_tradeable(cs.EXECUTABLE) is True
        for state in (cs.SCANNED, cs.WATCHING, cs.READY_TO_BUY,
                      cs.INVALIDATED, cs.BLOCKED):
            assert cs.is_tradeable(state) is False, state

    def test_ready_is_not_permission_to_buy(self):
        """A candidate can be READY all day and never be EXECUTABLE."""
        assert cs.READY_TO_BUY not in cs.TRADEABLE_STATES

    def test_executable_ranks_above_ready(self):
        assert cs.advanced(cs.READY_TO_BUY, cs.EXECUTABLE) is True
        assert cs.advanced(cs.EXECUTABLE, cs.READY_TO_BUY) is False

    def test_blocked_is_the_gap_between_them(self):
        """The strategy wanted it; the account refused. An operator's
        problem, not a strategy signal to tune."""
        assert cs.BLOCKED in cs.PRE_ORDER
        assert cs.rank_of(cs.BLOCKED) == -1

    def test_invalidated_and_blocked_are_different_failures(self):
        assert cs.INVALIDATED != cs.BLOCKED
        assert cs.rank_of(cs.INVALIDATED) == -1


class TestTheExecutionClaimIsRestricted:
    """§7 -- a scanner has not consulted cash, ownership, reconciliation
    or the broker route, and must not use words implying it has."""

    def test_the_phrase_that_was_printed_is_recognised(self):
        assert cs.describes_execution("실거래 가능 후보 0") is True

    def test_english_equivalents_are_recognised(self):
        assert cs.describes_execution("0 tradeable candidates") is True
        assert cs.describes_execution("EXECUTABLE: 0") is True

    def test_scanner_wording_is_not_an_execution_claim(self):
        for text in ("스캔 후보 30", "Watch 후보 12", "READY 후보 2",
                     "SCANNED: 30"):
            assert cs.describes_execution(text) is False, text

    def test_a_claim_is_only_honest_for_an_executable_candidate(self):
        """The rule stated as a single predicate a reporter can call."""
        for state in cs.ALL:
            claim_allowed = cs.is_tradeable(state)
            assert claim_allowed == (state == cs.EXECUTABLE)


class TestTheWatchSpeaksTheSharedVocabulary:
    def test_it_re_exports_rather_than_restates(self):
        assert pw.READY_TO_BUY is cs.READY_TO_BUY
        assert pw.WATCHING is cs.WATCHING
        assert pw.INVALIDATED is cs.INVALIDATED

    def test_its_old_names_still_resolve(self):
        assert pw.DISCOVERED == cs.SCANNED
        assert pw.DROPPED == cs.INVALIDATED

    def test_the_watch_never_claims_executable(self):
        """It evaluates the STRATEGY's conditions. Cash, ownership and
        the broker route are the execution gate's question, and a watch
        that answered them would be a second opinion."""
        import inspect

        source = inspect.getsource(pw._evaluate)
        assert "EXECUTABLE" not in source

    def test_a_ready_evaluation_is_not_tradeable_on_its_own(self):
        evaluation = pw.WatchEvaluation(symbol="DT", session="REGULAR",
                                        state=pw.READY_TO_BUY)
        assert cs.is_tradeable(evaluation.state) is False
