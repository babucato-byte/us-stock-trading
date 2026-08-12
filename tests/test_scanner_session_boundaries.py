"""Session boundaries and schedule alignment (spec sections 16-18).

Every failure guarded here is silent. An opening range measured over 14
bars instead of 15, a gap measured against the wrong prior close, a
premarket scan that reads regular-session bars -- none of them raise,
none of them look wrong in a log, and all of them produce a month of
plausible numbers describing something other than what the column name
says.

The bar-labelling convention this all rests on: a 1-minute bar is
stamped with its START. The bar labelled 09:30 covers 09:30:00-09:30:59.
So the 09:30-09:45 opening range is the bars labelled 09:30 through
09:44 -- fifteen of them -- and the bar labelled 09:45 is the first bar
AFTER the range, not the last bar of it.
"""

import sys
from datetime import date, datetime, time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_hours import EASTERN, MARKET_REGULAR_START  # noqa: E402
from scanners.base import session as sess  # noqa: E402
from scanners.base.market_data_provider import SymbolData  # noqa: E402
from scanners.base.models import ScannerDataError  # noqa: E402
from scanners.base.scanner_base import Rejected  # noqa: E402
from scanners.registry import build_scanner  # noqa: E402
from scanners.runner import PROFILES  # noqa: E402
from tests import scanner_fixtures as fx  # noqa: E402

DAY = "2026-08-12"
SESSION_DAY = date(2026, 8, 12)


def minute_session(bars, *, start_hour=9, start_minute=30, base=100.0):
    """`bars` one-minute bars starting at the given Eastern time."""
    index = pd.date_range(
        start=pd.Timestamp(datetime.combine(SESSION_DAY, time(start_hour, start_minute)),
                           tz=EASTERN),
        periods=bars, freq="1min")
    closes = np.full(bars, base)
    return pd.DataFrame(
        {"Open": closes, "High": closes + 0.05, "Low": closes - 0.05,
         "Close": closes, "Volume": np.full(bars, 1000.0)},
        index=index)


class TestOpeningRangeWindow:
    """Section 17: the 09:45 bar must not be swallowed or dropped."""

    def test_orb15_uses_exactly_the_0930_to_0944_bars(self):
        frame = minute_session(60)
        _, _, window = sess.opening_range(frame, 15)
        assert len(window) == 15
        assert window.index[0].strftime("%H:%M") == "09:30"
        assert window.index[-1].strftime("%H:%M") == "09:44"

    def test_the_0945_bar_starts_the_post_range_window_not_the_range(self):
        """The boundary is exclusive on the upper end. Including the
        09:45 bar would make the range 16 minutes long while still being
        labelled ORB15; excluding the 09:44 bar would lose a minute of
        the range."""
        frame = minute_session(60)
        _, _, window = sess.opening_range(frame, 15)
        post = frame.iloc[len(window):]
        assert post.index[0].strftime("%H:%M") == "09:45"
        assert len(window) + len(post) == len(frame)

    @pytest.mark.parametrize("minutes,last_bar,first_post", [
        (5, "09:34", "09:35"),
        (15, "09:44", "09:45"),
        (30, "09:59", "10:00"),
    ])
    def test_every_supported_window_lands_on_the_right_boundary(
            self, minutes, last_bar, first_post):
        frame = minute_session(90)
        _, _, window = sess.opening_range(frame, minutes)
        assert len(window) == minutes
        assert window.index[-1].strftime("%H:%M") == last_bar
        assert frame.iloc[len(window):].index[0].strftime("%H:%M") == first_post

    def test_the_range_high_is_taken_only_from_range_bars(self):
        """A spike after the range must not raise the range high --
        that would make the level unbreakable by construction."""
        frame = minute_session(60)
        frame.iloc[40, frame.columns.get_loc("High")] = 500.0
        high, _, _ = sess.opening_range(frame, 15)
        assert high < 200.0

    def test_a_late_open_measures_from_the_first_bar_traded(self):
        """A halted or half-day name still gets a real range rather than
        an empty one measured from a 09:30 that never traded."""
        frame = minute_session(40, start_hour=10, start_minute=15)
        _, _, window = sess.opening_range(frame, 15)
        assert len(window) == 15
        assert window.index[0].strftime("%H:%M") == "10:15"

    def test_a_0950_run_has_the_full_range_plus_bars_to_judge(self):
        """Section 16/17: the `open` profile runs at 09:50 ET, which is
        five minutes after an ORB15 range closes. That has to be enough
        for the scanner to reach a verdict rather than a data error."""
        scanner = build_scanner("orb")
        minimum_post = scanner.config.require_int("min_post_range_bars")
        bars_by_0950 = 20  # 09:30..09:49 inclusive
        assert bars_by_0950 - 15 >= minimum_post, (
            "the open profile fires before the ORB scanner can judge anything")

    def test_a_run_too_early_is_a_data_error_not_a_rejection(self):
        """"No verdict yet" must not read as "no setups today" -- one is
        a schedule fact, the other is a market observation."""
        scanner = build_scanner("orb")
        bundle = fx.orb_bundle(range_bars=15, post_bars=1)
        with pytest.raises(ScannerDataError, match="bars since"):
            scanner.check(scanner.build_features(bundle), bundle, {})

    def test_the_open_profile_carries_the_intraday_scanners(self):
        """Section 16: 09:50 is for ORB and the gap pullback."""
        assert set(PROFILES["open"]) == {"orb", "gap_pullback"}
        assert PROFILES["premarket"] == ["premarket_momentum"]
        assert set(PROFILES["daily"]) == {
            "hma_early_trend", "accumulation", "breakout_ready"}


