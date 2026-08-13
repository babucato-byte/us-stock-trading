"""Runtime optimisation: fewer calls, identical verdicts (spec 9-15, 19-25).

The measured starting point was 2.387 s/symbol over 200 server symbols,
of which 49% was HMA and 29.5% of symbols could not be judged at all.
Three changes address that -- one shared feature pass, a vectorised HMA,
and an eligibility cache -- and every one of them is only acceptable if
the scanners still reach exactly the same conclusions.

So this file is organised around that constraint. The counting tests
prove work was removed; `TestVerdictEquivalence` proves nothing else
was. The second is the one that would matter if it broke, because a
subtly different verdict does not raise -- it just quietly changes a
month of data.
"""

import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scanners import runner  # noqa: E402
from scanners.base import activity as act  # noqa: E402
from scanners.base import eligibility as elig  # noqa: E402
from scanners.base import features as feat_mod  # noqa: E402
from scanners.base import indicators as sind  # noqa: E402
from scanners.base.market_data_provider import StaticMarketDataProvider  # noqa: E402
from scanners.base.models import ScannerDataError  # noqa: E402
from scanners.registry import ALL_SCANNERS, build_scanner  # noqa: E402
from tests import scanner_fixtures as fx  # noqa: E402

DAY = "2026-08-13"

#: Identifiers that would mean a strategy condition had leaked into a
#: layer that is supposed to answer only "can this be computed" or "does
#: this trade" (spec sections 6 and 14).
STRATEGY_TERMS = ("adx", "hma", "rsi", "macd", "gap_pct", "premarket_gain",
                  "volume_multiple", "breakout", "extension")


def _strategy_terms_in(path):
    """Strategy identifiers appearing as CODE in `path`.

    Matched on identifier boundaries, not as substrings: "rsi" occurs
    inside "persistence", and a substring check would fail on a comment
    about saving a file. Docstrings and comments are stripped first --
    the concern is a threshold in the logic, not a sentence explaining
    that there is none.
    """
    import ast
    import re

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id.lower())
        elif isinstance(node, ast.Attribute):
            names.add(node.attr.lower())
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg.lower())
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if re.fullmatch(r"[a-z0-9_]+", node.value or ""):
                names.add(node.value.lower())
    return sorted(n for n in names
                  if any(re.search(rf"(^|_){term}(_|$)", n) for term in STRATEGY_TERMS))


@pytest.fixture
def provider():
    daily = fx.uptrend_bundle("TEST", volumes=fx.volume_surge()).daily
    intraday = fx.orb_bundle("TEST").intraday
    return StaticMarketDataProvider(daily={"TEST": daily}, intraday={"TEST": intraday})


@pytest.fixture
def counters(monkeypatch):
    """Count the expensive operations a run actually performs."""
    calls = Counter()

    original_hma = sind.fast_hma

    def counting_hma(series, length):
        calls[f"hma({length})"] += 1
        calls["hma_total"] += 1
        return original_hma(series, length)

    monkeypatch.setattr(sind, "fast_hma", counting_hma)

    original_build = feat_mod.build_features

    def counting_build(data, **kwargs):
        calls["build_features"] += 1
        return original_build(data, **kwargs)

    monkeypatch.setattr(feat_mod, "build_features", counting_build)
    monkeypatch.setattr("scanners.base.scanner_base.build_features", counting_build)
    monkeypatch.setattr("scanners.runner.build_features", counting_build)
    return calls


class TestFeatureReuse:
    """Section 11: one feature pass per symbol, however many scanners."""

    @pytest.mark.parametrize("profile,scanner_count", [
        ("daily", 3), ("open", 2), ("premarket", 1), ("all", 6),
    ])
    def test_features_are_computed_once_per_symbol(self, provider, counters,
                                                   profile, scanner_count):
        runner.run_scanners(scanners=runner.PROFILES[profile], symbols=["TEST"],
                            provider=provider, trading_day=DAY, store=False)
        assert len(runner.PROFILES[profile]) == scanner_count
        assert counters["build_features"] == 1, (
            f"{scanner_count} scanners triggered {counters['build_features']} "
            "feature passes; they must share one")

    def test_each_hma_is_computed_once_per_symbol(self, provider, counters):
        runner.run_scanners(scanners=runner.PROFILES["all"], symbols=["TEST"],
                            provider=provider, trading_day=DAY, store=False)
        assert counters["hma(89)"] == 1
        assert counters["hma(200)"] == 1

    def test_the_work_does_not_grow_with_scanner_count(self, provider, counters):
        runner.run_scanners(scanners=runner.PROFILES["premarket"], symbols=["TEST"],
                            provider=provider, trading_day=DAY, store=False)
        one_scanner = counters["hma_total"]
        counters.clear()
        runner.run_scanners(scanners=runner.PROFILES["all"], symbols=["TEST"],
                            provider=provider, trading_day=DAY, store=False)
        assert counters["hma_total"] == one_scanner

    def test_a_scanner_alone_still_computes_its_own_features(self, provider):
        """`evaluate()` must remain usable without the runner -- every
        scanner unit test drives it that way."""
        bundle = fx.uptrend_bundle("TEST", volumes=fx.volume_surge())
        signal = build_scanner("hma_early_trend").evaluate(bundle, trading_day=DAY)
        assert signal is not None


