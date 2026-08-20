"""The risk matrix: designed and tested, deliberately not in force.

Two obligations pull against each other here, and both have to hold.

The proposed matrix has to be exercised properly -- §12 names five cases
and every one of them is checked -- because a limit whose behaviour is
unknown is not a design, it is an intention.

And running those tests must not raise what the account can actually
lose. So the proposed matrix is reached through a PARAMETER, never by
setting the module flag: a test that flipped `ACTIVE` would leave it
flipped for whatever ran next, and an unapproved limit would reach a
live account by way of a fixture.
"""

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import position_limits as pl  # noqa: E402

S1 = "S1_HMA_EARLY_TREND_V1"
S2 = "S2_VOLUME_ACCUMULATION_V1"


def proposed(strategy, **open_counts):
    """Ask the PROPOSED matrix without activating it."""
    return pl.check_entry(strategy, open_counts, active=True)


class TestTheFiveCasesFromTheDirective:
    def test_s1_1_s2_0_allows_an_s2_entry(self):
        decision = proposed(S2, **{S1: 1})
        assert decision.allowed is True
        assert decision.reason == pl.ALLOW

    def test_s1_1_s2_1_is_the_full_allowed_book(self):
        """Both limits satisfied, global exactly at capacity."""
        assert proposed(S1, **{S1: 0, S2: 1}).allowed is True
        assert proposed(S2, **{S1: 1, S2: 0}).allowed is True

    def test_a_second_s1_is_blocked_by_the_strategy_limit(self):
        """Not by the global limit -- the global cap would have allowed a
        second position, and reporting the wrong one would send an
        operator to change the wrong number."""
        decision = proposed(S1, **{S1: 1})
        assert decision.allowed is False
        assert decision.reason == pl.BLOCK_STRATEGY

    def test_a_second_s2_is_blocked_by_the_strategy_limit(self):
        decision = proposed(S2, **{S2: 1})
        assert decision.allowed is False
        assert decision.reason == pl.BLOCK_STRATEGY

    def test_a_third_position_is_blocked_by_the_global_limit(self):
        """S1=1, S2=1 already; anything further is refused globally."""
        decision = proposed(S1, **{S1: 1, S2: 1})
        assert decision.allowed is False
        # S1 is at its own limit too, and the strategy limit is checked
        # first because it is the more specific reason.
        assert decision.reason == pl.BLOCK_STRATEGY

    def test_a_third_position_from_a_strategy_with_room_is_blocked_globally(self):
        """The case that isolates the global cap: a strategy under its own
        limit still cannot push the book past two."""
        decision = pl.check_entry(
            S2, {S1: 1, S2: 0, "S3_FUTURE": 1},
            active=True)
        assert decision.allowed is False
        assert decision.reason == pl.BLOCK_GLOBAL


class TestBothLimitsApplyIndependently:
    def test_the_global_cap_is_not_a_sum_of_the_strategy_caps(self):
        """Adding a strategy row must not silently raise total exposure.
        That is how limit systems usually fail."""
        assert pl.PROPOSED_GLOBAL_MAX == 2
        assert sum(pl.PROPOSED_STRATEGY_MAX.values()) == 2
        # A third strategy at 1 would make the sum 3; the global cap
        # still holds the book at 2.
        decision = pl.check_entry("S3_FUTURE", {S1: 1, S2: 1}, active=True)
        assert decision.allowed is False

    def test_an_unknown_strategy_gets_no_allowance(self):
        """Fail closed: "not yet decided" must not read as "no ceiling"."""
        decision = pl.check_entry("S9_UNAGREED", {}, active=True)
        assert decision.allowed is False
        assert decision.reason == pl.BLOCK_UNKNOWN_STRATEGY

    def test_an_empty_book_allows_a_first_position(self):
        assert proposed(S1).allowed is True
        assert proposed(S2).allowed is True

    def test_the_decision_carries_the_numbers_it_used(self):
        """A refusal an operator cannot act on is a refusal they will
        override."""
        decision = proposed(S1, **{S1: 1})
        assert decision.limits["global_max"] == 2
        assert decision.limits["strategy_max"] == 1
        assert "holds 1" in decision.detail
        assert decision.as_dict()["reason"] == pl.BLOCK_STRATEGY


class TestActivationIsHeld:
    def test_the_matrix_is_not_in_force(self):
        """§12: implementation and tests proceed, activation does not."""
        assert pl.ACTIVE is False

    def test_the_live_limits_are_todays_posture_not_the_proposal(self):
        global_max, strategy_max = pl.effective_limits()
        assert global_max == 1, "one position, as today"
        assert strategy_max == {S1: 1}
        assert S2 not in strategy_max, "S2 is DISCOVERY_ONLY and holds nothing"

    def test_s2_cannot_open_a_position_under_the_live_limits(self):
        decision = pl.check_entry(S2, {})
        assert decision.allowed is False
        assert decision.reason == pl.BLOCK_UNKNOWN_STRATEGY

    def test_a_second_s1_is_refused_under_the_live_limits_too(self):
        assert pl.check_entry(S1, {S1: 1}).allowed is False

    def test_the_first_s1_position_still_works(self):
        """The live posture must be unchanged by this file existing -- TX
        is open and S1 keeps trading."""
        assert pl.check_entry(S1, {}).allowed is True

    def test_no_code_in_the_repo_sets_active_to_true(self):
        """The value is the record of a decision, and a decision that can
        be made by a fixture is not one."""
        offenders = []
        for path in REPO_ROOT.rglob("*.py"):
            if "venv" in path.parts or path.name == "position_limits.py":
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "position_limits" not in source and "ACTIVE" not in source:
                continue
            for node in ast.walk(ast.parse(source)):
                # position_limits.ACTIVE = True
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if (isinstance(target, ast.Attribute)
                                and target.attr == "ACTIVE"
                                and isinstance(node.value, ast.Constant)
                                and node.value.value is True):
                            offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == [], f"ACTIVE set from code: {offenders}"

    def test_activating_it_is_a_one_line_change_with_no_other_edit(self):
        """The design is complete: turning it on must not require also
        writing the logic, or the review that approves it is approving
        something that does not exist yet."""
        _, strategy_max = pl.effective_limits(active=True)
        assert strategy_max == {S1: 1, S2: 1}
        assert pl.check_entry(S2, {S1: 1}, active=True).allowed is True
