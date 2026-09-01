"""Section 28's required cases, run against all six scanners.

    normal PASS / normal FAIL      -> tests/test_scanners_behaviour.py
    insufficient data              -> here
    NaN                            -> here
    zero volume                    -> here
    empty API response             -> here
    stale data                     -> here
    market holiday                 -> here
    missing bar                    -> here

Parametrised over every scanner rather than written once for one of
them, because the failure these prevent is scanner-specific: it is
always the newest scanner, or the one whose author assumed the frames
would be well-formed, that divides by a NaN in production.

The contract being pinned is the same for all six and is exactly what
section 28's last two lines require:

    bad data  -> ScannerDataError or a clean rejection, never a crash
    a crash   -> absorbed by the framework, never reaching the caller

`scan()` is used for the strongest assertions because it exercises the
isolation path a production run actually takes -- a test that only
called `evaluate()` would prove the scanner raises tidily without
proving the framework survives it.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scanners.base.market_data_provider import (  # noqa: E402
    MarketDataUnavailable,
    StaticMarketDataProvider,
    SymbolData,
)
from scanners.base.models import ScannerDataError  # noqa: E402
from scanners.registry import ALL_SCANNERS, build_scanner  # noqa: E402
from tests import scanner_fixtures as fx  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_candidate_store(tmp_path, monkeypatch):
    """A candidate store of this module's own, never the shared one.

    `runner.main` takes the publishing scanners' cycle locks before it
    reaches anything these tests control, and `candidate_dir` resolves
    those locks from the environment. On a release host that is the LIVE
    store: on 2026-09-01 a host gate run collided with the live `orb`
    scan --

        [SCAN CYCLE] skipped -- orb: a orb scan started at
        2026-09-01T06:17:29 (pid 3697118) is still running

    -- and the test read a refusal as its own result. These files never
    went red for it only because they assert == 0, which is also what a
    refused overlap returns; they were insensitive, not correct.

    The direction that matters more is the other one: without this, a
    test run can take the live S6 cycle lock and stand a real scan down.
    """
    store = tmp_path / "candidates"
    store.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SCANNER_CANDIDATE_DIR", str(store))


DAY = "2026-08-12"

pytestmark = pytest.mark.parametrize("scanner_name", ALL_SCANNERS)


def scan_one(scanner_name, bundle):
    """Run one bundle through the real isolation path."""
    scanner = build_scanner(scanner_name)
    return scanner.scan([bundle], trading_day=DAY)


def assert_survived_without_crashing(outcome):
    """No signal, no unhandled exception, and the run stayed usable."""
    assert outcome.exceptions == 0, outcome.error_samples
    assert outcome.signals == []
    assert outcome.symbols_seen == 1


class TestInsufficientData:
    def test_a_symbol_with_too_few_bars_fails_cleanly(self, scanner_name):
        """An IPO from three weeks ago cannot have an HMA200. That has to
        be an explicit, logged data failure -- not a comparison against
        NaN that reports itself as a market judgement."""
        bundle = SymbolData(symbol="NEW", daily=fx.daily_frame(np.linspace(10, 12, 40)))
        outcome = scan_one(scanner_name, bundle)
        assert_survived_without_crashing(outcome)
        assert outcome.data_errors == 1

    def test_exactly_one_bar_short_of_hma200_still_fails(self, scanner_name):
        from scanners.base.features import minimum_daily_bars

        count = minimum_daily_bars() - 1
        bundle = SymbolData(symbol="EDGE", daily=fx.daily_frame(np.linspace(10, 40, count)))
        outcome = scan_one(scanner_name, bundle)
        assert_survived_without_crashing(outcome)
        assert outcome.data_errors == 1


class TestNaN:
    def test_nan_closes_do_not_crash_a_scan(self, scanner_name):
        closes = fx.accelerating_uptrend().astype(float)
        closes[-1] = np.nan
        closes[-5] = np.nan
        bundle = SymbolData(
            symbol="NANNY",
            daily=fx.daily_frame(np.nan_to_num(closes, nan=0.0)),
            intraday=fx.orb_session(base=100.0),
        )
        # Rebuild with genuine NaN (daily_frame derives High/Low from Close).
        bundle.daily["Close"] = closes
        outcome = scan_one(scanner_name, bundle)
        assert outcome.exceptions == 0, outcome.error_samples

    def test_an_all_nan_frame_fails_cleanly(self, scanner_name):
        frame = fx.daily_frame(np.linspace(10, 40, 320))
        for column in ("Open", "High", "Low", "Close"):
            frame[column] = np.nan
        outcome = scan_one(scanner_name, SymbolData(symbol="EMPTYISH", daily=frame))
        assert_survived_without_crashing(outcome)

    def test_nan_never_reaches_a_stored_signal(self, scanner_name):
        """Whatever else happens, the dataset must not contain NaN."""
        import math

        from scanners.base.models import NUMERIC_FIELDS

        bundle = _passing_bundle(scanner_name)
        signal = build_scanner(scanner_name).evaluate(bundle, trading_day=DAY)
        assert signal is not None
        for field in NUMERIC_FIELDS:
            value = getattr(signal, field)
            assert value is None or math.isfinite(value), field


class TestZeroVolume:
    def test_zero_volume_throughout_yields_none_never_inf(self, scanner_name):
        """A halted name: every bar has zero volume.

        The contract is NOT "no signal". S1 and S3 have no volume
        condition at all -- price above a rising HMA200 with ADX over 20
        is a true statement about a halted name's price history, and
        inventing a volume floor for them here would be a filter the
        spec does not ask for and would quietly change what month 1
        measures.

        What must hold is that the divide-by-zero produces `None`, not
        `inf`: `volume_multiple = volume / avg_volume` with a zero
        denominator is the exact case section S2 calls out, and an `inf`
        reaching the stored dataset would poison every average computed
        over it.
        """
        closes = fx.accelerating_uptrend()
        bundle = SymbolData(
            symbol="HALTED",
            daily=fx.daily_frame(closes, volumes=np.zeros(len(closes))),
            intraday=fx.intraday_frame(np.full(60, 100.0), volumes=np.zeros(60)),
        )
        outcome = scan_one(scanner_name, bundle)
        assert outcome.exceptions == 0, outcome.error_samples
        for signal in outcome.signals:
            assert signal.volume_multiple is None
            assert signal.avg_volume is None

    def test_the_volume_scanners_reject_a_halted_name(self, scanner_name):
        """The two scanners whose conditions DO rest on volume must
        refuse it rather than pass on an unmeasurable ratio."""
        if scanner_name not in ("accumulation", "orb"):
            pytest.skip(f"{scanner_name} has no volume condition")
        closes = fx.accelerating_uptrend()
        bundle = SymbolData(
            symbol="HALTED",
            daily=fx.daily_frame(closes, volumes=np.zeros(len(closes))),
            intraday=fx.orb_session(base=float(closes[-1]),
                                    range_volume=0.0, post_volume=0.0),
        )
        outcome = scan_one(scanner_name, bundle)
        assert outcome.exceptions == 0, outcome.error_samples
        assert outcome.signals == []

    def test_a_single_zero_volume_bar_does_not_crash(self, scanner_name):
        closes = fx.accelerating_uptrend()
        volumes = np.full(len(closes), 1_000_000.0)
        volumes[-1] = 0.0
        bundle = SymbolData(symbol="QUIET", daily=fx.daily_frame(closes, volumes=volumes))
        outcome = scan_one(scanner_name, bundle)
        assert outcome.exceptions == 0, outcome.error_samples


class TestEmptyApiResponse:
    def test_an_empty_daily_frame_fails_cleanly(self, scanner_name):
        bundle = SymbolData(symbol="GONE", daily=pd.DataFrame())
        outcome = scan_one(scanner_name, bundle)
        assert_survived_without_crashing(outcome)
        assert outcome.data_errors == 1

    def test_a_none_daily_frame_fails_cleanly(self, scanner_name):
        outcome = scan_one(scanner_name, SymbolData(symbol="GONE", daily=None))
        assert_survived_without_crashing(outcome)
        assert outcome.data_errors == 1

    def test_the_provider_refusing_does_not_reach_the_scanner(self, scanner_name):
        """`get_symbol_data` raising is the runner's problem, and the
        runner skips the symbol -- the scanner is never handed a
        half-built bundle."""
        provider = StaticMarketDataProvider()
        with pytest.raises(MarketDataUnavailable):
            provider.get_symbol_data("NOTHING")


class TestStaleData:
    def test_month_old_bars_are_refused(self, scanner_name):
        """A scan must never judge a symbol from last month's prices and
        record the verdict as today's."""
        import datetime as dt

        old = fx.daily_frame(fx.accelerating_uptrend(),
                             end=dt.date.today() - dt.timedelta(days=45))
        outcome = scan_one(scanner_name, SymbolData(symbol="STALE", daily=old))
        assert_survived_without_crashing(outcome)
        assert outcome.data_errors == 1


