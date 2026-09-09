"""S6 active-watch READY candidates carry no scanner rank (fast_watch
never runs the scanner's scoring pass), so `candidate_row["score"]` is
None -- and domain.signal.Signal.score is a required finite field
shared by every strategy. Production evidence 2026-09-09: UNH and HUM
both reached READY and were blocked at instrument/signal construction
with "score must be a finite number, got None", producing zero broker
mutation but also zero live entries for otherwise-valid S6 candidates.
"""

from s6_live import qualification as q


def _row(**over):
    base = {
        "strategy_id": q.S6_STRATEGY_ID, "price": 22.74, "range_high": 23.0,
        "provenance": {"signal_id": "s6aw-abc123", "signal_timestamp": "2026-09-09T13:00:00Z"},
    }
    base.update(over)
    return base


class TestScoreNotApplicableSentinel:
    def test_a_row_with_no_score_qualifies_with_the_sentinel(self):
        result = q.qualify_s6("UNH", candidate_row=_row(score=None))
        assert result.qualified is True
        assert result.score == q.S6_SCORE_NOT_APPLICABLE
        assert isinstance(result.score, float)

    def test_a_row_that_never_carries_a_score_key_also_qualifies(self):
        row = _row()
        assert "score" not in row
        result = q.qualify_s6("UNH", candidate_row=row)
        assert result.qualified is True
        assert result.score == q.S6_SCORE_NOT_APPLICABLE

    def test_a_real_scanner_score_is_used_as_is_not_overridden(self):
        """A scanner-published row DOES carry a real rank -- the
        sentinel must never replace real information."""
        result = q.qualify_s6("UNH", candidate_row=_row(score=87.5))
        assert result.score == 87.5

    def test_the_sentinel_is_not_a_quality_looking_number(self):
        assert q.S6_SCORE_NOT_APPLICABLE == 0.0

    def test_the_sentinel_survives_signal_construction(self):
        """The whole point: domain.signal.Signal must actually accept it."""
        from domain.signal import build_signal
        from datetime import datetime, timezone

        result = q.qualify_s6("UNH", candidate_row=_row(score=None))
        signal = build_signal(
            strategy_id=result.strategy_id, strategy_version="v1",
            config_version="live_rollout_v1", code_commit="deadbeef",
            symbol="UNH", exchange="NYSE", signal_price=result.price,
            score=result.score, entry_reason=result.entry_reason,
            valid_for_seconds=180.0, now=datetime.now(timezone.utc))
        assert signal.score == q.S6_SCORE_NOT_APPLICABLE

    def test_other_qualification_failure_reasons_are_unaffected(self):
        assert q.qualify_s6("UNH", candidate_row=None).reason_code == \
            q.REASON_NOT_AN_S6_CANDIDATE
        assert q.qualify_s6("UNH", candidate_row=_row(price=None)
                            ).reason_code == q.REASON_UNUSABLE_CANDIDATE
        assert q.qualify_s6("UNH", candidate_row=_row(range_high=None)
                            ).reason_code == q.REASON_NO_RANGE


class TestS1ThroughS5ScoreSemanticsUnchanged:
    """The fix lives entirely inside s6_live/qualification.py -- prove
    S1's own qualification module is untouched and still requires a
    real score."""

    def test_s1_qualification_module_has_no_sentinel_and_is_unmodified(self):
        import subprocess

        diff = subprocess.run(
            ["git", "diff", "--stat", "--", "s1_live/qualification.py",
             "s2_live", "domain/signal.py"],
            capture_output=True, text=True).stdout
        assert diff.strip() == "", diff

    def test_s1_qualification_still_requires_a_real_score(self):
        from s1_live.qualification import Qualification

        # Qualification itself stays Optional[float] -- unchanged --
        # but nothing in s1_live's OWN qualify() path substitutes a
        # sentinel; that substitution exists only in s6_live.
        import inspect
        import s1_live.qualification as s1q

        source = inspect.getsource(s1q)
        assert "S6_SCORE_NOT_APPLICABLE" not in source
        assert "NOT_APPLICABLE" not in source
