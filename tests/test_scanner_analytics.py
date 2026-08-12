"""Forward returns, MFE/MAE, and the statistics built on them.

Sections 12, 13, 14, 15, 16, 17 and 22.

The tests that carry the most weight are the ones pinning the MFE/MAE
WINDOW. "Maximum favourable excursion over 1 day" has several plausible
readings and the wrong one is not obviously wrong -- it just quietly
credits a scanner with a move that happened before it spoke, or discards
the afternoon an intraday scanner was built to catch. Once a month of
data is collected under the wrong reading, there is no fixing it after
the fact.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_hours import EASTERN  # noqa: E402
from scanners.analytics import common, performance_tracker  # noqa: E402
from scanners.analytics.intersection_analysis import analyse, build_symbol_days  # noqa: E402
from scanners.base import result_store  # noqa: E402
from scanners.base.market_data_provider import StaticMarketDataProvider  # noqa: E402
from scanners.base.models import ScannerSignal  # noqa: E402
from tests import scanner_fixtures as fx  # noqa: E402

SIGNAL_DAY = "2026-08-12"
SIGNAL_TIME = "2026-08-12T14:00:00+00:00"  # 10:00 ET


def signal(**overrides):
    payload = dict(
        timestamp=SIGNAL_TIME,
        trading_day=SIGNAL_DAY,
        symbol="TEST",
        scanner_name="hma_early_trend",
        scanner_version="hma_early_trend_v1.0",
        scanner_score=80.0,
        signal_price=100.0,
    )
    payload.update(overrides)
    return ScannerSignal(**payload)


def forward(sessions, highs, lows, closes):
    return fx.forward_daily(SIGNAL_DAY, sessions=sessions, start_price=100.0,
                            highs=highs, lows=lows, closes=closes)


class TestForwardReturns:
    def test_multi_day_returns_use_the_close_of_the_nth_forward_session(self):
        daily = forward(5, highs=[101] * 5, lows=[99] * 5,
                        closes=[101, 102, 103, 104, 105])
        record = performance_tracker.compute_performance(signal(), daily=daily)
        assert record["return_1d"] == pytest.approx(1.0)
        assert record["return_3d"] == pytest.approx(3.0)
        assert record["return_5d"] == pytest.approx(5.0)

    def test_an_immature_horizon_is_null_not_zero(self):
        """A signal from yesterday has no 5-day return. Recording 0.0
        would drag every 5-day average toward zero by exactly the count
        of recent signals -- making the newest week of any report look
        worse than it was."""
        daily = forward(2, highs=[101, 102], lows=[99, 99], closes=[101, 102])
        record = performance_tracker.compute_performance(signal(), daily=daily)
        assert record["return_1d"] == pytest.approx(1.0)
        assert record["return_3d"] is None
        assert record["return_5d"] is None
        assert record["sessions_available"] == 2
        assert "return_1d" in record["horizons_complete"]
        assert "return_5d" not in record["horizons_complete"]

    def test_a_signal_with_no_price_is_refused_not_guessed(self):
        record = performance_tracker.compute_performance(signal(signal_price=None))
        assert record["error"]
        assert record["return_1d"] is None

    def test_intraday_horizons_are_measured_from_the_signal_timestamp(self):
        """30m/1h/2h come from the signal day's own minute bars."""
        closes = np.array([100.0 + index * 0.1 for index in range(180)])
        intraday = fx.intraday_frame(closes, day=datetime.fromisoformat(SIGNAL_DAY).date())
        # Session starts 09:30 ET; the signal is at 10:00 ET = bar 30.
        record = performance_tracker.compute_performance(
            signal(signal_price=float(closes[30])), intraday=intraday)
        assert record["includes_signal_day_intraday"] is True
        assert record["return_30m"] is not None
        assert record["return_1h"] is not None
        assert record["return_2h"] is not None
        assert record["return_30m"] < record["return_1h"] < record["return_2h"]

    def test_intraday_horizons_are_null_without_minute_bars(self):
        """After about a week the provider stops serving them; the record
        says which kind of measurement it got rather than pretending."""
        daily = forward(5, highs=[101] * 5, lows=[99] * 5, closes=[101] * 5)
        record = performance_tracker.compute_performance(signal(), daily=daily)
        assert record["includes_signal_day_intraday"] is False
        assert record["return_30m"] is None


