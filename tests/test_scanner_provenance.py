"""Data lineage, run identity and failure states (spec v1.1).

Sections 2-6, 12, 13, 14, 17, 22 and 23 of the finalization pass.

These are the tests that make the month-1 dataset interpretable a month
after it was written. Every one of them guards a fact that is trivially
recoverable today and permanently lost tomorrow: which vendor served the
bars, how fresh they were, which run produced a row, and whether a day
with no signals was a quiet market or a broken pipeline.
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_hours import EASTERN  # noqa: E402
from scanners import runner  # noqa: E402
from scanners.analytics import common, performance_tracker  # noqa: E402
from scanners.analytics import intersection_analysis as ia  # noqa: E402
from scanners.base import result_store, run_context  # noqa: E402
from scanners.base.market_data_provider import (  # noqa: E402
    AlpacaMarketDataProvider,
    BarMarketDataProvider,
    CachingMarketDataProvider,
    StaticMarketDataProvider,
    YahooFinanceMarketDataProvider,
    default_provider,
)
from scanners.base.models import ScannerSignal  # noqa: E402
from scanners.registry import ALL_SCANNERS, build_scanner  # noqa: E402
from tests import scanner_fixtures as fx  # noqa: E402

DAY = "2026-08-12"


@pytest.fixture
def provider():
    accumulating = fx.uptrend_bundle("TEST", volumes=fx.volume_surge())
    orb = fx.orb_bundle("TEST")
    return StaticMarketDataProvider(
        daily={"TEST": accumulating.daily},
        intraday={"TEST": orb.intraday},
    )


class TestProviderNaming:
    """Section 2/3: the class is named for what it actually calls."""

    def test_the_implementation_is_named_for_yahoo_finance(self):
        assert YahooFinanceMarketDataProvider.provider_name == "yfinance"

    def test_the_alpaca_name_survives_as_a_deprecated_alias(self):
        """Section 3: a rename must not break existing importers."""
        assert AlpacaMarketDataProvider is YahooFinanceMarketDataProvider

    def test_the_alias_still_records_the_truthful_vendor(self):
        """The point of the alias is compatibility, not a second
        identity. Code using the old name must not stamp "alpaca" onto a
        signal built from Yahoo bars."""
        assert AlpacaMarketDataProvider().provider_name == "yfinance"

    def test_feed_is_null_rather_than_invented(self):
        """Section 4: never guess a feed name that was not observed.
        Yahoo Finance does not report one."""
        assert YahooFinanceMarketDataProvider.feed_name is None

    def test_the_cache_wrapper_forwards_provider_identity(self):
        """`default_provider()` wraps every real run in the cache, so a
        wrapper that reported its own name would mean production never
        recorded a usable vendor."""
        cached = CachingMarketDataProvider(YahooFinanceMarketDataProvider())
        assert cached.provider_name == "yfinance"
        assert default_provider().provider_name == "yfinance"

    def test_the_base_class_default_is_not_usable_as_an_identity(self):
        """A subclass that forgets to override must be visible as such,
        not silently recorded as a real vendor."""
        assert BarMarketDataProvider.provider_name == "base"

    def test_no_scanner_module_actually_uses_the_deprecated_name(self):
        """Section 3: new code must not use the alias.

        Checked on the syntax tree, not on the text. Two places legally
        mention the name and neither is a use: the provider module
        defines it, and `scanners/base/__init__.py` re-exports it so the
        compatibility promise holds. What must not exist anywhere is a
        CALL to it -- i.e. a scanner actually constructing its data
        source under the wrong vendor's name.
        """
        import ast

        offenders = []
        for path in sorted((REPO_ROOT / "scanners").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    if node.func.id in ("AlpacaMarketDataProvider",
                                        "YFinanceMarketDataProvider"):
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
        assert offenders == [], offenders

    def test_the_default_provider_constructs_the_correctly_named_class(self):
        assert type(default_provider(cached=False)) is YahooFinanceMarketDataProvider


class TestSignalProvenance:
    """Section 4/6/19: every signal carries its own lineage."""

    def test_a_stored_signal_records_provider_timestamps_run_and_timeframe(self, provider):
        report = runner.run_scanners(symbols=["TEST"], provider=provider,
                                     trading_day=DAY, profile="daily")
        stored = result_store.read_signals(DAY)
        assert stored
        for signal in stored:
            assert signal.market_data_provider == "static"
            assert signal.market_data_feed is None
            assert signal.scanner_run_id == report.run_id
            assert signal.source_timeframe in ("1d", "1m")
            assert signal.data_timestamp, signal.scanner_name
            assert signal.feature_timestamp, signal.scanner_name

    def test_timestamps_are_offset_aware(self):
        """A bare 09:45 in the dataset is unreadable a month later:
        inside the session or hours outside it, depending on a zone
        nobody wrote down."""
        signal = build_scanner("hma_early_trend").evaluate(
            fx.uptrend_bundle(), trading_day=DAY)
        assert datetime.fromisoformat(signal.data_timestamp).tzinfo is not None
        assert datetime.fromisoformat(signal.feature_timestamp).tzinfo is not None

    def test_data_timestamp_matches_the_scanners_own_timeframe(self):
        """Section 6: a daily scanner reporting its newest MINUTE bar
        would claim a freshness it never used, and vice versa."""
        bundle = fx.orb_bundle("TEST")
        daily_signal = build_scanner("hma_early_trend").evaluate(
            bundle, trading_day=DAY)
        intraday_signal = build_scanner("orb").evaluate(bundle, trading_day=DAY)

        assert daily_signal.source_timeframe == "1d"
        assert intraday_signal.source_timeframe == "1m"
        assert daily_signal.data_timestamp == fx.newest_iso(bundle.daily)
        assert intraday_signal.data_timestamp == fx.newest_iso(bundle.intraday)
        assert daily_signal.data_timestamp != intraday_signal.data_timestamp

    def test_the_two_timestamps_measure_different_things(self):
        """Section 6: `data_timestamp` comes from the BARS,
        `feature_timestamp` from the CLOCK. The gap between them is what
        makes a stale-data run visible, so they must not be the same
        value read twice.

        The ordering between them is deliberately NOT asserted: daily
        bars are stamped at their session's start, so a scan running at
        13:00 ET against a bar stamped 00:00 ET has feature > data,
        while a bar stamped for a session that has not opened yet has
        data > feature. Both are legitimate.
        """
        bundle = fx.uptrend_bundle()
        before = datetime.now(timezone.utc)
        signal = build_scanner("hma_early_trend").evaluate(bundle, trading_day=DAY)
        after = datetime.now(timezone.utc)

        assert signal.data_timestamp == fx.newest_iso(bundle.daily)
        feature_at = datetime.fromisoformat(signal.feature_timestamp)
        assert before <= feature_at <= after, "feature_timestamp is not the clock"
        assert signal.feature_timestamp != signal.data_timestamp

    def test_every_scanner_declares_a_timeframe(self):
        for name in ALL_SCANNERS:
            assert build_scanner(name).source_timeframe in ("1d", "1m"), name

    def test_premarket_bar_availability_is_recorded_not_assumed(self):
        """Section 18: Yahoo's extended-hours coverage is not
        guaranteed, so whether premarket bars were actually there is a
        stored fact."""
        signal = build_scanner("orb").evaluate(fx.orb_bundle(), trading_day=DAY)
        assert "premarket_bars" in signal.metrics


class TestRunIdentity:
    """Section 5."""

    def test_every_signal_from_one_run_shares_the_run_id(self, provider):
        report = runner.run_scanners(symbols=["TEST"], provider=provider,
                                     trading_day=DAY, profile="daily")
        ids = {signal.scanner_run_id
               for outcome in report.outcomes for signal in outcome.signals}
        assert len(ids) == 1
        assert ids == {report.run_id}

    def test_a_re_run_gets_a_new_id(self):
        """Section 5: re-runs must not reuse an id -- a failed run and
        its retry have to stay distinguishable in the stored data."""
        first = run_context.new_run_id(DAY, "daily")
        second = run_context.new_run_id(DAY, "daily")
        assert first != second

    def test_the_id_carries_the_day_and_profile(self):
        assert run_context.new_run_id("2026-08-12", "premarket").startswith(
            "20260812_PREMARKET_")

    def test_an_adhoc_run_is_labelled_rather_than_left_blank(self):
        assert "ADHOC" in run_context.new_run_id("2026-08-12", None)

    def test_the_run_id_exists_even_when_startup_fails(self, monkeypatch, provider):
        """A run that dies before scanning still needs an identity in
        the run log, or the failure has nothing to be recorded against."""
        monkeypatch.setenv("SCANNER_UNIVERSE_FILE", "/nonexistent/universe.csv")
        report = runner.run_scanners(provider=provider, trading_day=DAY,
                                     profile="daily")
        assert report.run_id
        assert result_store.read_run_manifests(DAY)[0]["run_id"] == report.run_id


class TestRunStatusVersusZeroCandidates:
    """Section 14 -- the distinction the whole run log exists for."""

    def test_a_completed_scan_with_no_signals_is_success_with_zero(self):
        """A quiet market. `candidate_count` is a real measurement."""
        empty = StaticMarketDataProvider(
            daily={"FLAT": fx.daily_frame(np.full(320, 50.0))})
        report = runner.run_scanners(symbols=["FLAT"], provider=empty,
                                     trading_day=DAY, profile="daily")
        assert report.status == run_context.SUCCESS
        assert report.candidate_count == 0
        assert report.signal_count == 0

    def test_a_provider_outage_is_failed_with_a_null_count(self):
        """Reporting 0 here would assert "the scanners looked and found
        nothing", which is a claim only a completed scan may make."""
        class Dead(StaticMarketDataProvider):
            def get_daily_bars(self, symbol, lookback_days=400):
                raise ValueError("upstream down")

        report = runner.run_scanners(symbols=["A", "B", "C"], provider=Dead(),
                                     trading_day=DAY, profile="daily")
        assert report.fetch_failures == 3
        assert report.status == run_context.FAILED_PROVIDER
        assert report.candidate_count is None

    def test_the_two_are_not_the_same_record(self):
        """The property in one line: same signal count, different status
        and a different candidate_count."""
        empty = StaticMarketDataProvider(
            daily={"FLAT": fx.daily_frame(np.full(320, 50.0))})
        quiet = runner.run_scanners(symbols=["FLAT"], provider=empty,
                                    trading_day=DAY, store=False)

        class Dead(StaticMarketDataProvider):
            def get_daily_bars(self, symbol, lookback_days=400):
                raise ValueError("upstream down")

        broken = runner.run_scanners(symbols=["FLAT"], provider=Dead(),
                                     trading_day=DAY, store=False)

        assert quiet.signal_count == broken.signal_count == 0
        assert quiet.status != broken.status
        assert quiet.candidate_count == 0
        assert broken.candidate_count is None

    def test_no_scanner_could_be_built_is_its_own_status(self, monkeypatch, provider):
        from scanners import registry

        for name in list(registry.SCANNER_SPECS):
            monkeypatch.setitem(registry.SCANNER_SPECS, name,
                                ("scanners.does_not_exist", "Nope"))
        report = runner.run_scanners(symbols=["TEST"], provider=provider,
                                     trading_day=DAY)
        assert report.status == run_context.FAILED_NO_SCANNER
        assert report.candidate_count is None

    def test_a_missing_universe_is_its_own_status(self, monkeypatch, provider):
        monkeypatch.setenv("SCANNER_UNIVERSE_FILE", "/nonexistent/universe.csv")
        report = runner.run_scanners(provider=provider, trading_day=DAY)
        assert report.status == run_context.FAILED_NO_UNIVERSE
        assert report.candidate_count is None

    def test_a_partial_run_is_neither_success_nor_failure(self, provider, monkeypatch):
        from scanners import registry

        monkeypatch.setitem(registry.SCANNER_SPECS, "orb",
                            ("scanners.orb.does_not_exist", "Nope"))
        report = runner.run_scanners(symbols=["TEST"], provider=provider,
                                     trading_day=DAY)
        assert report.status == run_context.PARTIAL

    def test_a_closed_market_is_recorded_not_merely_printed(self, monkeypatch):
        """Otherwise a missing day in the signal files is ambiguous
        between "holiday" and "the cron job did not fire"."""
        import market_guard

        monkeypatch.setattr(market_guard, "is_us_trading_day", lambda *a, **k: False)
        assert runner.main(["--profile", "daily", "--trading-day", DAY]) == 0
        manifests = result_store.read_run_manifests(DAY)
        assert manifests
        assert manifests[-1]["run_status"] == run_context.SKIPPED_MARKET_CLOSED
        assert manifests[-1]["candidate_count"] is None

    def test_the_manifest_records_provider_and_status(self, provider):
        runner.run_scanners(symbols=["TEST"], provider=provider,
                            trading_day=DAY, profile="daily")
        manifest = result_store.read_run_manifests(DAY)[0]
        assert manifest["run_status"] == run_context.SUCCESS
        assert manifest["market_data_provider"] == "static"
        assert manifest["profile"] == "daily"
        assert manifest["candidate_count"] == manifest["stored_signals"]