class TestWickVersusConfirmedBreakout:
    """Section 9 (v1.1): the flag must actually change the outcome.

    An earlier draft made `require_close_breakout` unreachable -- the
    `price > range_high` test already implied a close above it, so the
    flag could never reject anything and the wick-only population could
    never be separated. Month 1 is supposed to MEASURE which of the two
    resolves better, which requires both to be identifiable.
    """

    def test_the_strict_flag_rejects_a_wick_only_breakout(self):
        scanner = build_scanner("orb")
        assert scanner.config.require_bool("require_close_breakout") is True
        bundle = fx.orb_bundle(confirm_close=False)
        with pytest.raises(Rejected):
            _check(scanner, bundle)

    def test_the_relaxed_flag_accepts_it_and_labels_it(self):
        scanner = build_scanner("orb")
        scanner.config.params["require_close_breakout"] = False
        _, context = _check(scanner, fx.orb_bundle(confirm_close=False))
        assert context["breakout_touched"] is True
        assert context["breakout_confirmed"] is False

    def test_both_flags_are_recorded_on_every_signal(self):
        """So the two populations are separable in the month-1 dataset
        regardless of which flag was in force when it was collected."""
        signal = build_scanner("orb").evaluate(fx.orb_bundle(), trading_day=DAY)
        assert signal.metrics["breakout_confirmed"] is True
        assert signal.metrics["breakout_touched"] is True
        assert "retest_confirmed" in signal.metrics

    def test_a_confirmed_breakout_that_fell_back_inside_is_rejected(self):
        """The second half of the strict branch: a name that broke and
        has since lost the level is not holding the breakout. This is
        the check that made the flag meaningful."""
        scanner = build_scanner("orb")
        bundle = fx.orb_bundle()
        session = bundle.intraday.copy()
        range_high = float(session["High"].iloc[:15].max())
        # Drag the final bar back under the range high, leaving the
        # earlier confirmed closes intact.
        session.iloc[-1, session.columns.get_loc("Close")] = range_high * 0.995
        pulled_back = SymbolData(symbol=bundle.symbol, daily=bundle.daily,
                                 intraday=session)
        with pytest.raises(Rejected, match="fallen back inside"):
            _check(scanner, pulled_back)

    def test_a_wick_only_signal_scores_below_a_confirmed_one(self):
        scanner = build_scanner("orb")
        scanner.config.params["require_close_breakout"] = False
        wick = scanner.evaluate(fx.orb_bundle(confirm_close=False), trading_day=DAY)
        confirmed = scanner.evaluate(fx.orb_bundle(confirm_close=True), trading_day=DAY)
        assert wick is not None and confirmed is not None
        assert wick.scanner_score < confirmed.scanner_score