class TestExcursionWindow:
    def test_the_window_excludes_the_signal_days_own_daily_bar(self):
        """The signal day's daily high includes the part of the session
        BEFORE the scanner spoke. Counting it would credit the scanner
        with a move it did not call."""
        daily = forward(1, highs=[103], lows=[98], closes=[100])
        # Prepend a signal-day bar with an enormous high.
        signal_day_bar = pd.DataFrame(
            {"Open": [100.0], "High": [500.0], "Low": [50.0], "Close": [100.0],
             "Volume": [1.0]},
            index=[pd.Timestamp(SIGNAL_DAY, tz=EASTERN)])
        combined = pd.concat([signal_day_bar, daily])
        record = performance_tracker.compute_performance(signal(), daily=combined)
        assert record["mfe_1d"] == pytest.approx(3.0)
        assert record["mae_1d"] == pytest.approx(-2.0)

    def test_the_window_includes_the_signal_days_bars_after_the_signal(self):
        """An intraday scanner's whole thesis plays out in the hours
        after its signal; starting at tomorrow's open would discard it."""
        closes = np.full(180, 100.0)
        highs = closes.copy()
        highs[60] = 108.0  # 10:30 ET, after the 10:00 signal
        lows = closes.copy()
        lows[10] = 80.0  # 09:40 ET, BEFORE the signal
        intraday = fx.intraday_frame(
            closes, highs=highs, lows=lows,
            day=datetime.fromisoformat(SIGNAL_DAY).date())
        daily = forward(1, highs=[101], lows=[99], closes=[100])

        record = performance_tracker.compute_performance(
            signal(), daily=daily, intraday=intraday)
        # The post-signal spike counts...
        assert record["mfe_1d"] == pytest.approx(8.0)
        # ...and the pre-signal low does not.
        assert record["mae_1d"] == pytest.approx(-1.0)

    def test_mfe_is_clamped_at_zero(self):
        """An excursion in the favourable direction that never went
        favourable is zero, not a negative favourable excursion -- which
        would flip the sign of section 16's MFE/MAE ratio."""
        daily = forward(1, highs=[95], lows=[90], closes=[92])
        record = performance_tracker.compute_performance(signal(), daily=daily)
        assert record["mfe_1d"] == 0.0
        assert record["mae_1d"] == pytest.approx(-10.0)

    def test_mae_is_clamped_at_zero_on_the_other_side(self):
        daily = forward(1, highs=[110], lows=[105], closes=[108])
        record = performance_tracker.compute_performance(signal(), daily=daily)
        assert record["mfe_1d"] == pytest.approx(10.0)
        assert record["mae_1d"] == 0.0

    def test_longer_horizons_are_supersets_of_shorter_ones(self):
        daily = forward(5, highs=[101, 102, 108, 103, 104],
                        lows=[99, 98, 92, 97, 96],
                        closes=[100] * 5)
        record = performance_tracker.compute_performance(signal(), daily=daily)
        assert record["mfe_1d"] <= record["mfe_3d"] <= record["mfe_5d"]
        assert record["mae_1d"] >= record["mae_3d"] >= record["mae_5d"]
        assert record["mfe_3d"] == pytest.approx(8.0)
        assert record["mae_3d"] == pytest.approx(-8.0)


