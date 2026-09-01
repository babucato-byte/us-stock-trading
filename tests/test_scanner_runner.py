"""The runner: scanner-level isolation, storage, and the calendar gate.

Section 5's outer layer. `tests/test_scanner_edge_cases.py` covers the
inner one (a bad symbol costs one evaluation); these cover the case
where a whole scanner is broken and the other five have to finish and
store their day regardless.

The fetch-once property is also pinned here. It is not just an
efficiency concern: if each scanner fetched for itself, two scanners
that both flagged NVDA would have judged it from separate downloads
taken minutes apart, and section 17's intersection analysis would be
partly measuring the gap between those downloads.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scanners import runner  # noqa: E402
from scanners.base import result_store  # noqa: E402
from scanners.base.market_data_provider import (  # noqa: E402
    MarketDataUnavailable,
    StaticMarketDataProvider,
)
from scanners.publish import candidates as publisher  # noqa: E402
from scanners.universe import UniverseUnavailable, load_symbols  # noqa: E402
from tests import scanner_fixtures as fx  # noqa: E402

DAY = "2026-08-12"


@pytest.fixture
def provider():
    """One symbol that passes several scanners at once.

    Deliberately a multi-scanner pass: section 6 requires the same
    symbol to be recorded once per scanner, and a single-scanner fixture
    could not detect a regression that deduplicated them.
    """
    accumulating = fx.uptrend_bundle("TEST", volumes=fx.volume_surge())
    orb = fx.orb_bundle("TEST")
    return StaticMarketDataProvider(
        daily={"TEST": accumulating.daily},
        intraday={"TEST": orb.intraday},
    )


class TestScannerLevelIsolation:
    def test_a_broken_scanner_does_not_stop_the_others(self, provider, monkeypatch):
        """One scanner failing on every symbol must not cost the others
        their day. Section 5's outer isolation layer."""
        from scanners.accumulation.scanner import AccumulationScanner

        monkeypatch.setattr(AccumulationScanner, "evaluate",
                            lambda self, data, **kwargs: (_ for _ in ()).throw(
                                RuntimeError("synthetic scanner failure")))
        report = runner.run_scanners(symbols=["TEST"], provider=provider, trading_day=DAY)

        broken = [o for o in report.outcomes if o.scanner_name == "accumulation"][0]
        assert broken.exceptions == 1
        assert broken.signals == []
        assert len(report.outcomes) == 6
        assert report.signal_count > 0, "the other scanners still produced signals"
        assert report.stored_signals > 0

    def test_a_scanner_failing_on_every_symbol_is_declared_broken(self, provider,
                                                                  monkeypatch):
        """A per-symbol catch is right for one bad ticker and wrong for a
        broken scanner: without a circuit breaker the same systemic fault
        is reported as 800 unrelated failures and the summary still says
        `failed=False`."""
        from scanners.accumulation.scanner import AccumulationScanner

        monkeypatch.setattr(AccumulationScanner, "evaluate",
                            lambda self, data, **kwargs: (_ for _ in ()).throw(
                                RuntimeError("synthetic scanner failure")))
        # The real threshold is 25; each symbol costs a full HMA200 pass
        # across six scanners, so the breaker is lowered here rather than
        # scanning 30 symbols to prove an off-by-one nobody is at risk of.
        monkeypatch.setattr(runner, "MAX_CONSECUTIVE_SCANNER_ERRORS", 4)
        symbols = [f"S{index}" for index in range(6)]
        daily = provider._daily["TEST"]
        wide = StaticMarketDataProvider(
            daily={symbol: daily for symbol in symbols},
            intraday={symbol: provider._intraday["TEST"] for symbol in symbols},
        )
        report = runner.run_scanners(symbols=symbols, provider=wide, trading_day=DAY)

        broken = [o for o in report.outcomes if o.scanner_name == "accumulation"][0]
        assert broken.failed
        assert "consecutive symbol failures" in broken.failure_reason
        # Stopped early rather than burning the whole universe.
        assert broken.exceptions == 4
        # And the others completed the full list.
        healthy = [o for o in report.outcomes if o.scanner_name == "hma_early_trend"][0]
        assert healthy.symbols_seen == len(symbols)
        assert not healthy.failed

    def test_scattered_symbol_failures_do_not_trip_the_breaker(self, provider,
                                                               monkeypatch):
        """A handful of malformed symbols in a large universe is normal;
        the counter must reset on any ordinary outcome."""
        from scanners.accumulation.scanner import AccumulationScanner

        original = AccumulationScanner.evaluate

        def sometimes(self, data, **kwargs):
            if data.symbol.endswith("7"):
                raise RuntimeError("one bad symbol")
            return original(self, data, **kwargs)

        monkeypatch.setattr(AccumulationScanner, "evaluate", sometimes)
        monkeypatch.setattr(runner, "MAX_CONSECUTIVE_SCANNER_ERRORS", 4)
        symbols = [f"S{index}" for index in range(20)]
        daily = provider._daily["TEST"]
        wide = StaticMarketDataProvider(
            daily={symbol: daily for symbol in symbols},
            intraday={symbol: provider._intraday["TEST"] for symbol in symbols},
        )
        report = runner.run_scanners(symbols=symbols, provider=wide, trading_day=DAY)
        outcome = [o for o in report.outcomes if o.scanner_name == "accumulation"][0]
        assert outcome.exceptions == 2  # S7 and S17
        assert not outcome.failed
        assert outcome.symbols_seen == 20

    def test_a_scanner_that_will_not_construct_is_reported_and_skipped(self, provider,
                                                                       monkeypatch):
        from scanners import registry

        monkeypatch.setitem(registry.SCANNER_SPECS, "orb",
                            ("scanners.orb.nonexistent_module", "Nope"))
        report = runner.run_scanners(symbols=["TEST"], provider=provider, trading_day=DAY)
        assert "orb" in report.construction_failures
        assert len(report.outcomes) == 5

    def test_a_symbol_the_provider_cannot_serve_is_skipped(self, provider):
        report = runner.run_scanners(
            symbols=["TEST", "MISSING"], provider=provider, trading_day=DAY)
        assert report.fetch_failures == 1
        assert report.signal_count > 0

    def test_an_unexpected_fetch_error_is_absorbed(self):
        class Exploding(StaticMarketDataProvider):
            def get_daily_bars(self, symbol, lookback_days=400):
                raise ValueError("upstream returned nonsense")

        report = runner.run_scanners(
            symbols=["TEST"], provider=Exploding(), trading_day=DAY)
        assert report.fetch_failures == 1
        assert report.signal_count == 0

    def test_returns_a_report_rather_than_raising_on_partial_failure(self, provider,
                                                                    monkeypatch):
        monkeypatch.setattr(result_store, "write_signals",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        report = runner.run_scanners(symbols=["TEST"], provider=provider, trading_day=DAY)
        assert report.stored_signals == 0
        assert report.signal_count > 0


class TestFetchOnce:
    def test_bars_are_fetched_once_per_symbol_not_once_per_scanner(self, provider):
        calls = []

        class Counting(StaticMarketDataProvider):
            def get_daily_bars(self, symbol, lookback_days=400):
                calls.append(symbol)
                return super().get_daily_bars(symbol, lookback_days=lookback_days)

        counting = Counting(daily=provider._daily, intraday=provider._intraday)
        runner.run_scanners(symbols=["TEST"], provider=counting, trading_day=DAY)
        assert calls == ["TEST"], "six scanners must share one fetch"

    def test_every_scanner_sees_the_same_timestamp(self, provider):
        """Otherwise two scanners' "+1h return" for one symbol would
        cover different hours."""
        report = runner.run_scanners(symbols=["TEST"], provider=provider, trading_day=DAY)
        stamps = {signal.timestamp for outcome in report.outcomes
                  for signal in outcome.signals}
        assert len(stamps) == 1


class TestStorage:
    def test_signals_are_written_to_the_analytics_store(self, provider):
        report = runner.run_scanners(symbols=["TEST"], provider=provider, trading_day=DAY)
        stored = result_store.read_signals(DAY)
        assert len(stored) == report.stored_signals > 0

    def test_the_same_symbol_is_stored_once_per_scanner(self, provider):
        """Section 6: duplicates across scanners are the point."""
        runner.run_scanners(symbols=["TEST"], provider=provider, trading_day=DAY)
        stored = result_store.read_signals(DAY)
        assert len(stored) >= 2
        assert {signal.symbol for signal in stored} == {"TEST"}
        assert len({signal.scanner_name for signal in stored}) == len(stored)

    def test_no_store_writes_nothing(self, provider):
        runner.run_scanners(symbols=["TEST"], provider=provider, trading_day=DAY,
                            store=False)
        assert result_store.read_signals(DAY) == []

    def test_a_run_manifest_records_versions_and_fingerprints(self, provider):
        """Sections 11 and 19: the claim that parameters never moved is
        only checkable if every run wrote down what it ran with."""
        runner.run_scanners(symbols=["TEST"], provider=provider, trading_day=DAY)
        manifests = result_store.read_run_manifests(DAY)
        assert len(manifests) == 1
        scanners = manifests[0]["scanners"]
        assert len(scanners) == 6
        for entry in scanners:
            assert entry["scanner_version"]
            assert entry["config_fingerprint"]

    def test_every_stored_signal_carries_its_config_fingerprint(self, provider):
        runner.run_scanners(symbols=["TEST"], provider=provider, trading_day=DAY)
        for signal in result_store.read_signals(DAY):
            assert signal.metrics.get("config_fingerprint")


class TestProfilesAndUniverse:
    def test_profiles_cover_all_six_scanners(self):
        assert set(runner.PROFILES["all"]) == set(
            runner.PROFILES["daily"]) | set(runner.PROFILES["intraday"])

    def test_a_profile_runs_only_its_scanners(self, provider):
        report = runner.run_scanners(
            scanners=runner.PROFILES["daily"], symbols=["TEST"], provider=provider,
            trading_day=DAY)
        assert {o.scanner_name for o in report.outcomes} == set(runner.PROFILES["daily"])

    def test_an_unavailable_universe_is_reported_not_raised(self, monkeypatch, provider):
        monkeypatch.setenv("SCANNER_UNIVERSE_FILE", "/nonexistent/universe.csv")
        report = runner.run_scanners(provider=provider, trading_day=DAY)
        assert report.skipped_reason and "universe" in report.skipped_reason

    def test_universe_reader_honours_the_tradable_column(self, tmp_path):
        path = tmp_path / "universe.csv"
        path.write_text("symbol,tradable\nAAA,True\nBBB,False\nCCC,True\n")
        assert load_symbols(path=path) == ["AAA", "CCC"]
        assert load_symbols(path=path, tradable_only=False) == ["AAA", "BBB", "CCC"]

    def test_universe_reader_deduplicates_and_preserves_order(self, tmp_path):
        path = tmp_path / "universe.csv"
        path.write_text("symbol,tradable\nZZZ,True\naaa,True\nZZZ,True\n")
        assert load_symbols(path=path) == ["ZZZ", "AAA"]

    def test_universe_reader_refuses_a_file_with_no_symbol_column(self, tmp_path):
        path = tmp_path / "universe.csv"
        path.write_text("name,tradable\nfoo,True\n")
        with pytest.raises(UniverseUnavailable, match="symbol column"):
            load_symbols(path=path)

    def test_limit_selects_the_same_slice_every_run(self, tmp_path):
        path = tmp_path / "universe.csv"
        path.write_text("symbol,tradable\n" + "".join(f"S{i},True\n" for i in range(50)))
        assert load_symbols(path=path, limit=5) == load_symbols(path=path, limit=5)


class TestCliGate:
    @pytest.fixture(autouse=True)
    def isolated_candidate_store(self, tmp_path, monkeypatch):
        """A candidate store of this class's own, never the shared one.

        `main` takes the publishing scanners' cycle locks before it
        reaches anything these tests monkeypatch, and `candidate_dir`
        resolves those locks from the environment. On a host where
        `TRADING_PROJECT_ROOT` points at a release that is the LIVE
        shared store, so a production scan holding one publishing
        scanner's lock made `main` return 0 for a refused overlap --
        correct behaviour, measured against a precondition this class
        never established. It failed in the full suite and passed in
        isolation purely because the two runs landed either side of a
        long daily scan.

        The reverse direction is the worse one: without this, the tests
        take real cycle locks, and a live scan can stand down for a test
        run. Overlap semantics are pinned in
        `tests/test_s6_long_scan_overlap.py`; what belongs here is the
        exit code a failed scanner produces.
        """
        store = tmp_path / "handoff"
        store.mkdir()
        monkeypatch.setenv(publisher.CANDIDATE_DIR_ENV, str(store))

    def test_the_market_calendar_gate_stops_a_holiday_run(self, monkeypatch):
        import market_guard

        called = []
        monkeypatch.setattr(market_guard, "is_us_trading_day", lambda *a, **k: False)
        monkeypatch.setattr(runner, "run_scanners",
                            lambda **kwargs: called.append(kwargs))
        assert runner.main([]) == 0
        assert called == []

    def test_a_failed_scanner_produces_a_nonzero_exit(self, monkeypatch, provider):
        import market_guard

        monkeypatch.setattr(market_guard, "is_us_trading_day", lambda *a, **k: True)

        def failing(**kwargs):
            report = runner.RunReport(trading_day=DAY, started_at="now",
                                      provider="static", universe_size=1)
            outcome = type("O", (), {})()
            report.construction_failures = {"orb": "boom"}
            return report

        monkeypatch.setattr(runner, "run_scanners", failing)
        assert runner.main([]) == 1


class TestCliGateIsolationFromLiveScans:
    """The CLI gate's exit code must not depend on what else is running.

    This reproduces the failure directly rather than asserting the fix:
    it holds the cycle locks a production scan holds, leaves
    `SCANNER_CANDIDATE_DIR` pointing at that store in the environment
    the way a release host does, and runs the exit-code test as a
    subprocess. Before the isolation fixture the child resolved the
    ambient store, found the locks held, and `main` returned 0 for a
    refused overlap -- the exact `assert 0 == 1` seen on the host.
    """

    def test_a_held_cycle_lock_cannot_change_the_cli_exit_code(
            self, tmp_path, monkeypatch):
        import os
        import subprocess
        from contextlib import ExitStack

        from scanners.base import scan_session
        from scanners.base.trading_calendar import us_trading_day
        from scanners.publish import scan_cycle

        store = tmp_path / "shared-store"
        store.mkdir()
        monkeypatch.setenv(publisher.CANDIDATE_DIR_ENV, str(store))

        day = us_trading_day()
        names = sorted(runner.PUBLISHING_SCANNERS)
        target = (f"{Path(__file__).name}::TestCliGate"
                  "::test_a_failed_scanner_produces_a_nonzero_exit")

        # Every session, not just the current one: the child resolves its
        # own session moments later, and a boundary crossed mid-test
        # would otherwise silently hold the wrong lock and prove nothing.
        with ExitStack() as holding:
            for session in scan_session.SESSIONS:
                cycle = holding.enter_context(
                    scan_cycle.hold_all(day, session, scanners=names))
                assert cycle.acquired, f"could not hold {session} locks"

            environment = dict(os.environ)
            environment[publisher.CANDIDATE_DIR_ENV] = str(store)
            finished = subprocess.run(
                [sys.executable, "-m", "pytest", f"tests/{target}",
                 "-q", "-p", "no:randomly", "--no-header"],
                cwd=str(REPO_ROOT), env=environment,
                capture_output=True, text=True, timeout=600)

        assert finished.returncode == 0, (
            "the CLI exit-code test changed its answer because another "
            f"scan held the cycle locks:\n{finished.stdout}")