class TestCircuitBreakerState:
    """Section 13: the breaker's state is data, not just a log line."""

    def test_the_manifest_records_breaker_fields(self, provider):
        runner.run_scanners(symbols=["TEST"], provider=provider,
                            trading_day=DAY, profile="daily")
        manifest = result_store.read_run_manifests(DAY)[0]
        for field in ("provider_error_count", "consecutive_error_peak",
                      "circuit_breaker_triggered", "circuit_breaker_reason"):
            assert field in manifest, field
        assert manifest["circuit_breaker_triggered"] is False

    def test_a_tripped_breaker_is_recorded_with_its_reason(self, provider, monkeypatch):
        from scanners.accumulation.scanner import AccumulationScanner

        monkeypatch.setattr(AccumulationScanner, "evaluate",
                            lambda self, data, **kwargs: (_ for _ in ()).throw(
                                RuntimeError("synthetic")))
        monkeypatch.setattr(runner, "MAX_CONSECUTIVE_SCANNER_ERRORS", 3)
        symbols = [f"S{index}" for index in range(5)]
        wide = StaticMarketDataProvider(
            daily={symbol: provider._daily["TEST"] for symbol in symbols},
            intraday={symbol: provider._intraday["TEST"] for symbol in symbols},
        )
        runner.run_scanners(symbols=symbols, provider=wide, trading_day=DAY,
                            profile="daily")

        manifest = result_store.read_run_manifests(DAY)[0]
        assert manifest["circuit_breaker_triggered"] is True
        assert "consecutive symbol failures" in manifest["circuit_breaker_reason"]
        assert manifest["consecutive_error_peak"] == 3

        broken = [item for item in manifest["scanners"]
                  if item["scanner_name"] == "accumulation"][0]
        assert broken["status"] == run_context.FAILED
        assert broken["candidate_count"] is None
        assert broken["circuit_breaker_triggered"] is True

    def test_a_healthy_scanner_reports_a_real_count_alongside_a_broken_one(
            self, provider, monkeypatch):
        from scanners.accumulation.scanner import AccumulationScanner

        monkeypatch.setattr(AccumulationScanner, "evaluate",
                            lambda self, data, **kwargs: (_ for _ in ()).throw(
                                RuntimeError("synthetic")))
        report = runner.run_scanners(symbols=["TEST"], provider=provider,
                                     trading_day=DAY, profile="daily")
        by_name = {outcome.scanner_name: outcome for outcome in report.outcomes}
        assert by_name["hma_early_trend"].candidate_count is not None
        assert by_name["accumulation"].candidate_count == 0