class TestTrackerIntegration:
    def test_track_day_reads_signals_and_writes_performance(self):
        result_store.write_signals([signal()], trading_day=SIGNAL_DAY)
        daily = forward(5, highs=[105] * 5, lows=[98] * 5, closes=[103] * 5)
        provider = StaticMarketDataProvider(daily={"TEST": daily})

        records = performance_tracker.track_day(SIGNAL_DAY, provider=provider)
        assert len(records) == 1
        stored = result_store.read_performance(SIGNAL_DAY)
        assert stored[signal().signal_id]["return_5d"] == pytest.approx(3.0)

    def test_rerunning_supersedes_the_earlier_record(self):
        """The tracker runs repeatedly as horizons mature; the run with
        five sessions of bars must win over the one that had two."""
        result_store.write_signals([signal()], trading_day=SIGNAL_DAY)

        partial = forward(2, highs=[101] * 2, lows=[99] * 2, closes=[101] * 2)
        performance_tracker.track_day(
            SIGNAL_DAY, provider=StaticMarketDataProvider(daily={"TEST": partial}))
        assert result_store.read_performance(SIGNAL_DAY)[signal().signal_id]["return_5d"] is None

        full = forward(5, highs=[105] * 5, lows=[99] * 5, closes=[104] * 5)
        performance_tracker.track_day(
            SIGNAL_DAY, provider=StaticMarketDataProvider(daily={"TEST": full}))
        assert result_store.read_performance(SIGNAL_DAY)[signal().signal_id][
            "return_5d"] == pytest.approx(4.0)

    def test_one_failing_signal_does_not_stop_the_rest(self):
        good = signal(symbol="GOOD")
        bad = signal(symbol="BAD")
        daily = forward(5, highs=[105] * 5, lows=[99] * 5, closes=[103] * 5)
        provider = StaticMarketDataProvider(daily={"GOOD": daily})
        records = performance_tracker.track_signals([good, bad], provider=provider)
        assert len(records) == 2
        by_symbol = {record["symbol"]: record for record in records}
        assert by_symbol["GOOD"]["return_5d"] is not None

    def test_bars_are_fetched_once_per_symbol_across_scanners(self):
        """Section 6 keeps the same symbol under several scanners, so a
        ticker routinely appears three or four times in a day's signals."""
        calls = []

        class Counting(StaticMarketDataProvider):
            def get_daily_bars(self, symbol, lookback_days=400):
                calls.append(symbol)
                return super().get_daily_bars(symbol, lookback_days=lookback_days)

        from scanners.base.market_data_provider import CachingMarketDataProvider

        daily = forward(5, highs=[105] * 5, lows=[99] * 5, closes=[103] * 5)
        provider = CachingMarketDataProvider(Counting(daily={"TEST": daily}))
        performance_tracker.track_signals(
            [signal(scanner_name="hma_early_trend"),
             signal(scanner_name="accumulation"),
             signal(scanner_name="breakout_ready")],
            provider=provider)
        assert calls == ["TEST"]


