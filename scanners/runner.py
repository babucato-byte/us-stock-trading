"""Runs the scanners. The piece section 5's isolation requirement is about.

Loop order: symbol-major
------------------------
    for symbol in universe:          # fetch bars ONCE
        for scanner in scanners:     # offer the same bars to all six
            scanner.evaluate(...)

Not the other way round, for two reasons.

Bars are fetched once per symbol rather than once per (symbol, scanner),
which is a six-fold reduction in provider calls over an 800-name
universe -- the difference between a scan that finishes and one that
gets rate-limited.

And every scanner judges a symbol from byte-identical data at the same
instant. Section 17's intersection analysis asks whether the names two
scanners BOTH flagged did better; if the two had scanned minutes apart
from separate fetches, that analysis would be partly measuring the gap
between two downloads.

The reverse order would also hold the whole universe's minute bars in
memory at once. This way one symbol's bundle is discarded before the
next is fetched.

Three layers of isolation
-------------------------
    fetch fails        -> that symbol is skipped for all scanners
    evaluate raises    -> that symbol is skipped for THAT scanner only
    a scanner explodes -> that scanner is marked failed; the rest finish

The third is the one section 5 states outright, and it is the reason
`_run_scanner_safely` wraps the per-scanner storage too: a scanner whose
results could not be written must not prevent the other five from
writing theirs.

What this does NOT do
---------------------
It does not place orders, size positions, evaluate risk, touch the kill
switch, or write to the trading candidate store. It writes to the
analytics store and to per-scanner logs. Section 30: adding these
scanners is not a live-trading change, and there is no code path from
here to an order.
"""

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from scanners.base import activity as act
from scanners.base import eligibility as elig
from scanners.base import result_store, run_context
from scanners.base.trading_calendar import us_trading_day
from scanners.base.features import build_features
from scanners.base.market_data_provider import (
    BarMarketDataProvider,
    MarketDataUnavailable,
    SymbolData,
    default_provider,
)
from scanners.base.models import ScannerDataError
from scanners.base.scanner_base import ScanOutcome, count_reject_reason
from scanners.base.scanner_logging import get_scanner_logger, log_decision
from scanners.registry import ALL_SCANNERS, DAILY_SCANNERS, INTRADAY_SCANNERS, build_scanners
from scanners.universe import UniverseUnavailable, load_symbols

logger = logging.getLogger(__name__)

#: Consecutive per-symbol exceptions before a scanner is declared broken
#: and dropped for the rest of the run.
#:
#: `evaluate_into` absorbs a per-symbol failure by design -- one bad
#: ticker must not cost a scanner its other 799 evaluations. But that
#: same guarantee means a scanner that is broken FOR EVERY SYMBOL (a bad
#: config value, a typo in a scoring expression) fails 800 times
#: quietly: 800 tracebacks in its log, 800 wasted evaluations, and a
#: summary that says `failed=False` because no single failure was ever
#: fatal. The systemic problem gets reported as 800 unrelated ones.
#:
#: A run of consecutive failures is the signal that distinguishes the
#: two. A handful of malformed symbols in an 800-name universe is
#: normal and the counter resets on the first symbol that produces any
#: ordinary outcome -- a signal, a rejection, or a data error. Twenty-
#: five in a row without one is not a data problem.
MAX_CONSECUTIVE_SCANNER_ERRORS = 25

#: Fraction of the universe that must fail to FETCH before the run is
#: downgraded from SUCCESS to PARTIAL.
#:
#: Some fetch failures are normal: an 800-name universe always contains
#: a few delisted or halted tickers, and flagging those would make every
#: healthy run look degraded. But when half the universe never reaches a
#: scanner, the day's signal counts cover a fraction of the intended
#: symbols and are not comparable with a healthy day's -- averaging them
#: into the month as if they were would understate every scanner's
#: activity for that day without leaving a trace.
PROVIDER_DEGRADED_FRACTION = 0.5