class TestExperimentGrouping:
    """Section 11/12: what may and may not be averaged together."""

    def rows(self, **overrides):
        base = {"scanner_name": "hma_early_trend", "scanner_version": "hma_early_trend_v1.0",
                "metric_config_fingerprint": "aaa111", "market_data_provider": "yfinance"}
        base.update(overrides)
        return base

    def test_the_key_is_scanner_version_fingerprint_and_provider(self):
        assert common.EXPERIMENT_KEY_FIELDS == (
            "scanner_name", "scanner_version", "metric_config_fingerprint",
            "market_data_provider")

    def test_identical_rows_form_one_experiment(self):
        assert len(common.group_by_experiment([self.rows(), self.rows()])) == 1

    def test_a_different_provider_is_a_different_experiment(self):
        """Section 12: bar timestamps, adjustment policy and extended-
        hours coverage all differ between vendors."""
        grouped = common.group_by_experiment(
            [self.rows(), self.rows(market_data_provider="alpaca")])
        assert len(grouped) == 2

    def test_a_different_fingerprint_is_a_different_experiment(self):
        grouped = common.group_by_experiment(
            [self.rows(), self.rows(metric_config_fingerprint="bbb222")])
        assert len(grouped) == 2

    def test_a_different_version_is_a_different_experiment(self):
        grouped = common.group_by_experiment(
            [self.rows(), self.rows(scanner_version="hma_early_trend_v1.1")])
        assert len(grouped) == 2

    def test_a_clean_set_produces_no_split_warning(self):
        assert common.split_experiments([self.rows(), self.rows()]) == []

    def test_a_provider_change_is_reported_with_its_cause(self):
        finding = common.split_experiments(
            [self.rows(), self.rows(market_data_provider="alpaca")])[0]
        assert finding["causes"] == ["market_data_provider"]
        assert finding["market_data_providers"] == ["alpaca", "yfinance"]
        assert "market data provider changed" in common.format_split_warning(finding)

    def test_both_causes_are_reported_together(self):
        finding = common.split_experiments([
            self.rows(),
            self.rows(market_data_provider="alpaca"),
            self.rows(metric_config_fingerprint="bbb222"),
        ])[0]
        assert set(finding["causes"]) == {"config_fingerprint", "market_data_provider"}
        assert finding["experiment_count"] == 3

    def test_different_scanners_are_not_reported_as_a_split(self):
        findings = common.split_experiments([
            self.rows(),
            self.rows(scanner_name="accumulation",
                      scanner_version="accumulation_v1.0"),
        ])
        assert findings == []


