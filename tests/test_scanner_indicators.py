"""Indicator and feature-pass behaviour.

The tests that matter most here are the agreement tests. `adx_series`
and `session_vwap` are new code sitting next to existing implementations
in `score_scanner/premarket_momentum_score.py` that the live premarket
scanner uses. If the two ever disagree, "ADX > 20" silently means two
different things in one repository -- the live technical filter's and
the new scanners' -- and every cross-scanner comparison in the month-end
report is quietly comparing apples to oranges. Those are pinned here.
"""

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from score_scanner.premarket_momentum_score import calculate_adx  # noqa: E402
from scanners.base import indicators as ind  # noqa: E402
from scanners.base import session as sess  # noqa: E402
from scanners.base.features import build_features, minimum_daily_bars  # noqa: E402
from scanners.base.market_data_provider import SymbolData  # noqa: E402
from scanners.base.models import ScannerDataError  # noqa: E402
from tests import scanner_fixtures as fx  # noqa: E402


class TestAgreementWithExistingCode:
    def test_adx_series_last_value_matches_existing_calculate_adx(self):
        """The new series and the existing scalar must be the same number.

        Not "close": identical. `adx_series` is a copy of the existing
        function's maths with the final `.dropna().iloc[-1]` removed, and
        this is what stops the copy drifting.
        """
        daily = fx.daily_frame(fx.accelerating_uptrend())
        series = ind.adx_series(daily, 14).dropna()
        assert not series.empty
        assert float(series.iloc[-1]) == pytest.approx(calculate_adx(daily, 14))

    def test_hma_is_the_repositorys_own_hma(self):
        """`scanners.base.indicators.hma` is re-exported, not reimplemented."""
        import indicators as repo_indicators

        assert ind.hma is repo_indicators.hma

    def test_session_vwap_matches_existing_vwap_for_a_single_session(self):
        """The only difference between the two is the day boundary."""
        from score_scanner.premarket_momentum_score import calculate_vwap

        frame = fx.intraday_frame(np.linspace(100, 102, 30))
        assert ind.last_valid(ind.session_vwap(frame)) == pytest.approx(
            ind.last_valid(calculate_vwap(frame)))

    def test_session_vwap_restarts_at_a_day_boundary(self):
        """A two-day frame must not carry one day's cumulative sum into
        the next -- the number that would produce is a VWAP of nothing."""
        import datetime as dt

        from score_scanner.premarket_momentum_score import calculate_vwap

        yesterday = fx.intraday_frame(
            np.full(30, 50.0), day=dt.date(2026, 8, 10))
        today = fx.intraday_frame(
            np.full(30, 100.0), day=dt.date(2026, 8, 11))
        combined = pd.concat([yesterday, today])

        # Session-aware: today's bars all traded at 100, so today's VWAP is 100.
        assert ind.last_valid(ind.session_vwap(combined)) == pytest.approx(100.0)
        # Naive cumulative: dragged down by yesterday's 50s.
        assert ind.last_valid(calculate_vwap(combined)) < 80.0


class TestDivisionAndNaNSafety:
    """Section S2 calls out divide-by-zero explicitly; section 28 adds
    NaN and zero volume. None of these may reach the stored dataset as
    `inf` or `NaN`."""

    def test_safe_ratio_refuses_zero_denominator(self):
        assert ind.safe_ratio(10, 0) is None
        assert ind.safe_ratio(10, None) is None
        assert ind.safe_ratio(None, 10) is None
        assert ind.safe_ratio(10, 4) == pytest.approx(2.5)

    def test_to_float_rejects_nan_and_inf(self):
        assert ind.to_float(float("nan")) is None
        assert ind.to_float(float("inf")) is None
        assert ind.to_float(float("-inf")) is None
        assert ind.to_float("not a number") is None
        assert ind.to_float("3.5") == pytest.approx(3.5)

    def test_volume_price_efficiency_survives_a_flat_price(self):
        """The interesting case is exactly where the denominator goes to
        zero: huge volume on an unchanged price is the accumulation
        signal, not an error."""
        value = ind.volume_price_efficiency(3.0, 0.0)
        assert value is not None
        assert math.isfinite(value)

    def test_average_volume_rejects_an_all_zero_window(self):
        daily = fx.daily_frame(np.linspace(10, 12, 40), volumes=np.zeros(40))
        assert ind.average_volume(daily, 20) is None

    def test_extension_pct_matches_the_spec_formula(self):
        assert ind.extension_pct(110, 100) == pytest.approx(10.0)
        assert ind.extension_pct(90, 100) == pytest.approx(-10.0)
        assert ind.extension_pct(100, 0) is None


class TestHighsAndDistances:
    def test_rolling_high_excludes_todays_bar(self):
        """A bar cannot break a level it is itself setting.

        Including today would give every new-high name a distance of 0%,
        so the most extended names would look like the closest to a
        breakout -- inverting the Breakout Ready scanner's whole purpose.
        """
        closes = np.concatenate([np.full(25, 10.0), [50.0]])
        daily = fx.daily_frame(closes)
        assert ind.rolling_high(daily, 20, exclude_current=True) == pytest.approx(
            10.0 * 1.005)
        assert ind.rolling_high(daily, 20, exclude_current=False) == pytest.approx(
            50.0 * 1.005)

    def test_rolling_high_requires_a_full_window(self):
        daily = fx.daily_frame(np.linspace(10, 11, 10))
        assert ind.rolling_high(daily, 20) is None

    def test_distance_pct_is_positive_when_below_the_level(self):
        assert ind.distance_pct(95, 100) == pytest.approx(5.0)
        assert ind.distance_pct(104, 100) == pytest.approx(-4.0)