#: Named scanner groups for the scheduler, matching how the six are meant
#: to be run through the trading day (see the runbook in docs/SCANNERS.md).
PROFILES = {
    "all": list(ALL_SCANNERS),
    "daily": list(DAILY_SCANNERS),
    "intraday": list(INTRADAY_SCANNERS),
    "premarket": ["premarket_momentum"],
    "open": ["orb", "gap_pullback"],
}

#: Which universe each profile draws from by default (section 13).
#:
#: `daily` walks the full universe after the close -- it has the time,
#: and it is what populates the activity ranking the others depend on.
#: The intraday profiles draw from that ranking instead, because the
#: ORB window is minutes wide and a full-universe pass takes hours: the
#: answer would arrive after the setup it describes had already
#: resolved.
PROFILE_UNIVERSE = {
    "all": "full",
    "daily": "full",
    "intraday": "active",
    "premarket": "active",
    "open": "active",
}

UNIVERSE_FULL = "full"
UNIVERSE_ACTIVE = "active"


@dataclass
class RunReport:
    """What one invocation of the runner did, per scanner."""

    trading_day: str
    started_at: str
    provider: str
    universe_size: int
    run_id: Optional[str] = None
    profile: Optional[str] = None
    provider_feed: Optional[str] = None
    outcomes: List[ScanOutcome] = field(default_factory=list)
    fetch_failures: int = 0
    fetch_failure_samples: List[str] = field(default_factory=list)
    #: Symbols the provider would not serve. Tracked in full (not just
    #: the sampled messages) because eligibility needs every one of them
    #: to record a recheck date.
    fetch_failed_symbols: List[str] = field(default_factory=list)
    construction_failures: Dict[str, str] = field(default_factory=dict)
    stored_signals: int = 0
    duration_seconds: float = 0.0
    skipped_reason: Optional[str] = None
    #: Set only when the run could not proceed at all. A run that
    #: completed derives its status from the outcomes instead.
    terminal_status: Optional[str] = None
    #: Which universe this run drew from: "full" or "active".
    universe_type: Optional[str] = None
    activity_summary: Dict[str, Any] = field(default_factory=dict)
    #: Symbols never fetched because a current eligibility record ruled
    #: them out. Reported so a shrinking universe is visible rather than
    #: looking like a quiet market.
    skipped_ineligible: int = 0
    eligibility_summary: Dict[str, Any] = field(default_factory=dict)
    required_history_bars: int = 0

    @property
    def signal_count(self) -> int:
        return sum(len(outcome.signals) for outcome in self.outcomes)

    @property
    def status(self) -> str:
        """The run's own status (spec section 14).

        Ordering matters. A terminal status set during startup wins,
        because at that point no scanner ran and the outcome list is
        empty -- deriving from it would report SUCCESS for a run that
        never happened, which is the single failure section 14 exists to
        prevent.

        After that: every scanner failed is FAILED, some failed is
        PARTIAL, none failed is SUCCESS.
        """
        if self.terminal_status:
            return self.terminal_status
        if not self.outcomes:
            return run_context.FAILED

        # Provider health is checked BEFORE scanner health, and this
        # order is the whole point of the property.
        #
        # When the provider fails for every symbol, no scanner is ever
        # invoked: `symbols_seen` stays 0, no outcome is marked failed,
        # and a scanner-derived status reports SUCCESS with zero
        # candidates. That is exactly the confusion section 14 exists to
        # prevent -- a total data outage recorded as "the market offered
        # nothing today", indistinguishable in the month-1 dataset from
        # a genuinely quiet session.
        if self.universe_size and self.fetch_failures >= self.universe_size:
            return run_context.FAILED_PROVIDER
        if (self.universe_size
                and self.fetch_failures >= self.universe_size * PROVIDER_DEGRADED_FRACTION):
            # Most of the universe never reached a scanner. Whatever the
            # scanners did produce covers a fraction of the intended
            # symbols, so the day's counts are not comparable with a
            # healthy day's and must not be read as one.
            return run_context.PARTIAL

        failed = [outcome for outcome in self.outcomes if outcome.failed]
        if failed and len(failed) == len(self.outcomes):
            return run_context.FAILED
        if failed or self.construction_failures:
            return run_context.PARTIAL
        return run_context.SUCCESS

    @property
    def candidate_count(self) -> Optional[int]:
        return run_context.candidate_count_for(self.status, self.signal_count)

    @property
    def consecutive_error_peak(self) -> int:
        return max((outcome.consecutive_error_peak for outcome in self.outcomes),
                   default=0)

    @property
    def circuit_breaker_triggered(self) -> bool:
        return any(outcome.circuit_breaker_triggered for outcome in self.outcomes)

    def to_manifest(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "profile": self.profile,
            "trading_day": self.trading_day,
            "started_at": self.started_at,
            "run_status": self.status,
            "candidate_count": self.candidate_count,
            "provider": self.provider,
            "market_data_provider": self.provider,
            "market_data_feed": self.provider_feed,
            "universe_size": self.universe_size,
            "universe_type": self.universe_type,
            "activity": dict(self.activity_summary),
            "skipped_ineligible": self.skipped_ineligible,
            "eligibility": dict(self.eligibility_summary),
            "required_history_bars": self.required_history_bars,
            "provider_error_count": self.fetch_failures,
            "fetch_failures": self.fetch_failures,
            "fetch_failure_samples": self.fetch_failure_samples[:5],
            "consecutive_error_peak": self.consecutive_error_peak,
            "circuit_breaker_triggered": self.circuit_breaker_triggered,
            "circuit_breaker_reason": "; ".join(
                outcome.circuit_breaker_reason for outcome in self.outcomes
                if outcome.circuit_breaker_reason) or None,
            "construction_failures": dict(self.construction_failures),
            "stored_signals": self.stored_signals,
            "duration_seconds": round(self.duration_seconds, 3),
            "skipped_reason": self.skipped_reason,
            "scanners": [outcome.summary() for outcome in self.outcomes],
        }