class TestIntersectionScopes:
    """Section 22: same-run agreement is a stronger claim than same-day."""

    def rows(self):
        return [
            {"trading_day": DAY, "symbol": "NVDA", "scanner_name": "orb",
             "scanner_run_id": "R1", "return_5d": 9.0, "signal_id": "a"},
            {"trading_day": DAY, "symbol": "NVDA", "scanner_name": "gap_pullback",
             "scanner_run_id": "R1", "return_5d": 8.0, "signal_id": "b"},
            # Same symbol, same day, but a DIFFERENT run half an hour later.
            {"trading_day": DAY, "symbol": "NVDA", "scanner_name": "premarket_momentum",
             "scanner_run_id": "R2", "return_5d": 7.0, "signal_id": "c"},
        ]

    def test_day_scope_counts_all_three_as_agreeing(self):
        record = ia.build_symbol_days(self.rows(), scope=ia.BY_DAY)[0]
        assert record["confirmation_count"] == 3

    def test_run_scope_separates_the_two_snapshots(self):
        """The two scanners that judged the same bars at the same instant
        are genuine agreement; the third saw a different half-hour."""
        records = {record["scanner_run_id"]: record
                   for record in ia.build_symbol_days(self.rows(), scope=ia.BY_RUN)}
        assert records["R1"]["confirmation_count"] == 2
        assert records["R2"]["confirmation_count"] == 1

    def test_rows_without_a_run_id_are_dropped_from_run_scope(self):
        """Bucketing them together would invent agreement between
        signals sharing nothing but a missing field."""
        rows = self.rows()
        rows[0].pop("scanner_run_id")
        records = ia.build_symbol_days(rows, scope=ia.BY_RUN)
        assert sum(record["confirmation_count"] for record in records) == 2

    def test_the_scope_is_stated_in_the_result_and_the_report(self):
        result = ia.analyse(self.rows(), scope=ia.BY_RUN)
        assert result["scope"] == ia.BY_RUN
        assert "symbol-runs" in ia.format_report(result)

    def test_an_unknown_scope_is_refused(self):
        with pytest.raises(ValueError, match="unknown intersection scope"):
            ia.build_symbol_days(self.rows(), scope="whenever")


