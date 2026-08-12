"""The framework: schema, config, store, and per-symbol isolation.

These are the properties the whole month-1 exercise rests on. If a
signal's schema can carry a NaN, or the store can lose a row, or one bad
symbol can end a scan, then the dataset the month-end comparison is
built from is not trustworthy no matter how good the scanners are.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scanners.base import result_store  # noqa: E402
from scanners.base.config import (  # noqa: E402
    ScannerConfig,
    ScannerConfigError,
    fingerprint_params,
    load_config,
)
from scanners.base.market_data_provider import (  # noqa: E402
    CachingMarketDataProvider,
    MarketDataUnavailable,
    StaticMarketDataProvider,
    SymbolData,
)
from scanners.base.models import ScannerDataError, ScannerSignal  # noqa: E402
from scanners.base.scanner_base import BaseScanner, Rejected  # noqa: E402
from scanners.registry import ALL_SCANNERS, build_scanner, build_scanners  # noqa: E402
from tests import scanner_fixtures as fx  # noqa: E402


def make_signal(**overrides):
    payload = dict(
        timestamp="2026-08-12T13:45:00+00:00",
        trading_day="2026-08-12",
        symbol="test",
        scanner_name="hma_early_trend",
        scanner_version="hma_early_trend_v1.0",
        scanner_score=71.5,
        signal_price=100.0,
    )
    payload.update(overrides)
    return ScannerSignal(**payload)


class TestSignalSchema:
    def test_nan_and_inf_become_null_not_a_number(self):
        """`NaN` is not valid JSON and reads differently in different
        languages. Every numeric field is cleaned at the boundary so the
        month-end dataset cannot contain one."""
        signal = make_signal(adx=float("nan"), volume_multiple=float("inf"),
                             hma200=float("-inf"))
        assert signal.adx is None
        assert signal.volume_multiple is None
        assert signal.hma200 is None

    def test_metrics_are_cleaned_too(self):
        signal = make_signal(metrics={"gap_pct": float("nan"), "ok": 2.5, "flag": True})
        assert signal.metrics["gap_pct"] is None
        assert signal.metrics["ok"] == pytest.approx(2.5)
        assert signal.metrics["flag"] is True

    def test_symbol_is_normalised(self):
        assert make_signal(symbol=" nvda ").symbol == "NVDA"

    def test_signal_is_immutable(self):
        """`signal_price` anchors every forward return; nothing
        downstream may rewrite the number its own scorecard is computed
        from."""
        signal = make_signal()
        with pytest.raises(Exception):
            signal.signal_price = 999.0

    def test_signal_id_is_stable_and_content_derived(self):
        assert make_signal().signal_id == make_signal().signal_id

    def test_signal_id_separates_scanners_for_the_same_symbol(self):
        """Section 6 keeps duplicates deliberately: the same symbol under
        two scanners must be two rows, not one."""
        first = make_signal(scanner_name="hma_early_trend")
        second = make_signal(scanner_name="accumulation")
        assert first.signal_id != second.signal_id

    def test_signal_id_collides_for_a_repeated_identical_scan(self):
        """A retried cron job must not double-count a symbol."""
        assert make_signal(scanner_score=10).signal_id == make_signal(
            scanner_score=90).signal_id

    def test_round_trips_through_a_dict(self):
        signal = make_signal(metrics={"gap_pct": 3.2}, reasons=["a", "b"])
        restored = ScannerSignal.from_dict(signal.to_dict())
        assert restored.signal_id == signal.signal_id
        assert restored.metrics["gap_pct"] == pytest.approx(3.2)
        assert restored.reasons == ["a", "b"]

    def test_flat_dict_prefixes_metrics_for_csv(self):
        flat = make_signal(metrics={"gap_pct": 3.2}).to_flat_dict()
        assert flat["metric_gap_pct"] == pytest.approx(3.2)
        assert "metrics" not in flat


class TestConfig:
    def test_every_shipped_scanner_config_loads_and_is_versioned(self):
        for name in ALL_SCANNERS:
            config = load_config(name)
            assert config.version, name
            assert config.params, name
            # Section 19: the version has to identify the parameter set.
            assert name.split("_")[0] in config.version, name

    def test_fingerprint_ignores_key_order_and_whitespace(self):
        assert fingerprint_params({"a": 1, "b": 2}) == fingerprint_params({"b": 2, "a": 1})

    def test_fingerprint_changes_when_a_value_changes(self):
        """This is what catches a parameter edit made without a version
        bump -- the failure section 11 depends on being visible."""
        assert fingerprint_params({"adx_min": 20}) != fingerprint_params({"adx_min": 25})

    def test_require_raises_rather_than_defaulting(self):
        """A code-side default is a hard-coded parameter that looks like
        a configured one (section 19)."""
        config = ScannerConfig(scanner_name="x", version="x_v1", params={})
        with pytest.raises(ScannerConfigError, match="adx_min"):
            config.require("adx_min")

    def test_require_float_rejects_a_non_number(self):
        config = ScannerConfig(scanner_name="x", version="x_v1", params={"adx_min": "high"})
        with pytest.raises(ScannerConfigError, match="must be a number"):
            config.require_float("adx_min")

    def test_missing_version_is_refused(self, tmp_path, monkeypatch):
        directory = tmp_path / "demo"
        directory.mkdir()
        (directory / "config.json").write_text(json.dumps({"params": {"a": 1}}))
        monkeypatch.setenv("SCANNER_CONFIG_DIR", str(tmp_path))
        with pytest.raises(ScannerConfigError, match="version"):
            load_config("demo")

    def test_missing_file_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SCANNER_CONFIG_DIR", str(tmp_path))
        with pytest.raises(ScannerConfigError, match="no scanner config"):
            load_config("nope")


class TestResultStore:
    def test_signals_round_trip(self):
        result_store.write_signals([make_signal()], trading_day="2026-08-12")
        stored = result_store.read_signals("2026-08-12")
        assert len(stored) == 1
        assert stored[0].symbol == "TEST"

    def test_appends_do_not_overwrite(self):
        result_store.write_signals([make_signal(symbol="AAA")], trading_day="2026-08-12")
        result_store.write_signals([make_signal(symbol="BBB")], trading_day="2026-08-12")
        assert {signal.symbol for signal in result_store.read_signals("2026-08-12")} == {
            "AAA", "BBB"}

    def test_duplicate_signal_ids_collapse_on_read(self):
        """A re-run scan must not double-count in the month-end stats."""
        signal = make_signal()
        result_store.write_signals([signal], trading_day="2026-08-12")
        result_store.write_signals([signal], trading_day="2026-08-12")
        assert len(result_store.read_signals("2026-08-12")) == 1

    def test_same_symbol_under_two_scanners_is_two_rows(self):
        """Section 6, at the storage layer."""
        result_store.write_signals(
            [make_signal(scanner_name="hma_early_trend"),
             make_signal(scanner_name="accumulation")],
            trading_day="2026-08-12")
        assert len(result_store.read_signals("2026-08-12")) == 2

    def test_a_malformed_line_loses_one_row_not_the_file(self):
        result_store.write_signals([make_signal(symbol="GOOD")], trading_day="2026-08-12")
        with open(result_store.signals_path("2026-08-12"), "a", encoding="utf-8") as handle:
            handle.write("{not json at all\n")
        result_store.write_signals([make_signal(symbol="ALSOGOOD")], trading_day="2026-08-12")
        assert len(result_store.read_signals("2026-08-12")) == 2

    def test_reading_a_day_with_no_file_is_empty_not_an_error(self):
        assert result_store.read_signals("1999-01-01") == []

    def test_performance_latest_record_wins(self):
        """The tracker re-runs as horizons mature; the run with 5 days of
        bars must supersede the run that only had 1."""
        result_store.write_performance(
            [{"signal_id": "abc", "return_5d": None}], trading_day="2026-08-12")
        result_store.write_performance(
            [{"signal_id": "abc", "return_5d": 4.2}], trading_day="2026-08-12")
        assert result_store.read_performance("2026-08-12")["abc"]["return_5d"] == 4.2

    def test_joined_rows_keep_signals_with_no_performance_yet(self):
        """Dropping them would overstate hit rate, because the missing
        ones are disproportionately the most recent."""
        result_store.write_signals([make_signal()], trading_day="2026-08-12")
        rows = result_store.joined_rows("2026-08-01", "2026-08-31")
        assert len(rows) == 1
        assert rows[0].get("return_5d") is None

    def test_joined_rows_merge_performance_when_present(self):
        signal = make_signal()
        result_store.write_signals([signal], trading_day="2026-08-12")
        result_store.write_performance(
            [{"signal_id": signal.signal_id, "return_1d": 3.0, "mfe_5d": 8.0}],
            trading_day="2026-08-12")
        row = result_store.joined_rows("2026-08-01", "2026-08-31")[0]
        assert row["return_1d"] == 3.0
        assert row["mfe_5d"] == 8.0

    def test_analytics_dir_is_not_the_candidate_dir(self, monkeypatch):
        """Section 10: the two stores must be separate at the filesystem
        level, so a bug here cannot put a row in front of the order path."""
        from market_data import candidate_store

        monkeypatch.setenv("KIS_CANDIDATE_DIR", "/tmp/candidates-under-test")
        monkeypatch.setenv(result_store.ANALYTICS_DIR_ENV, "/tmp/analytics-under-test")
        assert result_store.analytics_dir() != candidate_store.candidate_dir()


class TestProviders:
    def test_caching_provider_fetches_once_per_key(self):
        calls = []

        class Counting(StaticMarketDataProvider):
            def get_daily_bars(self, symbol, lookback_days=400):
                calls.append(symbol)
                return super().get_daily_bars(symbol, lookback_days=lookback_days)

        inner = Counting(daily={"AAA": fx.daily_frame([1.0] * 300)})
        provider = CachingMarketDataProvider(inner)
        provider.get_daily_bars("AAA")
        provider.get_daily_bars("AAA")
        assert calls == ["AAA"]

    def test_caching_provider_caches_failures_too(self):
        """Otherwise a delisted symbol costs one failed round trip per
        signal that references it."""
        calls = []

        class Counting(StaticMarketDataProvider):
            def get_daily_bars(self, symbol, lookback_days=400):
                calls.append(symbol)
                return super().get_daily_bars(symbol, lookback_days=lookback_days)

        provider = CachingMarketDataProvider(Counting())
        for _ in range(3):
            with pytest.raises(MarketDataUnavailable):
                provider.get_daily_bars("GONE")
        assert calls == ["GONE"]

    def test_get_symbol_data_survives_missing_intraday(self):
        provider = StaticMarketDataProvider(daily={"AAA": fx.daily_frame([1.0] * 300)})
        bundle = provider.get_symbol_data("AAA")
        assert bundle.daily is not None
        assert bundle.intraday is None

    def test_get_symbol_data_propagates_a_missing_daily_frame(self):
        """No scanner here can do anything without daily bars, so this
        one does NOT get demoted to None."""
        provider = StaticMarketDataProvider()
        with pytest.raises(MarketDataUnavailable):
            provider.get_symbol_data("AAA")

    def test_an_intraday_request_never_exceeds_the_intervals_limit(self):
        """Regression: the tracker asked for 7 days of 1-minute bars.

        Seven days is inside the provider's 8-day 1-minute limit, but
        there is no `7d` period string, so it rounded UP to `1mo` and the
        provider refused the request -- returning an EMPTY frame, which
        is indistinguishable from "this symbol did not trade". Every
        intraday return and the whole signal-day excursion window came
        back null, in production only, silently.

        Caught by an end-to-end run rather than by a unit test, because
        the static test provider ignores the period entirely. Hence this
        test asserts on the period actually SENT.
        """
        from scanners.base.market_data_provider import (
            MAX_PERIOD_FOR_INTERVAL,
            PERIODS,
            YFinanceMarketDataProvider,
        )

        sent = []

        class RecordingTicker:
            def history(self, **kwargs):
                sent.append(kwargs)
                return fx.daily_frame([1.0] * 5)

        provider = YFinanceMarketDataProvider(ticker_factory=lambda symbol: RecordingTicker())
        for interval, ceiling in MAX_PERIOD_FOR_INTERVAL.items():
            for days in (1, 5, 7, 30, 400, 5000):
                sent.clear()
                provider.get_intraday_bars("AAA", interval=interval, lookback_days=days)
                period = sent[0]["period"]
                assert period in PERIODS, (interval, days, period)
                assert PERIODS.index(period) <= PERIODS.index(ceiling), (
                    f"{interval} lookback {days}d asked for {period}, "
                    f"beyond the {ceiling} limit")

    def test_a_seven_day_one_minute_request_becomes_five_days(self):
        from scanners.base.market_data_provider import YFinanceMarketDataProvider

        sent = []

        class RecordingTicker:
            def history(self, **kwargs):
                sent.append(kwargs)
                return fx.daily_frame([1.0] * 5)

        provider = YFinanceMarketDataProvider(ticker_factory=lambda symbol: RecordingTicker())
        provider.get_intraday_bars("AAA", interval="1m", lookback_days=7)
        assert sent[0]["period"] == "5d"

    def test_daily_requests_are_not_capped(self):
        """The cap applies to intraday intervals only -- HMA200 needs a
        year of daily bars and must keep getting them."""
        from scanners.base.market_data_provider import YFinanceMarketDataProvider

        sent = []

        class RecordingTicker:
            def history(self, **kwargs):
                sent.append(kwargs)
                return fx.daily_frame([1.0] * 5)

        provider = YFinanceMarketDataProvider(ticker_factory=lambda symbol: RecordingTicker())
        provider.get_daily_bars("AAA", lookback_days=400)
        assert sent[0]["period"] == "2y"
        assert sent[0]["interval"] == "1d"

    def test_require_daily_names_what_was_missing(self):
        bundle = SymbolData(symbol="AAA", daily=fx.daily_frame([1.0] * 10))
        with pytest.raises(ScannerDataError, match="need 300"):
            bundle.require_daily(minimum_bars=300)


class TestPerSymbolIsolation:
    """Section 5's inner layer: one bad symbol must cost one evaluation,
    never the scanner's other 799."""

    def test_an_exploding_symbol_does_not_end_the_scan(self):
        class Exploding(BaseScanner):
            scanner_dir = "hma_early_trend"
            scanner_name = "hma_early_trend"

            def check(self, features, data, context):
                if data.symbol == "BOOM":
                    raise RuntimeError("synthetic failure")
                return ["ok"]

            def score(self, features, data, context):
                return 50.0

        scanner = Exploding()
        bundles = [fx.uptrend_bundle("AAA"), fx.uptrend_bundle("BOOM"),
                   fx.uptrend_bundle("ZZZ")]
        outcome = scanner.scan(bundles, trading_day="2026-08-12")
        assert outcome.exceptions == 1
        assert {signal.symbol for signal in outcome.signals} == {"AAA", "ZZZ"}
        assert outcome.error_samples and "BOOM" in outcome.error_samples[0]

    def test_data_errors_are_counted_separately_from_rejections(self):
        """"We could not judge it" and "we judged it and it failed" are
        different findings; conflating them makes month-1's rejection
        statistics meaningless."""
        scanner = build_scanner("hma_early_trend")
        short = SymbolData(symbol="NEW", daily=fx.daily_frame([10.0] * 30))
        outcome = scanner.scan([short], trading_day="2026-08-12")
        assert outcome.data_errors == 1
        assert outcome.rejected == 0
        assert outcome.signals == []

    def test_score_is_clamped_to_0_100(self):
        class Overscoring(BaseScanner):
            scanner_dir = "hma_early_trend"
            scanner_name = "hma_early_trend"

            def check(self, features, data, context):
                return ["ok"]

            def score(self, features, data, context):
                return 5000.0

        signal = Overscoring().evaluate(fx.uptrend_bundle(), trading_day="2026-08-12")
        assert signal.scanner_score == 100.0

    def test_rejection_reason_is_recorded_not_just_a_boolean(self):
        """Section 27, on the reject side."""
        class Picky(BaseScanner):
            scanner_dir = "hma_early_trend"
            scanner_name = "hma_early_trend"

            def check(self, features, data, context):
                raise Rejected("ADX 14.20 not above 20.00")

            def score(self, features, data, context):
                return 0.0

        assert Picky().evaluate(fx.uptrend_bundle(), trading_day="2026-08-12") is None

    def test_context_does_not_leak_between_symbols(self):
        """A reused scanner instance must not carry one symbol's state
        into the next one's score."""
        seen = []

        class Recording(BaseScanner):
            scanner_dir = "hma_early_trend"
            scanner_name = "hma_early_trend"

            def check(self, features, data, context):
                seen.append(dict(context))
                context["symbol"] = data.symbol
                return ["ok"]

            def score(self, features, data, context):
                return 10.0

        scanner = Recording()
        scanner.scan([fx.uptrend_bundle("AAA"), fx.uptrend_bundle("BBB")],
                     trading_day="2026-08-12")
        assert seen == [{}, {}]


class TestRegistry:
    def test_all_six_scanners_build(self):
        built = build_scanners()
        assert len(built) == 6
        assert {scanner.scanner_name for scanner in built} == set(ALL_SCANNERS)

    def test_a_broken_scanner_does_not_stop_the_others(self, monkeypatch):
        """Section 5's isolation starts at import, not at evaluation."""
        from scanners import registry

        monkeypatch.setitem(registry.SCANNER_SPECS, "orb",
                            ("scanners.orb.does_not_exist", "Nope"))
        failures = {}
        built = registry.build_scanners(on_error=lambda name, exc: failures.update({name: exc}))
        assert len(built) == 5
        assert "orb" in failures

    def test_scanner_names_are_stable_identifiers(self):
        """Section 6 keys the duplicate analysis on these; renaming one
        would split its history in two."""
        assert set(ALL_SCANNERS) == {
            "hma_early_trend", "accumulation", "breakout_ready",
            "premarket_momentum", "gap_pullback", "orb"}