def _symbol_bundles(
    provider: BarMarketDataProvider,
    symbols: Iterable[str],
    *,
    report: RunReport,
    daily_lookback_days: int,
    intraday_interval: str,
    intraday_lookback_days: int,
    want_intraday: bool,
) -> Iterable[SymbolData]:
    """Yield one bundle per symbol, skipping the ones that cannot be fetched.

    A generator, not a list: this is what keeps memory flat across an
    800-name universe, since each bundle is released once every scanner
    has seen it.
    """
    for symbol in symbols:
        try:
            yield provider.get_symbol_data(
                symbol,
                daily_lookback_days=daily_lookback_days,
                intraday_interval=intraday_interval,
                intraday_lookback_days=intraday_lookback_days,
                want_premarket=want_intraday,
            )
        except MarketDataUnavailable as exc:
            report.fetch_failures += 1
            report.fetch_failed_symbols.append(symbol)
            if len(report.fetch_failure_samples) < 20:
                report.fetch_failure_samples.append(str(exc))
            # Expected outcome for a delisted or unlisted ticker: logged
            # as a line, not a traceback. At 13k symbols the traceback
            # form buries anything real (section 22).
            logger.debug("skipping %s: %s", symbol, exc)
        except Exception as exc:  # noqa: BLE001 - a bad symbol must not end the run
            report.fetch_failures += 1
            report.fetch_failed_symbols.append(symbol)
            if len(report.fetch_failure_samples) < 20:
                report.fetch_failure_samples.append(f"{symbol}: {type(exc).__name__}: {exc}")
            logger.exception("unexpected fetch failure for %s", symbol)