class TestPerformanceRecordsAreNeverBlanked:
    """Section 23 -- the bug this suite exists to prevent recurring."""

    def signal(self):
        return ScannerSignal(
            timestamp=f"{DAY}T14:00:00+00:00", trading_day=DAY, symbol="TEST",
            scanner_name="orb", scanner_version="orb_v1.0",
            scanner_score=80.0, signal_price=100.0)

    def test_a_later_null_does_not_overwrite_a_measured_value(self):
        """The tracker re-walks recent days daily. Minute bars expire
        after about a week, so the day-8 run computes `return_30m=None`.
        Under plain last-write-wins that null replaced the correct value
        computed on day 0 -- the intraday columns would fill in, then
        silently empty out again, and a month-end report would show them
        missing everywhere except the last week."""
        signal_id = self.signal().signal_id
        result_store.write_performance(
            [{"signal_id": signal_id, "return_30m": 1.5, "return_5d": None}],
            trading_day=DAY)
        result_store.write_performance(
            [{"signal_id": signal_id, "return_30m": None, "return_5d": 4.0}],
            trading_day=DAY)
        merged = result_store.read_performance(DAY)[signal_id]
        assert merged["return_30m"] == pytest.approx(1.5), "measured value was blanked"
        assert merged["return_5d"] == pytest.approx(4.0), "new value did not land"

    def test_a_later_non_null_still_corrects_an_earlier_one(self):
        signal_id = self.signal().signal_id
        result_store.write_performance(
            [{"signal_id": signal_id, "return_1d": 1.0}], trading_day=DAY)
        result_store.write_performance(
            [{"signal_id": signal_id, "return_1d": 2.0}], trading_day=DAY)
        assert result_store.read_performance(DAY)[signal_id]["return_1d"] == 2.0

    def test_bookkeeping_fields_do_follow_the_latest_run(self):
        """`horizon_status` describes the newest attempt, not the
        measurement, so it must be replaceable in both directions."""
        signal_id = self.signal().signal_id
        result_store.write_performance(
            [{"signal_id": signal_id, "status": "pending",
              "horizon_status": {"return_5d": "pending"}}], trading_day=DAY)
        result_store.write_performance(
            [{"signal_id": signal_id, "status": "complete",
              "horizon_status": {"return_5d": "complete"}}], trading_day=DAY)
        merged = result_store.read_performance(DAY)[signal_id]
        assert merged["status"] == "complete"
        assert merged["horizon_status"] == {"return_5d": "complete"}

    def test_the_daily_re_track_converges_rather_than_oscillating(self):
        """End-to-end version: track with intraday bars, then track again
        after they have expired, and the intraday returns must survive."""
        signal = self.signal()
        result_store.write_signals([signal], trading_day=DAY)

        closes = np.array([100.0 + index * 0.1 for index in range(180)])
        intraday = fx.intraday_frame(closes, day=date.fromisoformat(DAY))
        daily = fx.forward_daily(DAY, sessions=5, start_price=100.0,
                                 highs=[105] * 5, lows=[98] * 5, closes=[103] * 5)

        fresh = StaticMarketDataProvider(daily={"TEST": daily},
                                         intraday={"TEST": intraday})
        performance_tracker.track_day(DAY, provider=fresh)
        first = result_store.read_performance(DAY)[signal.signal_id]
        assert first["return_30m"] is not None

        # A week later: the provider no longer serves that day's minutes.
        expired = StaticMarketDataProvider(daily={"TEST": daily})
        performance_tracker.track_day(DAY, provider=expired)
        second = result_store.read_performance(DAY)[signal.signal_id]
        assert second["return_30m"] == pytest.approx(first["return_30m"])
        assert second["return_5d"] is not None