class TestFetchReuse:
    """Section 12. This already held before the optimisation; pinned so
    it cannot regress while the surrounding code moves."""

    def test_one_daily_fetch_per_symbol_regardless_of_scanners(self, provider):
        calls = Counter()

        class Counting(StaticMarketDataProvider):
            def get_daily_bars(self, symbol, lookback_days=400):
                calls["daily"] += 1
                return super().get_daily_bars(symbol, lookback_days=lookback_days)

            def get_intraday_bars(self, symbol, **kwargs):
                calls["intraday"] += 1
                return super().get_intraday_bars(symbol, **kwargs)

        counting = Counting(daily=provider._daily, intraday=provider._intraday)
        runner.run_scanners(scanners=runner.PROFILES["all"], symbols=["TEST"],
                            provider=counting, trading_day=DAY, store=False)
        assert calls["daily"] == 1
        assert calls["intraday"] == 1


class TestVerdictEquivalence:
    """Section 19 -- the constraint the whole optimisation lives under.

    Every scanner's PASS/REJECT, score and reason must be identical to
    what the pre-optimisation path produced. The comparison is made
    against the REFERENCE HMA, recomputed here, so it is a real
    end-to-end check rather than the optimised path agreeing with
    itself.
    """

    def bundles(self):
        return {
            "hma_early_trend": fx.uptrend_bundle("TEST"),
            "accumulation": fx.uptrend_bundle("TEST", volumes=fx.volume_surge()),
            "breakout_ready": fx.uptrend_bundle("TEST"),
            "premarket_momentum": fx.premarket_momentum_bundle("TEST"),
            "gap_pullback": fx.gap_pullback_bundle("TEST"),
            "orb": fx.orb_bundle("TEST"),
        }

    @pytest.mark.parametrize("scanner_name", ALL_SCANNERS)
    def test_same_verdict_through_reference_and_fast_hma(self, scanner_name,
                                                         monkeypatch):
        import indicators as reference

        bundles = self.bundles()
        results = {}
        for label, hma_impl in (("fast", sind.fast_hma), ("reference", reference.hma)):
            monkeypatch.setattr(sind, "fast_hma", hma_impl)
            feat_mod.reset_common_config()
            signals = {}
            for bundle_name, bundle in bundles.items():
                scanner = build_scanner(scanner_name)
                signal = None
                try:
                    signal = scanner.evaluate(bundle, trading_day=DAY,
                                              timestamp="2026-08-13T14:00:00+00:00")
                except ScannerDataError:
                    signal = "DATA_ERROR"
                signals[bundle_name] = signal
            results[label] = signals

        for bundle_name in bundles:
            fast = results["fast"][bundle_name]
            ref = results["reference"][bundle_name]
            assert (fast is None) == (ref is None), (
                f"{scanner_name}/{bundle_name}: PASS/REJECT differs")
            if fast is None or fast == "DATA_ERROR" or ref == "DATA_ERROR":
                assert fast == ref or (fast is None and ref is None)
                continue
            assert fast.reasons == ref.reasons, f"{scanner_name}/{bundle_name}: reason"
            assert fast.scanner_score == pytest.approx(ref.scanner_score, rel=1e-9), (
                f"{scanner_name}/{bundle_name}: score")
            assert fast.signal_price == pytest.approx(ref.signal_price, rel=1e-9)

    def test_shared_features_give_the_same_signals_as_per_scanner_features(self, provider):
        """The other half: sharing the pass must not change a verdict."""
        shared = runner.run_scanners(
            scanners=runner.PROFILES["all"], symbols=["TEST"],
            provider=provider, trading_day=DAY, store=False)

        bundle = provider.get_symbol_data("TEST")
        for outcome in shared.outcomes:
            scanner = build_scanner(outcome.scanner_name)
            try:
                alone = scanner.evaluate(bundle, trading_day=DAY)
            except ScannerDataError:
                alone = None
            got = outcome.signals[0] if outcome.signals else None
            assert (alone is None) == (got is None), outcome.scanner_name
            if alone is not None and got is not None:
                assert alone.reasons == got.reasons
                assert alone.scanner_score == pytest.approx(got.scanner_score)