def run_scanners(
    *,
    scanners: Optional[List[str]] = None,
    symbols: Optional[List[str]] = None,
    limit: Optional[int] = None,
    provider: Optional[BarMarketDataProvider] = None,
    trading_day: Optional[str] = None,
    store: bool = True,
    daily_lookback_days: int = 400,
    intraday_interval: str = "1m",
    intraday_lookback_days: int = 5,
    profile: Optional[str] = None,
    run_id: Optional[str] = None,
    use_eligibility: bool = True,
    universe_type: Optional[str] = None,
    active_pool_size: int = act.DEFAULT_POOL_SIZE,
) -> RunReport:
    """Run the requested scanners over the requested symbols.

    Returns a report rather than raising on partial failure. A scan that
    lost one scanner still produced five scanners' worth of data, and
    the caller (a cron job) needs to store that and then report the
    failure -- not lose the day.
    """
    day = trading_day or us_trading_day()
    # UNCACHED, deliberately.
    #
    # `CachingMarketDataProvider` memoises every fetch for the lifetime of
    # the provider and never evicts. That is right for the performance
    # tracker, where one symbol carries several signals and the cache
    # measurably saves fetches -- but this loop is symbol-major and
    # fetches each symbol exactly once, so there is nothing to hit.
    #
    # Measured over 200/500/1000/3000 symbols, cache-on vs cache-off with
    # an identical symbol set:
    #
    #     cache hits            0 at every size
    #     entries == misses     == provider calls (397/987/1950/5900)
    #     provider calls        identical with and without the cache
    #     RSS growth      on    107.7 KB/symbol   116.8 -> 411.3 MB
    #                     off     2.9 KB/symbol    89.7 ->  97.6 MB
    #
    # So the cache bought nothing here and charged ~108 KB per symbol.
    # Extrapolated to the 13,362-symbol universe that is ~1.5 GB against
    # a 956 MB server -- the full daily scan would have been killed part
    # way through, which is the failure this line prevents.
    provider = provider or default_provider(cached=False)
    started = time.monotonic()
    # Minted here, before anything can fail, so a run that dies during
    # startup still has an identity in the run log. Section 5: never
    # reused across invocations.
    identifier = run_id or run_context.new_run_id(day, profile)
    report = RunReport(
        trading_day=day,
        started_at=datetime.now(timezone.utc).isoformat(),
        provider=getattr(provider, "provider_name", None)
        or getattr(provider, "name", type(provider).__name__),
        provider_feed=getattr(provider, "feed_name", None),
        universe_size=0,
        run_id=identifier,
        profile=profile,
    )

    requested = list(scanners or ALL_SCANNERS)
    built = build_scanners(
        requested,
        on_error=lambda name, exc: report.construction_failures.__setitem__(
            name, f"{type(exc).__name__}: {exc}"),
    )
    if not built:
        report.skipped_reason = "no scanner could be constructed"
        report.terminal_status = run_context.FAILED_NO_SCANNER
        report.duration_seconds = time.monotonic() - started
        logger.error("no scanner could be constructed; nothing to run")
        _record_manifest(report, day)
        return report

    explicit_symbols = symbols
    activity_store = (act.ActivityStore.load(report.provider) if use_eligibility
                      else act.NullActivityStore(report.provider))
    selected_universe = universe_type or PROFILE_UNIVERSE.get(profile or "", UNIVERSE_FULL)
    report.universe_type = selected_universe if symbols is None else "explicit"

    if symbols is None:
        if selected_universe == UNIVERSE_ACTIVE:
            symbols = activity_store.active_symbols(limit=limit or active_pool_size)
            if not symbols:
                # An empty pool means the daily run has not populated the
                # ranking yet -- an operational fact, not a market with
                # no active names. Reported as a failure so it cannot be
                # mistaken for a quiet session (section 14).
                report.skipped_reason = (
                    "no active universe available; run the daily profile first "
                    "to populate the activity ranking")
                report.terminal_status = run_context.FAILED_NO_UNIVERSE
                report.duration_seconds = time.monotonic() - started
                logger.error("%s", report.skipped_reason)
                _record_manifest(report, day)
                return report
            logger.info("active universe: %s symbols (pool limit %s)",
                        len(symbols), limit or active_pool_size)
        else:
            try:
                symbols = load_symbols(limit=limit)
            except UniverseUnavailable as exc:
                report.skipped_reason = f"universe unavailable: {exc}"
                report.terminal_status = run_context.FAILED_NO_UNIVERSE
                report.duration_seconds = time.monotonic() - started
                logger.error("universe unavailable: %s", exc)
                _record_manifest(report, day)
                return report
    elif limit:
        symbols = symbols[:limit]
    report.universe_size = len(symbols)
    report.required_history_bars = max(
        (scanner.required_history for scanner in built), default=0)

    # Eligibility: drop symbols a CURRENT record says cannot be judged,
    # before any of them costs a network round trip. Section 6 -- this is
    # a data-availability filter only; nothing strategy-shaped reaches
    # it, so it cannot change which symbols pass a scanner condition,
    # only which ones were worth asking about.
    eligibility_store = (elig.EligibilityStore.load(report.provider)
                         if use_eligibility
                         else elig.NullEligibilityStore(report.provider))
    # Explicitly named symbols are NEVER filtered. `--symbols AAPL,MSFT`
    # is an instruction to scan those two, and silently dropping one
    # because a cache from last week says it had short history would
    # make the flag untrustworthy exactly when it is used -- debugging a
    # specific name. The cache still LEARNS from such a run; it just
    # does not gate it.
    if use_eligibility and explicit_symbols is None:
        keep = eligibility_store.eligible_symbols(symbols)
        report.skipped_ineligible = len(symbols) - len(keep)
        if report.skipped_ineligible:
            logger.info("eligibility: skipping %s of %s symbols with a current "
                        "ineligible record", report.skipped_ineligible, len(symbols))
        symbols = keep
        report.universe_size = len(symbols)

    want_intraday = any(getattr(scanner, "requires_intraday", False) for scanner in built)
    # One timestamp for the whole run. Every forward return in section
    # 12 is measured from a signal's timestamp, so letting it drift
    # across a 20-minute scan would mean two scanners' "+1h return" for
    # the same symbol covered different hours.
    stamp = datetime.now(timezone.utc).isoformat()

    outcomes = {scanner.scanner_name: scanner.new_outcome(day) for scanner in built}
    consecutive_errors = {scanner.scanner_name: 0 for scanner in built}
    for bundle in _symbol_bundles(
        provider, symbols, report=report,
        daily_lookback_days=daily_lookback_days,
        intraday_interval=intraday_interval,
        intraday_lookback_days=intraday_lookback_days,
        want_intraday=want_intraday,
    ):
        # ONE feature pass per symbol, shared by every scanner in the run.
        #
        # Each scanner used to call `build_features` itself, so the daily
        # profile computed the same HMA89, HMA200 and ADX three times
        # over -- identical inputs, identical outputs, 0.90 s of HMA per
        # repetition on the server. Sharing is not only cheaper: section
        # 17's intersection analysis rests on every scanner having judged
        # a symbol from the same numbers, and one pass makes that true by
        # construction rather than by coincidence.
        #
        # A failure here belongs to the SYMBOL, not to any one scanner,
        # so it is recorded against all of them exactly as the per-scanner
        # path used to record it.
        shared_features = None
        feature_error = None
        try:
            shared_features = build_features(bundle)
        except ScannerDataError as exc:
            feature_error = exc
        except Exception as exc:  # noqa: BLE001 - unexpected: keep it per-scanner
            feature_error = exc

        # Remember what this symbol turned out to be, so the next run
        # does not pay to rediscover it.
        if isinstance(feature_error, ScannerDataError):
            reason = elig.classify_data_error(str(feature_error))
            eligibility_store.note_ineligible(
                bundle.symbol, reason,
                history_bars=(0 if bundle.daily is None else len(bundle.daily)),
                required_bars=report.required_history_bars,
                detail=str(feature_error)[:200])
        elif feature_error is None:
            eligibility_store.note_eligible(
                bundle.symbol,
                history_bars=(0 if bundle.daily is None else len(bundle.daily)),
                required_bars=report.required_history_bars)
            # Liquidity for the intraday pool, taken from the feature
            # pass that already computed it. Only the full-universe
            # profile updates the ranking -- an intraday run sees only
            # the pool it was given, so letting it write would shrink
            # the ranking to itself, run after run, until nothing else
            # could ever re-enter.
            if selected_universe == UNIVERSE_FULL and shared_features is not None:
                activity_store.note(bundle.symbol, trading_day=day,
                                    price=shared_features.price,
                                    avg_volume=shared_features.avg_volume)

        for scanner in built:
            name = scanner.scanner_name
            outcome = outcomes[name]
            if outcome.failed:
                continue
            errors_before = outcome.exceptions
            try:
                if feature_error is not None:
                    raise feature_error
                scanner.evaluate_into(outcome, bundle, trading_day=day, timestamp=stamp,
                                      run_id=identifier,
                                      shared_features=shared_features)
            except ScannerDataError as exc:
                # The shared pass could not build features for this
                # symbol. Counted and logged exactly as the per-scanner
                # path did, so the run summary is unchanged by the
                # sharing (section 28: a data shortfall is not a
                # rejection and not a scanner fault).
                outcome.data_errors += 1
                count_reject_reason(outcome.reject_reasons, "insufficient_or_stale_data")
                outcome.symbols_seen += 1
                log_decision(scanner.log, scanner=name, version=scanner.version,
                             symbol=bundle.symbol, result="FAIL", reason=str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 - section 5: isolate the scanner
                # `evaluate_into` already absorbs per-symbol failures, so
                # reaching here means the scanner instance itself is
                # broken. Mark it done and keep whatever it produced --
                # the other five carry on.
                outcome.failed = True
                outcome.failure_reason = f"{type(exc).__name__}: {exc}"
                logger.exception("scanner %s failed and was disabled for this run", name)
                continue

            if outcome.exceptions > errors_before:
                consecutive_errors[name] += 1
                outcome.consecutive_error_peak = max(
                    outcome.consecutive_error_peak, consecutive_errors[name])
                if consecutive_errors[name] >= MAX_CONSECUTIVE_SCANNER_ERRORS:
                    outcome.failed = True
                    outcome.circuit_breaker_triggered = True
                    outcome.circuit_breaker_reason = (
                        f"{consecutive_errors[name]} consecutive symbol failures; "
                        f"treating the scanner as broken rather than the data. "
                        f"Last: {outcome.error_samples[-1] if outcome.error_samples else 'n/a'}")
                    outcome.failure_reason = outcome.circuit_breaker_reason
                    logger.error("scanner %s disabled for this run: %s",
                                 name, outcome.circuit_breaker_reason)
            else:
                consecutive_errors[name] = 0

    report.outcomes = [outcomes[scanner.scanner_name] for scanner in built]

    for symbol in report.fetch_failed_symbols:
        # A provider refusal is treated as TRANSIENT (short recheck).
        # Believing it durably would let one bad afternoon at the vendor
        # evict a large slice of the universe for a month.
        eligibility_store.note_ineligible(symbol, elig.PROVIDER_UNAVAILABLE,
                              required_bars=report.required_history_bars)
    report.eligibility_summary = eligibility_store.summary()
    report.activity_summary = activity_store.summary()
    eligibility_store.save()
    activity_store.save()

    if store:
        for outcome in report.outcomes:
            report.stored_signals += _store_safely(outcome, day)
        _record_manifest(report, day)

    report.duration_seconds = time.monotonic() - started
    _log_summary(report)
    return report


def _store_safely(outcome: ScanOutcome, trading_day: str) -> int:
    """Persist one scanner's signals; never let a storage failure spread.

    Section 5's isolation is about results, not just execution: a
    scanner whose write fails must not cost the other five their day's
    data, which is what would happen if this raised out of the loop.
    """
    if not outcome.signals:
        return 0
    try:
        return result_store.write_signals(outcome.signals, trading_day=trading_day)
    except Exception as exc:  # noqa: BLE001
        outcome.failed = True
        outcome.failure_reason = f"storage failed: {type(exc).__name__}: {exc}"
        logger.exception("could not store signals for %s", outcome.scanner_name)
        return 0


def _record_manifest(report: RunReport, trading_day: str) -> None:
    try:
        result_store.write_run_manifest(report.to_manifest(), trading_day=trading_day)
    except Exception:  # noqa: BLE001 - the manifest is an audit aid, not the data
        logger.exception("could not write the run manifest for %s", trading_day)


def _log_summary(report: RunReport) -> None:
    for outcome in report.outcomes:
        log = get_scanner_logger(outcome.scanner_name)
        log.info("run summary %s", outcome.summary())
    logger.info(
        "scanner run complete day=%s universe=%s signals=%s stored=%s "
        "fetch_failures=%s duration=%.1fs",
        report.trading_day, report.universe_size, report.signal_count,
        report.stored_signals, report.fetch_failures, report.duration_seconds)


def print_report(report: RunReport) -> None:
    print(f"run id          : {report.run_id}")
    print(f"profile         : {report.profile or '(explicit scanner list)'}")
    print(f"trading day     : {report.trading_day}")
    print(f"run status      : {report.status}")
    print(f"candidate count : "
          f"{'null (run did not complete)' if report.candidate_count is None else report.candidate_count}")
    print(f"provider        : {report.provider}"
          + (f" feed={report.provider_feed}" if report.provider_feed else " feed=null"))
    print(f"universe        : {report.universe_size} symbols"
          + (f" ({report.universe_type})" if report.universe_type else ""))
    if report.skipped_ineligible:
        print(f"skipped (inelig): {report.skipped_ineligible} symbols with a current "
              f"ineligible record")
    if report.skipped_reason:
        print(f"SKIPPED         : {report.skipped_reason}")
    for name, reason in report.construction_failures.items():
        print(f"NOT BUILT       : {name} -- {reason}")
    print(f"fetch failures  : {report.fetch_failures}")
    print("")
    header = (f"{'scanner':22} {'version':28} {'status':>8} {'signals':>7} "
              f"{'rejected':>8} {'data_err':>8} {'errors':>7}")
    print(header)
    print("-" * len(header))
    for outcome in report.outcomes:
        count = outcome.candidate_count
        print(f"{outcome.scanner_name:22} {outcome.scanner_version:28} "
              f"{outcome.status:>8} {('null' if count is None else str(count)):>7} "
              f"{outcome.rejected:8} {outcome.data_errors:8} {outcome.exceptions:7}")
        if outcome.failed:
            print(f"    reason: {outcome.failure_reason}")
        if outcome.circuit_breaker_triggered:
            print(f"    circuit breaker: TRIGGERED after "
                  f"{outcome.consecutive_error_peak} consecutive failures")
    print("")
    print(f"stored signals  : {report.stored_signals}")
    print(f"duration        : {report.duration_seconds:.1f}s")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the independent scanners and record their signals. "
                    "Never places, sizes, or authorises an order.")
    parser.add_argument("--profile", choices=sorted(PROFILES), default=None,
                        help="named scanner group (all/daily/intraday/premarket/open)")
    parser.add_argument("--scanners", default="",
                        help="comma-separated scanner names; overrides --profile")
    parser.add_argument("--symbols", default="",
                        help="comma-separated symbols; defaults to universe.csv")
    parser.add_argument("--limit", type=int, default=None,
                        help="scan at most this many universe symbols")
    parser.add_argument("--trading-day", default=None,
                        help="override the trading day label (YYYY-MM-DD)")
    parser.add_argument("--no-store", action="store_true",
                        help="evaluate and print without writing to the analytics store")
    parser.add_argument("--ignore-market-calendar", action="store_true",
                        help="run even when the US market is closed (backfill/testing)")
    parser.add_argument("--universe", choices=[UNIVERSE_FULL, UNIVERSE_ACTIVE], default=None,
                        help="which universe to draw from; defaults to the profile's "
                             "own (daily=full, premarket/open=active)")
    parser.add_argument("--active-pool-size", type=int, default=act.DEFAULT_POOL_SIZE,
                        help=f"symbols in the intraday active pool (default "
                             f"{act.DEFAULT_POOL_SIZE})")
    parser.add_argument("--no-eligibility", action="store_true",
                        help="ignore the eligibility and activity caches for this run")
    parser.add_argument("--intraday-interval", default="1m")
    parser.add_argument("--intraday-lookback-days", type=int, default=5)
    parser.add_argument("--daily-lookback-days", type=int, default=400)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if not args.ignore_market_calendar:
        # Same gate the existing scanner entry points use, so a holiday
        # cannot quietly produce a day of signals that never traded and
        # contaminate the month-1 dataset (section 28's market-holiday
        # case).
        from market_guard import is_us_trading_day

        if not is_us_trading_day():
            # Recorded, not just printed. Section 14: a closed market is
            # neither a success with zero candidates nor a failure, and
            # month 1 needs to be able to tell "we did not scan" from
            # "we scanned and found nothing" when a day is missing from
            # the signal files.
            day = args.trading_day or us_trading_day()
            skipped = RunReport(
                trading_day=day,
                started_at=datetime.now(timezone.utc).isoformat(),
                provider="n/a",
                universe_size=0,
                run_id=run_context.new_run_id(day, args.profile),
                profile=args.profile,
                terminal_status=run_context.SKIPPED_MARKET_CLOSED,
                skipped_reason="US market closed",
            )
            _record_manifest(skipped, day)
            print("[MARKET GUARD] NYSE closed. Scanner run skipped.")
            # Deliberately no notification: a closed market is a correct
            # no-op, and alerting on it would fire on every holiday.
            return 0

    names = [item.strip() for item in args.scanners.split(",") if item.strip()]
    if not names and args.profile:
        names = PROFILES[args.profile]
    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()] or None

    report = run_scanners(
        scanners=names or None,
        symbols=symbols,
        limit=args.limit,
        trading_day=args.trading_day,
        store=not args.no_store,
        daily_lookback_days=args.daily_lookback_days,
        intraday_interval=args.intraday_interval,
        intraday_lookback_days=args.intraday_lookback_days,
        profile=args.profile,
        universe_type=args.universe,
        active_pool_size=args.active_pool_size,
        use_eligibility=not args.no_eligibility,
    )
    print_report(report)
    # A scanner that failed is an operational problem worth a non-zero
    # exit so cron/systemd surfaces it -- but only after the other
    # scanners' results have been stored.
    exit_code = 0 if report.status == run_context.SUCCESS else 1
    # Notification LAST, and after the exit code is already decided, so
    # that a Slack outage cannot influence what this process reports.
    # `notify_run` swallows its own exceptions; the guard here is for
    # the import itself, which must not be able to fail a scan either.
    try:
        from scanners.notify import slack as notify

        notify.notify_run(report)
    except Exception:  # noqa: BLE001 - see scanners/notify/slack.py
        logger.warning("scanner notification could not be attempted", exc_info=True)
    # The monitor channel is separate from the alert channel and reports
    # EVERY run, quiet ones included -- see scanners/notify/monitor.py for
    # why the two cannot share a policy. Same placement and same guard: it
    # runs after the exit code is decided and cannot change it.
    try:
        from scanners.notify import monitor

        monitor.notify_run(report)
    except Exception:  # noqa: BLE001 - a monitor must never fail a scan
        logger.warning("scanner monitor could not be attempted", exc_info=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