class TestMinimumBars:
    def test_min_bars_for_hma_accounts_for_the_outer_smoothing(self):
        """HMA200's first value needs 200 + sqrt(200) - 1 bars.

        Getting this wrong yields a NaN HMA200 that every comparison
        evaluates False against -- which looks like a legitimate
        rejection and is not.
        """
        assert ind.min_bars_for_hma(200) == 200 + 14 - 1
        assert ind.min_bars_for_hma(89) == 89 + 9 - 1
        assert ind.min_bars_for_hma(1) == 1

    def test_hma200_is_actually_none_one_bar_short(self):
        needed = ind.min_bars_for_hma(200)
        short = fx.daily_frame(np.linspace(10, 40, needed - 1))
        assert ind.last_valid(ind.hma_series(short, 200)) is None
        exact = fx.daily_frame(np.linspace(10, 40, needed))
        assert ind.last_valid(ind.hma_series(exact, 200)) is not None


class TestFeaturePass:
    def test_builds_the_full_common_schema(self):
        features = build_features(fx.uptrend_bundle(volumes=fx.volume_surge()))
        schema = features.schema_fields()
        for field in ("hma89", "hma200", "hma200_slope", "adx", "volume",
                      "avg_volume", "volume_multiple", "price_change_pct",
                      "high_20d", "high_50d", "high_52w",
                      "extension_hma200_pct"):
            assert field in schema
            assert schema[field] is not None, field

    def test_volume_multiple_uses_a_denominator_that_excludes_today(self):
        """Otherwise the spike damps its own ratio."""
        features = build_features(fx.uptrend_bundle(volumes=fx.volume_surge(multiple=2.4)))
        assert features.volume_multiple == pytest.approx(2.4, rel=1e-6)

    def test_refuses_insufficient_history(self):
        bundle = SymbolData(symbol="NEW", daily=fx.daily_frame(np.linspace(10, 12, 50)))
        with pytest.raises(ScannerDataError, match="daily bars"):
            build_features(bundle)

    def test_refuses_stale_daily_bars(self):
        """Section 28's stale-data case. A scan must never judge a
        symbol from last month's prices and report it as today's."""
        import datetime as dt

        closes = fx.accelerating_uptrend()
        old = fx.daily_frame(closes, end=dt.date.today() - dt.timedelta(days=40))
        with pytest.raises(ScannerDataError, match="old"):
            build_features(SymbolData(symbol="STALE", daily=old))

    def test_intraday_absence_does_not_break_a_daily_feature_pass(self):
        """Routine for thinly traded names, and the three daily scanners
        must still be able to judge them."""
        features = build_features(fx.uptrend_bundle())
        assert features.intraday_bars == 0
        assert features.vwap is None
        assert features.hma200 is not None

    def test_require_intraday_raises_when_it_is_missing(self):
        with pytest.raises(ScannerDataError, match="intraday"):
            build_features(fx.uptrend_bundle(), require_intraday=True)

    def test_minimum_daily_bars_covers_hma200_plus_its_slope(self):
        assert minimum_daily_bars() >= ind.min_bars_for_hma(200)


class TestSessionSlicing:
    def test_slices_only_the_latest_regular_session(self):
        import datetime as dt

        yesterday = fx.intraday_frame(np.full(20, 50.0), day=dt.date(2026, 8, 10))
        today = fx.intraday_frame(np.full(20, 100.0), day=dt.date(2026, 8, 11))
        combined = pd.concat([yesterday, today])
        session = sess.slice_session(combined)
        assert len(session) == 20
        assert float(session["Close"].iloc[0]) == pytest.approx(100.0)

    def test_drops_premarket_bars_when_regular_only(self):
        """An opening range built from 04:00 bars is not an opening range."""
        import datetime as dt

        premarket = fx.intraday_frame(np.full(10, 90.0), day=dt.date(2026, 8, 11))
        premarket.index = premarket.index - pd.Timedelta(hours=4)
        regular = fx.intraday_frame(np.full(10, 100.0), day=dt.date(2026, 8, 11))
        combined = pd.concat([premarket, regular])
        assert len(sess.slice_session(combined, regular_only=True)) == 10
        assert len(sess.slice_session(combined, regular_only=False)) == 20

    def test_opening_range_measures_from_the_first_bar_not_from_0930(self):
        """A late open (halt, half-day) still gets a real range."""
        import datetime as dt

        late = fx.intraday_frame(np.linspace(100, 101, 40), day=dt.date(2026, 8, 11))
        late.index = late.index + pd.Timedelta(minutes=45)
        high, low, bars = sess.opening_range(late, 15)
        assert len(bars) == 15
        assert high is not None and low is not None

    def test_previous_daily_close_is_the_bar_before_the_session(self):
        """Not `iloc[-2]`: whether today's partial bar is present depends
        on the time of day, so a fixed offset picks the wrong close half
        the time."""
        daily = fx.daily_frame(np.linspace(10, 20, 30))
        session_day = daily.index[-1].date()
        assert sess.previous_daily_close(daily, before=session_day) == pytest.approx(
            float(daily["Close"].iloc[-2]))
        # A session on the newest bar's own date must not use that bar.
        assert sess.previous_daily_close(daily, before=session_day) != pytest.approx(
            float(daily["Close"].iloc[-1]))

    def test_naive_index_is_treated_as_one_session(self):
        """The unit-test fixtures use plain RangeIndex frames, and every
        branch of both intraday scanners has to stay reachable from them."""
        frame = pd.DataFrame({"Close": [1.0, 2.0], "High": [1.0, 2.0],
                              "Low": [1.0, 2.0], "Volume": [1.0, 1.0]})
        assert len(sess.slice_session(frame)) == 2