class TestDataErrorClassification:
    """Sections 21 and 22."""

    def test_a_pandas_data_error_becomes_a_scanner_data_error(self, monkeypatch):
        """A malformed frame is a DATA problem, not a scanner fault, and
        must not arrive as an ERROR with a traceback."""
        def explode(*args, **kwargs):
            raise pd.errors.DataError("No numeric types to aggregate")

        monkeypatch.setattr(feat_mod.ind, "adx_series", explode)
        bundle = fx.uptrend_bundle("BAD")
        with pytest.raises(ScannerDataError, match="non_numeric_ohlcv"):
            feat_mod.build_features(bundle)

    def test_it_is_counted_as_a_data_error_not_an_exception(self, monkeypatch):
        def explode(*args, **kwargs):
            raise pd.errors.DataError("No numeric types to aggregate")

        monkeypatch.setattr(feat_mod.ind, "adx_series", explode)
        scanner = build_scanner("hma_early_trend")
        outcome = scanner.scan([fx.uptrend_bundle("BAD")], trading_day=DAY)
        assert outcome.data_errors == 1
        assert outcome.exceptions == 0

    def test_an_unexpected_exception_still_propagates_as_an_exception(self, monkeypatch):
        """The classification must be narrow. A genuine bug hidden as a
        data error is the failure this is meant to prevent, inverted."""
        def explode(*args, **kwargs):
            raise RuntimeError("a real bug")

        monkeypatch.setattr(feat_mod.ind, "adx_series", explode)
        scanner = build_scanner("hma_early_trend")
        outcome = scanner.scan([fx.uptrend_bundle("BAD")], trading_day=DAY)
        assert outcome.exceptions == 1
        assert outcome.data_errors == 0

    def test_the_reason_maps_onto_an_eligibility_verdict(self):
        assert elig.classify_data_error(
            "X: OHLCV columns are not numeric (reason=non_numeric_ohlcv)"
        ) == elig.NON_NUMERIC_OHLCV
        assert elig.classify_data_error("X: 40 daily bars, need 218") == (
            elig.INSUFFICIENT_HISTORY)
        assert elig.classify_data_error("X: newest daily bar is 40d old, limit 5d") == (
            elig.STALE_HISTORY)
        assert elig.classify_data_error("X: no daily bars") == elig.EMPTY_HISTORY
        assert elig.classify_data_error("something unforeseen") == elig.EMPTY_HISTORY