class TestMarketHoliday:
    def test_the_runner_skips_a_closed_market(self, scanner_name, monkeypatch):
        """Section 28's holiday case, at the only place it can be
        enforced: a holiday scan would otherwise write a day of signals
        for a session that never traded, and every forward return
        computed from them would be measured across a gap."""
        import market_guard

        from scanners import runner

        monkeypatch.setattr(market_guard, "is_us_trading_day", lambda *a, **k: False)
        assert runner.main(["--scanners", scanner_name]) == 0

    def test_the_calendar_gate_can_be_bypassed_for_backfill(self, scanner_name, monkeypatch,
                                                            tmp_path):
        """`--ignore-market-calendar` reaches the scan instead of
        short-circuiting on the calendar.

        The assertion is on the RUN STATUS, not on the exit code. The
        universe here is a single nonexistent symbol, so every fetch
        fails and the run correctly reports `FAILED_PROVIDER` (and exits
        non-zero) -- which is the section 14 behaviour, not a bug. What
        this test is about is that the calendar gate was bypassed, and
        the evidence for that is a run that got far enough to have a
        provider verdict at all: a gated run never reaches the provider
        and returns `SKIPPED_MARKET_CLOSED`.
        """
        import market_guard

        from scanners import runner
        from scanners.base import run_context

        monkeypatch.setattr(market_guard, "is_us_trading_day", lambda *a, **k: False)
        universe = tmp_path / "universe.csv"
        universe.write_text("symbol,tradable\nNONESUCH,True\n")
        monkeypatch.setenv("SCANNER_UNIVERSE_FILE", str(universe))

        captured = {}
        original = runner.run_scanners

        def record(**kwargs):
            report = original(**kwargs)
            captured["report"] = report
            return report

        monkeypatch.setattr(runner, "run_scanners", record)
        runner.main(["--scanners", scanner_name, "--ignore-market-calendar", "--no-store"])

        report = captured["report"]
        assert report.status != run_context.SKIPPED_MARKET_CLOSED
        assert report.universe_size == 1, "the scan was reached"

    def test_the_calendar_gate_stops_the_run_without_it(self, scanner_name, monkeypatch,
                                                        tmp_path):
        """The other half: without the flag, nothing is scanned at all."""
        import market_guard

        from scanners import runner

        monkeypatch.setattr(market_guard, "is_us_trading_day", lambda *a, **k: False)
        called = []
        monkeypatch.setattr(runner, "run_scanners",
                            lambda **kwargs: called.append(kwargs))
        assert runner.main(["--scanners", scanner_name]) == 0
        assert called == []