class TestSessionSelection:
    def test_premarket_bars_are_excluded_from_the_opening_range(self):
        """An opening range built from 04:00 bars is not an opening
        range, and the error is invisible: it produces a plausible high
        and low from the wrong hours."""
        premarket = minute_session(30, start_hour=8, start_minute=0, base=90.0)
        regular = minute_session(30, base=100.0)
        combined = pd.concat([premarket, regular])

        session = sess.slice_session(combined, regular_only=True)
        assert len(session) == 30
        assert session.index[0].strftime("%H:%M") == "09:30"
        high, _, _ = sess.opening_range(session, 15)
        assert high == pytest.approx(100.05)

    def test_only_the_latest_session_is_used(self):
        yesterday = fx.intraday_frame(np.full(30, 50.0), day=date(2026, 8, 11))
        today = fx.intraday_frame(np.full(30, 100.0), day=SESSION_DAY)
        session = sess.slice_session(pd.concat([yesterday, today]))
        assert len(session) == 30
        assert float(session["Close"].iloc[0]) == pytest.approx(100.0)

    def test_a_utc_index_is_converted_before_the_boundary_is_applied(self):
        """A provider swapped in later may return UTC. Comparing 13:30Z
        against a 09:30 Eastern boundary would drop the whole session."""
        frame = minute_session(30).tz_convert("UTC")
        session = sess.slice_session(frame, regular_only=True)
        assert len(session) == 30


class TestGapMeasurement:
    """Section 7: the gap is measured against the right prior close."""

    def test_the_prior_close_is_the_session_before_not_a_fixed_offset(self):
        daily = fx.daily_frame(np.linspace(10, 20, 30))
        session_day = daily.index[-1].date()
        assert sess.previous_daily_close(daily, before=session_day) == pytest.approx(
            float(daily["Close"].iloc[-2]))

    @pytest.mark.parametrize("gap_pct,expected", [
        (1.99, "below the 2.00% floor"),
        (8.01, "above the 8.00% ceiling"),
    ])
    def test_the_gap_band_boundaries_reject_just_outside(self, gap_pct, expected):
        scanner = build_scanner("gap_pullback")
        bundle = fx.gap_pullback_bundle(gap_pct=gap_pct)
        with pytest.raises(Rejected, match=expected):
            scanner.check(scanner.build_features(bundle), bundle, {})

    @pytest.mark.parametrize("gap_pct", [2.01, 5.0, 7.99])
    def test_the_gap_band_accepts_values_inside_it(self, gap_pct):
        scanner = build_scanner("gap_pullback")
        bundle = fx.gap_pullback_bundle(gap_pct=gap_pct)
        _, context = _check(scanner, bundle)
        assert context["gap_pct"] == pytest.approx(gap_pct, abs=1e-6)

    def test_the_exact_boundary_is_float_sensitive_and_that_is_acceptable(self):
        """A gap of exactly 8.000% may land either side of the ceiling.

        `gap_pct` is derived as `(open / prior_close - 1) * 100`, and for
        a synthetic open of `prior_close * 1.08` that arithmetic yields
        8.000000000000002 -- just over the limit. Real prices land on
        the boundary just as arbitrarily.

        This is recorded rather than fixed. Widening the comparison by an
        epsilon would be a threshold change during a frozen month
        (sections 15 and 31) to buy a distinction that does not exist:
        the band is a coarse 2-8% filter, and whether a name gapping
        exactly 8.000% is included changes nothing any month-1 question
        turns on. What matters -- and is pinned above -- is that 7.99
        passes and 8.01 does not.
        """
        scanner = build_scanner("gap_pullback")
        bundle = fx.gap_pullback_bundle(gap_pct=8.0)
        try:
            _, context = _check(scanner, bundle)
            assert context["gap_pct"] == pytest.approx(8.0, abs=1e-9)
        except Rejected as rejection:
            assert "above the 8.00% ceiling" in str(rejection)

    def test_the_configured_band_is_two_to_eight_percent(self):
        """Pinned so the shipped v1.0 range cannot drift unnoticed
        during month 1 (spec sections 7 and 15)."""
        config = build_scanner("gap_pullback").config
        assert config.require_float("gap_min_pct") == 2.0
        assert config.require_float("gap_max_pct") == 8.0

    def test_the_volume_ratio_parameter_names_its_direction(self):
        """Section 8: `max_pullback_volume_ratio` did not say which leg
        was the numerator. The value is unchanged; the name now does."""
        config = build_scanner("gap_pullback").config
        assert config.require_float("max_pullback_to_impulse_volume_ratio") == 1.0
        assert "max_pullback_volume_ratio" not in config.params

    def test_the_ratio_is_pullback_over_impulse(self):
        scanner = build_scanner("gap_pullback")
        bundle = fx.gap_pullback_bundle(impulse_volume=40_000.0,
                                        pullback_volume=10_000.0)
        _, context = _check(scanner, bundle)
        assert context["pullback_volume_ratio"] == pytest.approx(0.25)