class TestEligibility:
    """Sections 5-8."""

    def test_a_symbol_with_short_history_is_recorded_ineligible(self):
        short = fx.daily_frame(np.linspace(10, 12, 40))
        good = fx.uptrend_bundle("GOOD", volumes=fx.volume_surge()).daily
        prov = StaticMarketDataProvider(daily={"GOOD": good, "SHORT": short})
        runner.run_scanners(scanners=runner.PROFILES["daily"], provider=prov,
                            symbols=["GOOD", "SHORT"], trading_day=DAY, store=False,
                            profile="daily")
        store = elig.EligibilityStore.load("static")
        assert store.get("GOOD").eligible is True
        assert store.get("SHORT").eligible is False
        assert store.get("SHORT").reason == elig.INSUFFICIENT_HISTORY

    def test_a_second_run_skips_the_ineligible_symbol(self):
        short = fx.daily_frame(np.linspace(10, 12, 40))
        good = fx.uptrend_bundle("GOOD", volumes=fx.volume_surge()).daily
        prov = StaticMarketDataProvider(daily={"GOOD": good, "SHORT": short})
        first = runner.run_scanners(scanners=runner.PROFILES["daily"], provider=prov,
                                    symbols=["GOOD", "SHORT"], trading_day=DAY,
                                    store=False, profile="daily")
        # Explicit symbols are never filtered, so the skip is observed
        # through the store rather than through a second explicit run.
        store = elig.EligibilityStore.load("static")
        assert store.should_skip("SHORT") is True
        assert store.should_skip("GOOD") is False
        assert first.signal_count > 0

    def test_explicit_symbols_are_never_filtered(self):
        """`--symbols X` must scan X even if a cache dislikes it, or the
        flag is untrustworthy exactly when it is used for debugging."""
        good = fx.uptrend_bundle("GOOD", volumes=fx.volume_surge()).daily
        prov = StaticMarketDataProvider(daily={"GOOD": good})
        store = elig.EligibilityStore.load("static")
        store.note_ineligible("GOOD", elig.INSUFFICIENT_HISTORY, history_bars=1,
                              required_bars=218)
        store.save()
        report = runner.run_scanners(scanners=["hma_early_trend"], provider=prov,
                                     symbols=["GOOD"], trading_day=DAY, store=False,
                                     profile="daily")
        assert report.skipped_ineligible == 0
        assert report.universe_size == 1

    def test_recheck_for_short_history_is_derived_from_the_shortfall(self):
        """A symbol 3 bars short returns in days; one 200 short is not
        re-fetched 200 times to be told the same thing."""
        near = elig.recheck_days_for(elig.INSUFFICIENT_HISTORY,
                                     history_bars=215, required_bars=218)
        far = elig.recheck_days_for(elig.INSUFFICIENT_HISTORY,
                                    history_bars=18, required_bars=218)
        assert near < far
        assert near <= 7
        assert far <= elig.MAX_RECHECK_DAYS

    def test_provider_failures_are_treated_as_transient(self):
        """One bad afternoon at the vendor must not evict a third of the
        universe for a month."""
        assert elig.RECHECK_DAYS[elig.PROVIDER_UNAVAILABLE] <= 1
        assert (elig.RECHECK_DAYS[elig.PROVIDER_UNAVAILABLE]
                < elig.RECHECK_DAYS[elig.UNSUPPORTED_SYMBOL])

    def test_nothing_is_excluded_permanently(self):
        for reason, days in elig.RECHECK_DAYS.items():
            assert days <= elig.MAX_RECHECK_DAYS, reason
        record = elig.make_record("X", eligible=False,
                                  reason=elig.INSUFFICIENT_HISTORY, provider="p",
                                  history_bars=0, required_bars=100_000)
        assert date.fromisoformat(record.next_check) <= (
            date.today() + timedelta(days=elig.MAX_RECHECK_DAYS))

    def test_a_due_record_no_longer_skips(self):
        store = elig.EligibilityStore("static")
        store.note_ineligible("X", elig.EMPTY_HISTORY,
                              today=date.today() - timedelta(days=365))
        assert store.should_skip("X") is False

    def test_an_eligible_record_never_causes_a_skip(self):
        store = elig.EligibilityStore("static")
        store.note_eligible("X")
        assert store.should_skip("X") is False

    def test_the_cache_survives_a_round_trip(self):
        store = elig.EligibilityStore("static")
        store.note_ineligible("X", elig.INSUFFICIENT_HISTORY, history_bars=5,
                              required_bars=218)
        store.save()
        assert elig.EligibilityStore.load("static").get("X").reason == (
            elig.INSUFFICIENT_HISTORY)

    def test_a_corrupt_cache_degrades_to_empty_rather_than_raising(self):
        path = elig.store_path("static")
        path.write_text("{not json", encoding="utf-8")
        store = elig.EligibilityStore.load("static")
        assert store.get("ANY") is None

    def test_eligibility_holds_no_strategy_condition(self):
        """Section 6. A threshold here would be a hidden filter that no
        scanner config records and no month-1 fingerprint captures."""
        assert _strategy_terms_in(REPO_ROOT / "scanners" / "base" / "eligibility.py") == []

    def test_disabling_eligibility_scans_everything(self):
        good = fx.uptrend_bundle("GOOD", volumes=fx.volume_surge()).daily
        prov = StaticMarketDataProvider(daily={"GOOD": good})
        store = elig.EligibilityStore.load("static")
        store.note_ineligible("GOOD", elig.EMPTY_HISTORY)
        store.save()
        report = runner.run_scanners(scanners=["hma_early_trend"], provider=prov,
                                     symbols=["GOOD"], trading_day=DAY, store=False,
                                     use_eligibility=False)
        assert report.skipped_ineligible == 0


class TestRequiredHistoryIsDerived:
    """Section 7: no hardcoded 218."""

    def test_every_scanner_reports_a_derived_requirement(self):
        from scanners.base.features import minimum_daily_bars

        for name in ALL_SCANNERS:
            assert build_scanner(name).required_history == minimum_daily_bars()

    def test_the_requirement_tracks_the_configured_hma_length(self, monkeypatch):
        """Raising the HMA length must raise the requirement, not leave a
        stale literal behind."""
        from scanners.base import features

        features.reset_common_config()
        base = features.minimum_daily_bars()
        config = features.common_config()
        monkeypatch.setitem(config.params, "hma_slow_length", 300)
        assert features.minimum_daily_bars() > base
        features.reset_common_config()

    def test_no_module_hardcodes_the_bar_count(self):
        for name in ("scanners/base/eligibility.py", "scanners/runner.py"):
            source = (REPO_ROOT / name).read_text(encoding="utf-8")
            assert "218" not in source, name


