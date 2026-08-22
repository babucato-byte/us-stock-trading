"""What happened after an S6 candidate, measured without inventing it.

The property that carries this file: an unelapsed horizon is None, never
zero and never the last known price carried forward. A candidate twenty
minutes old genuinely has no +1h return, and filling it would put a
fabricated number into an average that a threshold decision later rests
on -- which is the whole reason the shadow dataset exists.

The second property is that populations stay apart. Judging tradeable
instruments by untradeable ones, or the reverse, is what a single
blended number would do.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scanners.analytics import s6_candidate_tracker as t  # noqa: E402

T0 = datetime(2026, 8, 21, 14, 0, tzinfo=timezone.utc)


def bar(minute, close, high=None, low=None, vwap=None):
    return {"timestamp": T0 + timedelta(minutes=minute), "close": close,
            "high": high if high is not None else close,
            "low": low if low is not None else close, "vwap": vwap}


def candidate(**kw):
    base = {"symbol": "ABC", "price": 100.0, "range_high": 99.5,
            "range_low": 99.0, "variant": "S6-R", "session": "REGULAR",
            "security_type": "COMMON_STOCK", "live_eligible": True,
            "score": 62.75, "volume_expansion": 1.39}
    base.update(kw)
    return base


class TestUnelapsedHorizonsAreAbsent:
    def test_a_young_candidate_has_no_long_horizon(self):
        row = t.follow(candidate(), [bar(0, 100.0), bar(10, 101.0)],
                       candidate_time=T0)
        assert row["return_5m"] is not None
        assert row["return_30m"] is None
        assert row["return_60m"] is None

    def test_the_last_price_is_not_carried_forward(self):
        """Carrying it would report the present as the future."""
        row = t.follow(candidate(), [bar(0, 100.0), bar(20, 105.0)],
                       candidate_time=T0)
        assert row["price_60m"] is None
        assert row["return_60m"] is None

    def test_no_bars_measures_nothing(self):
        row = t.follow(candidate(), [], candidate_time=T0)
        assert row["bars_seen"] == 0
        assert row["mfe_pct"] is None and row["mae_pct"] is None
        assert all(row[f"return_{m}m"] is None for m in t.HORIZONS)

    def test_an_absent_session_close_is_none(self):
        row = t.follow(candidate(), [bar(0, 100.0)], candidate_time=T0)
        assert row["return_close"] is None


class TestReturnsAndExcursions:
    def bars(self):
        return [bar(0, 100.0, high=100.2, low=99.9),
                bar(5, 101.0, high=101.5, low=100.5),
                bar(15, 99.0, high=101.0, low=98.0),
                bar(30, 102.0, high=103.0, low=99.0),
                bar(60, 100.5, high=102.0, low=100.0)]

    def test_each_horizon_is_measured_from_the_candidate_price(self):
        row = t.follow(candidate(), self.bars(), candidate_time=T0)
        assert row["return_5m"] == pytest.approx(1.0)
        assert row["return_15m"] == pytest.approx(-1.0)
        assert row["return_30m"] == pytest.approx(2.0)

    def test_mfe_is_the_best_high_and_is_clamped_at_zero(self):
        row = t.follow(candidate(), self.bars(), candidate_time=T0)
        assert row["mfe_pct"] == pytest.approx(3.0)
        assert row["time_to_peak_minutes"] == 30

    def test_mae_is_the_worst_low_and_is_clamped_at_zero(self):
        row = t.follow(candidate(), self.bars(), candidate_time=T0)
        assert row["mae_pct"] == pytest.approx(-2.0)
        assert row["time_to_max_adverse_minutes"] == 15

    def test_a_candidate_that_only_went_up_has_zero_mae(self):
        row = t.follow(candidate(), [bar(0, 100.0, high=101.0, low=100.0)],
                       candidate_time=T0)
        assert row["mae_pct"] == 0.0

    def test_the_session_close_return_is_recorded(self):
        row = t.follow(candidate(), self.bars(), candidate_time=T0,
                       session_close_price=104.0)
        assert row["return_close"] == pytest.approx(4.0)


class TestTheResearchLabels:
    def test_price_back_inside_the_range_is_a_reentry(self):
        row = t.follow(candidate(range_high=99.5),
                       [bar(0, 100.0), bar(10, 99.0)], candidate_time=T0)
        assert row["range_reentry_15m"] is True
        assert row["time_to_range_reentry_minutes"] == 10

    def test_holding_above_the_range_is_not_a_reentry(self):
        row = t.follow(candidate(range_high=99.5),
                       [bar(0, 100.0), bar(10, 101.0), bar(30, 102.0)],
                       candidate_time=T0)
        assert row["range_reentry_30m"] is False
        assert row["time_to_range_reentry_minutes"] is None

    def test_an_unelapsed_horizon_has_no_label(self):
        """False would claim it survived a window it never reached."""
        row = t.follow(candidate(), [bar(0, 100.0)], candidate_time=T0)
        assert row["range_reentry_60m"] is None

    def test_a_missing_range_cannot_be_reentered(self):
        row = t.follow(candidate(range_high=None), [bar(0, 100.0)],
                       candidate_time=T0)
        assert row["range_reentry_5m"] is None

    def test_vwap_failure_needs_a_vwap(self):
        row = t.follow(candidate(), [bar(0, 100.0), bar(10, 99.0)],
                       candidate_time=T0)
        assert row["vwap_failure_15m"] is None

    def test_a_close_below_vwap_is_a_failure(self):
        row = t.follow(candidate(),
                       [bar(0, 100.0, vwap=100.0), bar(10, 99.0, vwap=100.0)],
                       candidate_time=T0)
        assert row["vwap_failure_15m"] is True
        assert row["vwap_failure_5m"] is False

    def test_false_breakout_follows_the_exit_policys_definition(self):
        """Re-entry into the broken range -- the same phenomenon
        S6_EXIT_V0 exits on, recorded here rather than applied."""
        row = t.follow(candidate(range_high=99.5),
                       [bar(0, 100.0), bar(20, 99.0)], candidate_time=T0)
        assert row["false_breakout"] is row["range_reentry_30m"] is True

    def test_the_label_drives_nothing(self):
        import ast

        source = (REPO_ROOT / "scanners" / "analytics"
                  / "s6_candidate_tracker.py").read_text()
        banned = {"brokers", "kis_broker", "execution", "order_gate",
                  "s6_live", "position_limits", "kis_live_trading"}
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [str(getattr(node, "module", "") or "")]
                names += [a.name for a in node.names]
                for name in names:
                    for segment in name.split("."):
                        assert segment not in banned, name


class TestQualityFeaturesTravel:
    @pytest.mark.parametrize("field", [
        "security_type", "live_eligible", "score", "volume_expansion",
        "daily_relative_volume", "absolute_volume", "dollar_volume",
        "breakout_pct", "opening_range_width_pct",
        "normalized_breakout_by_range", "vwap_distance_pct",
        "ema_spread_pct", "score_breakout_quality", "score_volume_expansion",
        "score_entry_proximity", "score_vwap", "score_retest"])
    def test_every_requested_feature_is_on_the_row(self, field):
        row = t.follow(candidate(), [bar(0, 100.0)], candidate_time=T0)
        assert field in row


class TestPopulationsStayApart:
    def rows(self):
        return [
            t.follow(candidate(symbol="AAPL"), [bar(0, 100.0), bar(5, 102.0)],
                     candidate_time=T0),
            t.follow(candidate(symbol="IEFA", security_type="ETP",
                               live_eligible=False),
                     [bar(0, 100.0), bar(5, 100.1)], candidate_time=T0),
            t.follow(candidate(symbol="???", security_type="UNKNOWN",
                               live_eligible=False),
                     [bar(0, 100.0), bar(5, 99.0)], candidate_time=T0),
        ]

    def test_each_population_is_reported_separately(self):
        summary = t.summarise(self.rows())
        assert summary[t.POP_ALL]["candidates"] == 3
        assert summary[t.POP_LIVE]["candidates"] == 1
        assert summary[t.POP_ETP]["candidates"] == 1
        assert summary[t.POP_UNKNOWN]["candidates"] == 1

    def test_the_tradeable_population_is_not_diluted(self):
        """A blended number would let the instruments that can be traded
        be judged by the ones that cannot."""
        summary = t.summarise(self.rows())
        assert summary[t.POP_LIVE]["return_5m"]["mean"] == pytest.approx(2.0)
        assert summary[t.POP_ALL]["return_5m"]["mean"] != pytest.approx(2.0)

    def test_all_observed_includes_everything(self):
        summary = t.summarise(self.rows())
        assert summary[t.POP_ALL]["candidates"] == sum(
            summary[p]["candidates"] for p in
            (t.POP_LIVE, t.POP_ETP, t.POP_UNKNOWN))

    def test_every_mean_carries_its_count(self):
        """An average of one and an average of a hundred are not
        comparable, and a summary that hides which invites treating them
        alike."""
        summary = t.summarise(self.rows())
        assert summary[t.POP_LIVE]["return_5m"]["n"] == 1
        assert summary[t.POP_ALL]["mfe_pct"]["n"] == 3

    def test_an_unmeasurable_mean_is_none_not_zero(self):
        summary = t.summarise([t.follow(candidate(), [], candidate_time=T0)])
        assert summary[t.POP_ALL]["return_60m"]["mean"] is None
        assert summary[t.POP_ALL]["return_60m"]["n"] == 0

    def test_the_false_breakout_rate_reports_its_sample(self):
        summary = t.summarise(self.rows())
        assert "n" in summary[t.POP_ALL]["false_breakout_rate"]