class TestHorizonStatus:
    """Section 23's vocabulary."""

    def signal(self, **overrides):
        payload = dict(
            timestamp=f"{DAY}T14:00:00+00:00", trading_day=DAY, symbol="TEST",
            scanner_name="orb", scanner_version="orb_v1.0",
            scanner_score=80.0, signal_price=100.0)
        payload.update(overrides)
        return ScannerSignal(**payload)

    def test_an_unelapsed_multi_day_horizon_is_pending_not_expired(self):
        """Daily bars do not age out; a 5-day return that has not filled
        WILL fill, and calling it expired would tell an operator to stop
        waiting for data that is still coming."""
        daily = fx.forward_daily(DAY, sessions=1, start_price=100.0,
                                 highs=[101], lows=[99], closes=[101])
        record = performance_tracker.compute_performance(self.signal(), daily=daily)
        assert record["horizon_status"]["return_1d"] == performance_tracker.COMPLETE
        assert record["horizon_status"]["return_5d"] == performance_tracker.PENDING
        assert record["status"] == performance_tracker.PARTIAL

    def test_missing_recent_minute_bars_are_data_unavailable_not_expired(self):
        """Within the retention window this may succeed on a retry --
        a different operator action from "gone for good"."""
        now = datetime.fromisoformat(f"{DAY}T20:00:00+00:00")
        record = performance_tracker.compute_performance(
            self.signal(), daily=None, intraday=None, now=now)
        assert record["horizon_status"]["return_30m"] == (
            performance_tracker.DATA_UNAVAILABLE)

    def test_old_missing_minute_bars_are_expired(self):
        """Past the provider's retention window the value can never be
        computed, and the row will stay empty forever."""
        now = datetime.fromisoformat(f"{DAY}T20:00:00+00:00") + timedelta(days=30)
        record = performance_tracker.compute_performance(
            self.signal(), daily=None, intraday=None, now=now)
        assert record["horizon_status"]["return_30m"] == performance_tracker.EXPIRED

    def test_everything_measured_rolls_up_to_complete(self):
        closes = np.array([100.0 + index * 0.1 for index in range(200)])
        intraday = fx.intraday_frame(closes, day=date.fromisoformat(DAY))
        daily = fx.forward_daily(DAY, sessions=5, start_price=100.0,
                                 highs=[105] * 5, lows=[98] * 5, closes=[103] * 5)
        record = performance_tracker.compute_performance(
            self.signal(), daily=daily, intraday=intraday,
            now=datetime.now(timezone.utc))
        assert record["status"] == performance_tracker.COMPLETE

    def test_the_status_vocabulary_is_exactly_the_five_specified(self):
        assert {performance_tracker.PENDING, performance_tracker.PARTIAL,
                performance_tracker.COMPLETE, performance_tracker.EXPIRED,
                performance_tracker.DATA_UNAVAILABLE} == {
            "pending", "partial", "complete", "expired", "data_unavailable"}