class TestMissingBars:
    def test_a_gap_in_the_daily_history_does_not_crash(self, scanner_name):
        """A missing session, as happens after a halt."""
        frame = fx.daily_frame(fx.accelerating_uptrend())
        frame = frame.drop(frame.index[[50, 51, 120, 200]])
        outcome = scan_one(scanner_name, SymbolData(symbol="GAPPY", daily=frame))
        assert outcome.exceptions == 0, outcome.error_samples

    def test_a_missing_ohlc_column_fails_cleanly(self, scanner_name):
        frame = fx.daily_frame(fx.accelerating_uptrend()).drop(columns=["High"])
        outcome = scan_one(scanner_name, SymbolData(symbol="PARTIAL", daily=frame))
        assert outcome.exceptions == 0, outcome.error_samples

    def test_a_one_bar_intraday_session_does_not_crash(self, scanner_name):
        bundle = SymbolData(
            symbol="THIN",
            daily=fx.daily_frame(fx.accelerating_uptrend()),
            intraday=fx.intraday_frame(np.array([100.0])),
        )
        outcome = scan_one(scanner_name, bundle)
        assert outcome.exceptions == 0, outcome.error_samples

    def test_an_empty_intraday_frame_does_not_crash(self, scanner_name):
        bundle = SymbolData(
            symbol="NOINTRA",
            daily=fx.daily_frame(fx.accelerating_uptrend()),
            intraday=pd.DataFrame(),
        )
        outcome = scan_one(scanner_name, bundle)
        assert outcome.exceptions == 0, outcome.error_samples


class TestErrorsNeverEscape:
    def test_a_scanner_crash_is_absorbed_by_the_framework(self, scanner_name, monkeypatch):
        """Section 28's closing requirement: a scanner error must not
        propagate towards the order system. It cannot even reach the
        runner's caller."""
        scanner = build_scanner(scanner_name)
        monkeypatch.setattr(
            type(scanner), "check",
            lambda self, features, data, context: (_ for _ in ()).throw(
                RuntimeError("synthetic explosion")))
        outcome = scanner.scan([_passing_bundle(scanner_name)], trading_day=DAY)
        assert outcome.exceptions == 1
        assert outcome.signals == []

    def test_a_storage_failure_does_not_lose_the_other_scanners(self, scanner_name,
                                                                monkeypatch):
        from scanners import runner
        from scanners.base import result_store

        def explode(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(result_store, "write_signals", explode)
        provider = _provider_for(scanner_name)
        report = runner.run_scanners(
            scanners=[scanner_name], symbols=["TEST"], provider=provider,
            trading_day=DAY)
        # The run completed and reported the failure rather than raising.
        assert report.stored_signals == 0
        assert any(outcome.failed for outcome in report.outcomes)


def _passing_bundle(scanner_name):
    if scanner_name == "accumulation":
        return fx.uptrend_bundle(volumes=fx.volume_surge())
    if scanner_name == "breakout_ready":
        return SymbolData(symbol="TEST", daily=fx.daily_frame(fx.coiled_under_high()))
    if scanner_name == "premarket_momentum":
        return fx.premarket_momentum_bundle()
    if scanner_name == "gap_pullback":
        return fx.gap_pullback_bundle()
    if scanner_name == "orb":
        return fx.orb_bundle()
    return fx.uptrend_bundle()


def _provider_for(scanner_name):
    bundle = _passing_bundle(scanner_name)
    return StaticMarketDataProvider(
        daily={"TEST": bundle.daily},
        intraday={"TEST": bundle.intraday} if bundle.intraday is not None else None,
    )