class TestStatistics:
    def test_nulls_are_excluded_from_averages_not_counted_as_zero(self):
        rows = [{"return_5d": 10.0}, {"return_5d": None}, {"return_5d": 20.0}]
        assert common.mean(common.numbers(rows, "return_5d")) == pytest.approx(15.0)

    def test_every_statistic_reports_the_count_it_used(self):
        """An average over four signals and one over four hundred are not
        comparable, and month-2 calibration acting on the former without
        knowing it is exactly the overfitting section 20 warns against."""
        summary = common.summarise([{"return_5d": 5.0}, {"return_5d": None}])
        assert summary["return_5d_n"] == 1
        assert summary["signal_count"] == 2

    def test_positive_rate_treats_flat_as_not_a_win(self):
        assert common.positive_rate([1.0, 0.0, -1.0]) == pytest.approx(33.33, abs=0.01)

    def test_median_is_reported_alongside_the_mean(self):
        """These distributions have long right tails; the gap between the
        two is itself the reading."""
        rows = [{"return_5d": value} for value in [1.0, 1.0, 1.0, 1.0, 200.0]]
        summary = common.summarise(rows)
        assert summary["median_return_5d"] == pytest.approx(1.0)
        assert summary["avg_return_5d"] > 40

    def test_mfe_mae_ratio_uses_the_absolute_mae(self):
        summary = common.summarise([{"mfe_5d": 8.0, "mae_5d": -4.0}])
        assert summary["mfe_mae_ratio"] == pytest.approx(2.0)

    def test_mfe_mae_ratio_is_null_when_nothing_went_against(self):
        """Division by zero here would produce `inf`, which sorts and
        averages in ways that corrupt any table it lands in."""
        summary = common.summarise([{"mfe_5d": 8.0, "mae_5d": 0.0}])
        assert summary["mfe_mae_ratio"] is None

    def test_versions_are_grouped_separately(self):
        """Sections 11/19: rows either side of a parameter change came
        from two different experiments and must not be averaged."""
        rows = [
            {"scanner_name": "orb", "scanner_version": "orb_v1.0", "return_1d": 1.0},
            {"scanner_name": "orb", "scanner_version": "orb_v1.1", "return_1d": 9.0},
        ]
        assert len(common.group_by_scanner_version(rows)) == 2

    def test_best_and_worst_candidates_are_identifiable(self):
        rows = [
            {"symbol": "AAA", "return_1d": 9.0, "trading_day": SIGNAL_DAY},
            {"symbol": "BBB", "return_1d": -4.0, "trading_day": SIGNAL_DAY},
        ]
        summary = common.summarise(rows)
        assert summary["best_candidate"]["symbol"] == "AAA"
        assert summary["worst_candidate"]["symbol"] == "BBB"


class TestIntersections:
    def rows(self):
        return [
            {"trading_day": SIGNAL_DAY, "symbol": "NVDA", "scanner_name": "hma_early_trend",
             "return_5d": 10.0, "mfe_5d": 12.0, "mae_5d": -2.0, "signal_id": "a"},
            {"trading_day": SIGNAL_DAY, "symbol": "NVDA", "scanner_name": "accumulation",
             "return_5d": 8.0, "mfe_5d": 10.0, "mae_5d": -2.0, "signal_id": "b"},
            {"trading_day": SIGNAL_DAY, "symbol": "NVDA", "scanner_name": "breakout_ready",
             "return_5d": 9.0, "mfe_5d": 11.0, "mae_5d": -2.0, "signal_id": "c"},
            {"trading_day": SIGNAL_DAY, "symbol": "SOLO", "scanner_name": "hma_early_trend",
             "return_5d": -3.0, "mfe_5d": 1.0, "mae_5d": -6.0, "signal_id": "d"},
        ]

    def test_symbol_days_carry_the_agreeing_scanner_set(self):
        records = {record["symbol"]: record for record in build_symbol_days(self.rows())}
        assert records["NVDA"]["confirmation_count"] == 3
        assert records["NVDA"]["scanners"] == [
            "accumulation", "breakout_ready", "hma_early_trend"]
        assert records["SOLO"]["confirmation_count"] == 1

    def test_a_combination_averages_across_its_contributing_rows(self):
        """Three scanners priced the same symbol at three moments; taking
        one arbitrarily would make the statistics depend on sort order."""
        records = {record["symbol"]: record for record in build_symbol_days(self.rows())}
        assert records["NVDA"]["return_5d"] == pytest.approx(9.0)

    def test_combination_order_does_not_create_two_buckets(self):
        rows = self.rows()
        reversed_rows = list(reversed(rows))
        first = {item["combination"] for item in analyse(rows)["combinations"]}
        second = {item["combination"] for item in analyse(reversed_rows)["combinations"]}
        assert first == second

    def test_reports_statistics_per_confirmation_count(self):
        result = analyse(self.rows())
        counts = {item["confirmation_count"]: item
                  for item in result["by_confirmation_count"]}
        assert counts[3]["signal_count"] == 1
        assert counts[1]["signal_count"] == 1

    def test_states_that_agreement_is_not_an_entry_rule(self):
        """Sections 17/18 defer acting on this; a reader of the report
        must not mistake it for a rule already in force."""
        note = analyse(self.rows())["note"]
        assert "not an entry condition" in note