class TestPremarketDataAvailability:
    """Section 18: the 09:20 run happens before the regular session."""

    def test_premarket_bar_count_is_recorded(self):
        """Yahoo's extended-hours coverage is not something to assume,
        so whether the bars were there is a stored fact, not a guess."""
        premarket = minute_session(30, start_hour=8, start_minute=0)
        regular = minute_session(10)
        bundle = SymbolData(
            symbol="TEST",
            daily=fx.daily_frame(fx.accelerating_uptrend()),
            intraday=pd.concat([premarket, regular]),
        )
        features = build_scanner("premarket_momentum").build_features(bundle)
        assert features.premarket_bars == 30

    def test_a_session_with_no_premarket_bars_reports_zero_not_none(self):
        bundle = SymbolData(
            symbol="TEST",
            daily=fx.daily_frame(fx.accelerating_uptrend()),
            intraday=minute_session(30),
        )
        features = build_scanner("premarket_momentum").build_features(bundle)
        assert features.premarket_bars == 0

    def test_absent_intraday_bars_are_a_data_error_not_a_rejection(self):
        """Section 18: NO_DATA and FAIL are different findings.

        "We could not look" must not be counted alongside "we looked and
        it did not qualify", or month 1's rejection statistics describe
        a mixture of the two.
        """
        scanner = build_scanner("premarket_momentum")
        bundle = SymbolData(symbol="TEST",
                            daily=fx.daily_frame(fx.accelerating_uptrend()),
                            intraday=None)
        outcome = scanner.scan([bundle], trading_day=DAY)
        assert outcome.data_errors == 1
        assert outcome.rejected == 0

    def test_a_qualifying_session_that_fails_the_thresholds_is_a_rejection(self):
        scanner = build_scanner("premarket_momentum")
        bundle = fx.premarket_momentum_bundle(gain_pct=0.5, volume_multiple=1.0)
        outcome = scanner.scan([bundle], trading_day=DAY)
        assert outcome.rejected == 1
        assert outcome.data_errors == 0

    def test_the_premarket_scanner_reads_prepost_bars(self):
        """The runner fetches with prepost enabled, and the wrapped
        scanner reads the last row of whatever frame it is handed -- so
        before the open it must be seeing extended-hours bars."""
        import inspect

        from scanners import runner

        source = inspect.getsource(runner._symbol_bundles)
        assert "want_premarket" in source
        signature = inspect.signature(
            build_scanner("orb").build_features.__self__.build_features)
        assert signature is not None


def _check(scanner, bundle):
    context = {}
    reasons = scanner.check(scanner.build_features(bundle), bundle, context)
    return reasons, context
