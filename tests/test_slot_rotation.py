"""How long a symbol keeps its realtime slot, and why it lost it.

The obvious control for churn is hysteresis, and it needs a number. There
is no evidence for any particular one yet, and picking 1.15 today would
mean every later question about churn is answered by a constant nobody
measured -- hard to argue with precisely because it would already be in
production. So this measures and decides nothing.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s6_live import slot_rotation as rot  # noqa: E402

NOW = datetime(2026, 8, 31, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path):
    return {"SLOT_ROTATION_DIR": str(tmp_path)}


def _record(**kw):
    fields = dict(symbol="OWL", session="REGULAR", entered_at=NOW,
                  removed_at=NOW + timedelta(minutes=10),
                  replacement_reason=rot.REASON_OUTRANKED)
    fields.update(kw)
    return rot.build_record(**fields)


class TestATenureIsMeasured:
    def test_the_slot_duration_is_recorded(self):
        assert _record()["slot_duration_seconds"] == pytest.approx(600.0)

    def test_a_missing_end_leaves_the_duration_unknown(self):
        assert _record(removed_at=None)["slot_duration_seconds"] is None

    def test_a_negative_duration_is_unknown_not_negative(self):
        """Same discipline as the slippage log: a backwards interval is
        evidence the stamps are wrong, not a fast tenure."""
        out = _record(entered_at=NOW + timedelta(minutes=5), removed_at=NOW)
        assert out["slot_duration_seconds"] is None


class TestHowMuchBetterTheReplacementWas:
    """The input to any future hysteresis argument: a swap for a 0.1%
    rank difference and one for a 40% difference are not the same
    event."""

    def test_the_rank_delta_is_recorded(self):
        out = _record(incumbent_rank=10.0, replacement_symbol="HOT",
                      replacement_rank=4.0)
        assert out["rank_delta"] == pytest.approx(6.0)
        assert out["replacement_symbol"] == "HOT"

    def test_a_missing_rank_leaves_the_delta_unknown(self):
        assert _record(incumbent_rank=10.0)["rank_delta"] is None

    def test_a_marginal_swap_is_recorded_as_marginal(self):
        out = _record(incumbent_rank=10.0, replacement_rank=9.9)
        assert out["rank_delta"] == pytest.approx(0.1, abs=1e-9)


class TestASlotLostMidWarmupProducedNothing:
    """Churn is not automatically bad -- it may be the ranking working.
    What is bad is a slot spent accumulating history that is discarded."""

    def test_losing_a_slot_while_warming_up_is_flagged(self):
        out = _record(state_at_removal="WARMING_UP")
        assert out["wasted_warmup"] is True

    def test_losing_a_slot_while_watching_is_not(self):
        assert _record(state_at_removal="WATCHING")["wasted_warmup"] is False

    def test_the_summary_counts_them(self, env):
        rot.append(_record(state_at_removal="WARMING_UP"),
                   trading_day="D", env=env)
        rot.append(_record(state_at_removal="WATCHING"),
                   trading_day="D", env=env)
        assert rot.churn_summary("D", env=env)["wasted_warmups"] == 1


class TestTheSummaryDescribesAndDoesNotRecommend:
    def test_it_reports_tenure_and_reasons(self, env):
        for reason in (rot.REASON_OUTRANKED, rot.REASON_OUTRANKED,
                       rot.REASON_INVALIDATED):
            rot.append(_record(replacement_reason=reason),
                       trading_day="D", env=env)
        out = rot.churn_summary("D", env=env)
        assert out["rotations"] == 3
        assert out["by_reason"][rot.REASON_OUTRANKED] == 2
        assert out["median_slot_seconds"] == pytest.approx(600.0)

    def test_it_defines_no_hysteresis_constant(self):
        """The decision this data exists to inform, not pre-empt.

        Checked against the CODE: the module discusses hysteresis in
        prose deliberately, and a substring search would match the
        explanation of why there is no constant.
        """
        import ast

        tree = ast.parse((REPO_ROOT / "s6_live" / "slot_rotation.py").read_text())
        assigned = {target.id
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Assign)
                    for target in node.targets
                    if isinstance(target, ast.Name)}
        assert not [name for name in assigned if "HYSTERESIS" in name.upper()]

        numbers = {node.value for node in ast.walk(tree)
                   if isinstance(node, ast.Constant)
                   and isinstance(node.value, float)}
        assert 1.15 not in numbers

    def test_an_empty_day_reports_nothing_rather_than_zero(self, env):
        out = rot.churn_summary("D", env=env)
        assert out["rotations"] == 0
        assert out["median_slot_seconds"] is None

    def test_the_shortest_tenure_is_visible(self, env):
        rot.append(_record(removed_at=NOW + timedelta(seconds=20)),
                   trading_day="D", env=env)
        rot.append(_record(removed_at=NOW + timedelta(minutes=30)),
                   trading_day="D", env=env)
        assert rot.churn_summary("D", env=env)["shortest_slot_seconds"] \
            == pytest.approx(20.0)


class TestItWritesNowhereUnlessTold:
    def test_no_configured_root_writes_nothing(self):
        assert rot.log_path("D", env={}) is None
        assert rot.append(_record(), trading_day="D", env={}) is False

    def test_reading_without_a_root_is_empty(self):
        assert rot.read("D", env={}) == []

    def test_a_corrupt_line_does_not_lose_the_others(self, env):
        rot.append(_record(), trading_day="D", env=env)
        with open(rot.log_path("D", env=env), "a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        assert rot.churn_summary("D", env=env)["rotations"] == 1