class TestActivityFunnel:
    """Sections 13, 14, 29."""

    def wide_provider(self, count=8):
        daily, intraday = {}, {}
        for index in range(count):
            symbol = f"S{index}"
            volumes = np.full(fx.DEFAULT_DAILY_BARS, 1_000_000.0 * (index + 1))
            daily[symbol] = fx.daily_frame(fx.accelerating_uptrend(), volumes=volumes)
            intraday[symbol] = fx.orb_bundle(symbol).intraday
        return StaticMarketDataProvider(daily=daily, intraday=intraday)

    def test_the_intraday_profiles_default_to_the_active_universe(self):
        assert runner.PROFILE_UNIVERSE["open"] == runner.UNIVERSE_ACTIVE
        assert runner.PROFILE_UNIVERSE["premarket"] == runner.UNIVERSE_ACTIVE
        assert runner.PROFILE_UNIVERSE["daily"] == runner.UNIVERSE_FULL

    def test_an_open_run_without_a_ranking_refuses_rather_than_reporting_quiet(self):
        """Section 14: an empty pool is an operational fact, not a market
        with no active names."""
        report = runner.run_scanners(scanners=runner.PROFILES["open"],
                                     provider=self.wide_provider(), trading_day=DAY,
                                     store=False, profile="open")
        assert report.status == "FAILED_NO_UNIVERSE"
        assert report.candidate_count is None
        assert "daily profile first" in report.skipped_reason

    def test_daily_populates_the_ranking_and_open_consumes_it(self):
        prov = self.wide_provider()
        symbols = [f"S{i}" for i in range(8)]
        runner.run_scanners(scanners=runner.PROFILES["daily"], provider=prov,
                            symbols=symbols, trading_day=DAY, store=False,
                            profile="daily")
        report = runner.run_scanners(scanners=runner.PROFILES["open"], provider=prov,
                                     trading_day=DAY, store=False, profile="open",
                                     active_pool_size=3)
        assert report.universe_type == runner.UNIVERSE_ACTIVE
        assert report.universe_size == 3

    def test_the_pool_is_ranked_by_dollar_volume(self):
        prov = self.wide_provider()
        symbols = [f"S{i}" for i in range(8)]
        runner.run_scanners(scanners=runner.PROFILES["daily"], provider=prov,
                            symbols=symbols, trading_day=DAY, store=False,
                            profile="daily")
        pool = act.ActivityStore.load("static").active_symbols(limit=3)
        assert pool == ["S7", "S6", "S5"], pool

    def test_a_stale_ranking_is_not_used(self):
        """Yesteryear's most active names look entirely plausible."""
        store = act.ActivityStore("static")
        store.note("OLD", trading_day="2020-01-02", price=100.0, avg_volume=1e9)
        store.save()
        assert act.ActivityStore.load("static").active_symbols(limit=10) == []

    def test_an_intraday_run_does_not_rewrite_the_ranking(self):
        """Otherwise the pool would shrink to itself run after run until
        nothing outside it could ever re-enter."""
        prov = self.wide_provider()
        symbols = [f"S{i}" for i in range(8)]
        runner.run_scanners(scanners=runner.PROFILES["daily"], provider=prov,
                            symbols=symbols, trading_day=DAY, store=False,
                            profile="daily")
        before = set(act.ActivityStore.load("static")._records)
        runner.run_scanners(scanners=runner.PROFILES["open"], provider=prov,
                            trading_day=DAY, store=False, profile="open",
                            active_pool_size=2)
        after = set(act.ActivityStore.load("static")._records)
        assert before == after

    def test_activity_holds_no_strategy_condition(self):
        """Section 14: dollar volume measures whether a name TRADES, not
        whether it is going anywhere."""
        assert _strategy_terms_in(REPO_ROOT / "scanners" / "base" / "activity.py") == []

    def test_the_run_manifest_records_which_universe_was_used(self):
        prov = self.wide_provider()
        report = runner.run_scanners(scanners=["hma_early_trend"], provider=prov,
                                     symbols=["S1"], trading_day=DAY, store=False,
                                     profile="daily")
        manifest = report.to_manifest()
        assert "universe_type" in manifest
        assert "eligibility" in manifest
        assert "activity" in manifest
        assert manifest["required_history_bars"] > 0
