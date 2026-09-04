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
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from scanners.base import activity as act
from scanners.base import eligibility as elig
from scanners.base import result_store, run_context, scan_session
from scanners.base import intraday_supplement
from scanners.base import universe_selection as universe_sel
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
#: Symbols chosen by the SCANNER NODE from today's whole-market data.
#: See discovery/manifest.py. Falls back to the active ranking when the
#: manifest is missing, stale or malformed -- the trading node must not
#: take a laptop's uptime as a dependency of its own discovery.
UNIVERSE_MANIFEST = "manifest"

#: Where the trading node looks for the scanner node's manifest. The
#: scanner node writes it here over scp; nothing else writes to it.
MANIFEST_DEFAULT_PATH = "shared/state/discovery/manifest.json"


@dataclass
class RunReport:
    """What one invocation of the runner did, per scanner."""

    trading_day: str
    started_at: str
    provider: str
    universe_size: int
    run_id: Optional[str] = None
    profile: Optional[str] = None
    #: Which of the four clock sessions this run belongs to. A label
    #: on the run, never a condition: see scanners/base/scan_session.
    session: Optional[str] = None
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
    #: What happened to the candidate hand-off. None when this run had no
    #: publishing scanner in it and there was nothing to hand off.
    #:
    #: Carried on the run report, not only in a log line, because the run
    #: manifest is the only record that survives -- and a producer that
    #: could not reach the shared store must not be reconstructable later
    #: as "the scan ran and found nothing".
    publication_status: Optional[str] = None
    publication_detail: Optional[str] = None
    published_rows: int = 0
    #: Symbols never fetched because a current eligibility record ruled
    #: them out. Reported so a shrinking universe is visible rather than
    #: looking like a quiet market.
    skipped_ineligible: int = 0
    #: How the scan universe was filled: requested vs selected, how deep
    #: into the ranking it walked, and how many the supplement added.
    #: On the manifest so a shrinking universe is a number in the record
    #: rather than something reconstructed from a log line.
    universe_selection: Dict[str, Any] = field(default_factory=dict)
    #: How the scanner node's manifest was judged this run, and how old
    #: it was. On the manifest so "we fell back" is a recorded fact.
    manifest_status: Optional[str] = None
    manifest_detail: Optional[str] = None
    manifest_age_seconds: Optional[float] = None
    eligibility_summary: Dict[str, Any] = field(default_factory=dict)
    required_history_bars: int = 0
    # Passive scan diagnostics.  These records are intentionally kept out of
    # candidate rows: observability must not alter the hand-off contract.
    symbol_timings: List[Dict[str, Any]] = field(default_factory=list)

    def timing_summary(self) -> Dict[str, Any]:
        def values(name):
            return sorted(float(row[name]) for row in self.symbol_timings
                          if row.get(name) is not None)
        def percentiles(items):
            if not items:
                return {"p50_ms": None, "p90_ms": None, "p95_ms": None, "p99_ms": None, "max_ms": None}
            def pick(p):
                return items[min(len(items) - 1, int((len(items) - 1) * p))]
            return {"p50_ms": round(pick(.50), 3), "p90_ms": round(pick(.90), 3),
                    "p95_ms": round(pick(.95), 3),
                    "p99_ms": round(pick(.99), 3), "max_ms": round(items[-1], 3)}
        acquisition = values("acquisition_elapsed_ms")
        total = values("total_symbol_elapsed_ms")
        return {"symbol_count": len(self.symbol_timings), "request_count": len(acquisition),
                "symbol_total": percentiles(total), "kis_acquisition": percentiles(acquisition),
                "limiter_wait_total_ms": "NOT_SEPARABLE",
                "network_time_total_ms": "NOT_SEPARABLE",
                "local_processing_total_ms": round(sum(
                    (row.get("feature_eval_elapsed_ms") or 0.0) for row in self.symbol_timings), 3)}

    @property
    def timing_enabled(self) -> bool:
        return (str(self.provider).lower() == "kis" and
                str(self.session).upper() in {"PREMARKET", "AFTER_HOURS", "OVERNIGHT_DAYTIME"})

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
            "publication_status": self.publication_status,
            "publication_detail": self.publication_detail,
            "published_rows": self.published_rows,
            "stored_signals": self.stored_signals,
            "duration_seconds": round(self.duration_seconds, 3),
            "skipped_reason": self.skipped_reason,
            "universe_selection": dict(self.universe_selection),
            "manifest_status": self.manifest_status,
            "manifest_detail": self.manifest_detail,
            "manifest_age_seconds": self.manifest_age_seconds,
            **({"timing": self.timing_summary()} if self.timing_enabled else {}),
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
        record = ({"symbol": symbol, "request_start": datetime.now(timezone.utc).isoformat(),
                  "limiter_wait_ms": "NOT_SEPARABLE", "network_broker_elapsed_ms": "NOT_SEPARABLE",
                  "response_parse_elapsed_ms": "NOT_SEPARABLE", "session_slice_elapsed_ms": "NOT_SEPARABLE",
                  "orb_eval_elapsed_ms": None, "feature_eval_elapsed_ms": None,
                  "result": "data_error"} if report.timing_enabled else None)
        began = time.perf_counter()
        try:
            bundle = provider.get_symbol_data(
                symbol,
                daily_lookback_days=daily_lookback_days,
                intraday_interval=intraday_interval,
                intraday_lookback_days=intraday_lookback_days,
                want_premarket=want_intraday,
            )
            if record is not None:
                record["request_end"] = datetime.now(timezone.utc).isoformat()
                record["acquisition_elapsed_ms"] = round((time.perf_counter() - began) * 1000.0, 3)
                report.symbol_timings.append(record)
            yield bundle
        except MarketDataUnavailable as exc:
            if record is not None:
                record["request_end"] = datetime.now(timezone.utc).isoformat()
                record["acquisition_elapsed_ms"] = round((time.perf_counter() - began) * 1000.0, 3)
                report.symbol_timings.append(record)
            report.fetch_failures += 1
            report.fetch_failed_symbols.append(symbol)
            if len(report.fetch_failure_samples) < 20:
                report.fetch_failure_samples.append(str(exc))
            # Expected outcome for a delisted or unlisted ticker: logged
            # as a line, not a traceback. At 13k symbols the traceback
            # form buries anything real (section 22).
            logger.debug("skipping %s: %s", symbol, exc)
        except Exception as exc:  # noqa: BLE001 - a bad symbol must not end the run
            if record is not None:
                record["request_end"] = datetime.now(timezone.utc).isoformat()
                record["acquisition_elapsed_ms"] = round((time.perf_counter() - began) * 1000.0, 3)
                report.symbol_timings.append(record)
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
    session: Optional[str] = None,
    run_id: Optional[str] = None,
    use_eligibility: bool = True,
    universe_type: Optional[str] = None,
    active_pool_size: int = act.DEFAULT_POOL_SIZE,
    supplement_size: int = intraday_supplement.DEFAULT_SUPPLEMENT_SIZE,
    manifest_path: Optional[str] = None,
    publish: bool = False,
) -> RunReport:
    """Run the requested scanners over the requested symbols.

    Returns a report rather than raising on partial failure. A scan that
    lost one scanner still produced five scanners' worth of data, and
    the caller (a cron job) needs to store that and then report the
    failure -- not lose the day.
    """
    # Two different questions, deliberately answered differently.
    #
    # `scanned_session` is what the REPORT says the run covered, and
    # falls back to the current session so a report is never sessionless.
    #
    # `requested_session` is what the SCANNERS are told, and falls back
    # to nothing. A caller that named a session gets that session; a
    # caller that named none keeps the previous behaviour exactly --
    # ORB's REGULAR branch. Defaulting this one to the wall clock would
    # make the same call judge OVERNIGHT_DAYTIME at 3am and REGULAR at
    # 10am, which is a verdict decided by when a cron happened to fire
    # rather than by what was asked for.
    #
    # Production always passes --session explicitly, so the session-aware
    # branch -- unreachable until now, because nothing ever put a session
    # in the scanner's context -- becomes live where it matters.
    scanned_session = scan_session.normalize(session) or scan_session.session_at()
    requested_session = scan_session.normalize(session)
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
    # The provider is INJECTED, never chosen here. In the extended
    # sessions the entrypoint supplies a KIS-backed one, because the
    # default has no usable extended-hours intraday data -- S6's
    # premarket scan on 2026-08-31 read universe 83, DATA_ERROR 77,
    # evaluated 6, signals 0.
    #
    # Selecting it here would mean this module importing a broker, and
    # `tests/test_scanner_trading_isolation.py` forbids that for a good
    # reason: an import that does not exist cannot be reached by a path
    # nobody thought of. The scanner observes; it does not get the
    # capability to trade in order to read a bar.
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
        # An explicit session wins; otherwise it is read off the clock.
        # A caller-supplied name that is not one of the four is NOT
        # silently corrected -- `normalize` returns None and the clock
        # answers instead, because a typo quietly becoming REGULAR would
        # file an off-hours scan under the one session allowed to trade.
        session=scanned_session,
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

    # Loaded BEFORE the universe is chosen, because the active universe
    # is now filled TO its pool size with eligible names rather than
    # filtered down from it -- see scanners/base/universe_selection.
    eligibility_store = (elig.EligibilityStore.load(report.provider)
                         if use_eligibility
                         else elig.NullEligibilityStore(report.provider))
    universe_selection = None

    if symbols is None and selected_universe == UNIVERSE_MANIFEST:
        from discovery import manifest as manifest_module

        verdict = manifest_module.validate(
            manifest_module.read(manifest_path or MANIFEST_DEFAULT_PATH),
            trading_day=day)
        report.manifest_status = verdict["status"]
        report.manifest_detail = verdict["detail"]
        report.manifest_age_seconds = verdict.get("age_seconds")
        # PARTIAL is usable. The provider throttles a full-market pass
        # part-way, and a ranking from part of today's market is still a
        # better answer than all of yesterday's -- which is the exact
        # staleness the manifest exists to replace. It is labelled, not
        # refused, so the run record shows what it was drawn from.
        if verdict["status"] in (manifest_module.VALID,
                                 manifest_module.PARTIAL):
            symbols = [str(r["symbol"]).upper() for r in verdict["symbols"]]
            report.universe_type = UNIVERSE_MANIFEST
            logger.info("manifest universe: %s symbols (%s)",
                        len(symbols), verdict["detail"])
        else:
            # Every non-VALID verdict falls back to the server's own
            # ranking. A stale laptop must degrade this scan to what it
            # would have been anyway, never stop it -- and never let it
            # trade on symbols nobody re-derived today.
            logger.warning("manifest unusable (%s: %s); falling back to the "
                           "active ranking", verdict["status"], verdict["detail"])
            selected_universe = UNIVERSE_ACTIVE
            report.universe_type = UNIVERSE_ACTIVE

    if symbols is None:
        if selected_universe == UNIVERSE_ACTIVE:
            pool = limit or active_pool_size
            universe_selection = universe_sel.eligible_top(
                activity_store, eligibility_store, limit=pool)
            symbols = list(universe_selection.symbols)
            report.skipped_ineligible = universe_selection.skipped_ineligible
            if symbols and supplement_size > 0:
                # Yesterday's ranking cannot know what moved this
                # morning. Bounded and cached per session -- see
                # scanners/base/intraday_supplement -- and additive
                # only: it changes which symbols are LOOKED AT, never
                # what any of them has to do to become a candidate.
                extra = intraday_supplement.load_or_build(
                    provider, activity_store, eligibility_store,
                    trading_day=day, session=report.session,
                    already=symbols, cut=pool, size=supplement_size)
                universe_sel.merge_supplement(universe_selection, extra,
                                              limit=supplement_size)
                symbols = list(universe_selection.symbols)
            report.universe_selection = universe_selection.summary()
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
            logger.info("active universe: %s of %s eligible symbols "
                        "(considered %s, skipped %s ineligible, depth %s)",
                        len(symbols), pool, universe_selection.considered,
                        universe_selection.skipped_ineligible,
                        universe_selection.depth_reached)
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
    #
    # The ACTIVE universe has already been filled with eligible names, so
    # filtering it again would remove nothing and only re-walk the store.
    # Explicitly named symbols are NEVER filtered. `--symbols AAPL,MSFT`
    # is an instruction to scan those two, and silently dropping one
    # because a cache from last week says it had short history would
    # make the flag untrustworthy exactly when it is used -- debugging a
    # specific name. The cache still LEARNS from such a run; it just
    # does not gate it.
    if use_eligibility and explicit_symbols is None and universe_selection is None:
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
        timing = (next((row for row in reversed(report.symbol_timings)
                        if row["symbol"] == bundle.symbol and "total_symbol_elapsed_ms" not in row), None)
                  if report.timing_enabled else None)
        symbol_started = time.perf_counter()
        shared_features = None
        feature_error = None
        try:
            shared_features = build_features(bundle)
        except ScannerDataError as exc:
            feature_error = exc
        except Exception as exc:  # noqa: BLE001 - unexpected: keep it per-scanner
            feature_error = exc
        if timing is not None:
            timing["feature_eval_elapsed_ms"] = round((time.perf_counter() - symbol_started) * 1000.0, 3)

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
                eval_started = time.perf_counter()
                if feature_error is not None:
                    raise feature_error
                # The session being scanned, passed rather than left for
                # the scanner to default. Without it ORB resolved
                # `context.get("session") or "REGULAR"` to REGULAR on
                # every run and judged the regular session no matter
                # which one was requested.
                scanner.evaluate_into(outcome, bundle, trading_day=day, timestamp=stamp,
                                      run_id=identifier,
                                      shared_features=shared_features,
                                      session=requested_session)
                if timing is not None and name == "orb":
                    timing["orb_eval_elapsed_ms"] = round((time.perf_counter() - eval_started) * 1000.0, 3)
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
        if timing is not None:
            timing["result"] = ("data_error" if feature_error is not None else
                                ("candidate" if any(s.symbol == bundle.symbol for o in outcomes.values()
                                                    for s in o.signals) else "rejected"))
            timing["total_symbol_elapsed_ms"] = round((time.perf_counter() - symbol_started) * 1000.0 + timing.get("acquisition_elapsed_ms", 0.0), 3)

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

    # Stamped BEFORE the manifest is written, not after. The manifest is
    # the only record of this run that survives, and it recorded 0.0 for
    # every scan because the duration was set on the line after it.
    report.duration_seconds = time.monotonic() - started

    if store:
        for outcome in report.outcomes:
            report.stored_signals += _store_safely(outcome, day)
            _record_rejects_safely(outcome, day, report.session,
                                   universe_selection)
        # Publication happens BEFORE the manifest, so its outcome is IN
        # the manifest. One row per run: appending a second, amended row
        # would double-count in every reader that tallies run statuses.
        if publish:
            _publish_safely(report)
        _record_manifest(report, day)

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


def _record_rejects_safely(outcome: ScanOutcome, trading_day: str,
                           session: Optional[str], selection=None) -> None:
    """Persist why each symbol was refused; never let that failure spread.

    The scan itself is unaffected by whether this succeeds -- an
    observability write that could fail a trading-day scan would be a
    strictly worse trade than the blindness it fixes.

    One line per rejected symbol, and the line is short by design:
    symbol, the gate code, and the two numbers behind it. A 202-symbol
    scan every 15 minutes over a 6.5-hour session is roughly 5,000
    rows a day at about 70 bytes each -- a third of a megabyte, readable
    with grep. Writing the intermediate calculations instead would be
    megabytes an hour and would answer no question these fields do not.
    """
    rows = getattr(outcome, "first_rejects", None)
    if not rows:
        return
    try:
        stamp = datetime.now(timezone.utc).isoformat()
        directory = result_store.analytics_dir() / "rejects"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{trading_day}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                # Provenance travels with the rejection, so a month from
                # now "did the supplement find anything the ranking
                # missed" is a query over these rows rather than an
                # argument. Without it, PREVIOUS_DAY_ONLY and
                # INTRADAY_SUPPLEMENT are indistinguishable after the
                # fact.
                record = {"timestamp": stamp, "session": session,
                          "scanner": outcome.scanner_name, **row}
                if selection is not None:
                    record["universe_source"] = selection.source_of(row["symbol"])
                    record["activity_rank"] = selection.rank_of(row["symbol"])
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception:  # noqa: BLE001 - never costs the scan its results
        logger.warning("could not record reject reasons for %s",
                       outcome.scanner_name, exc_info=True)


def _publish_safely(report: RunReport) -> None:
    """Hand the candidates over, and record how that went.

    Never raises -- but never silently succeeds either. The three
    outcomes are kept apart because they demand different responses:

        NOT_APPLICABLE       this run had no publishing scanner
        PRODUCER_CONFIG_ERROR the shared store could not be located
        PUBLICATION_WRITE_FAILED it was found and the write failed

    The middle one used to be impossible to observe. `candidate_dir()`
    guessed a runtime-local path, `publish()` wrote there happily, and
    the run recorded SUCCESS -- so a producer writing where no consumer
    looks was indistinguishable from a market with nothing to offer.
    """
    if not any(PUBLISHING_SCANNERS.get(str(getattr(o, "scanner_name", None)))
               for o in report.outcomes or []):
        report.publication_status = run_context.PUBLICATION_NOT_APPLICABLE
        return

    from scanners.publish.candidates import CandidateHandoffMisconfigured

    try:
        report.published_rows = publish_report_candidates(report)
    except CandidateHandoffMisconfigured as exc:
        report.publication_status = run_context.PUBLICATION_CONFIG_ERROR
        report.publication_detail = str(exc)
        logger.error("candidate hand-off is misconfigured; %s published "
                     "NOTHING: %s",
                     ", ".join(sorted(PUBLISHING_SCANNERS)), exc)
        return
    except Exception as exc:  # noqa: BLE001 - a scan must survive a failed
        # write; the signals are already in the analytics store.
        report.publication_status = run_context.PUBLICATION_WRITE_FAILED
        report.publication_detail = f"{type(exc).__name__}: {exc}"
        logger.warning("candidate publication failed", exc_info=True)
        return
    report.publication_status = run_context.PUBLICATION_OK


def _record_manifest(report: RunReport, trading_day: str) -> None:
    try:
        result_store.write_run_manifest(report.to_manifest(), trading_day=trading_day)
        # Bounded: one diagnostic file per immutable run id, at most one row
        # per scanned symbol; no request payloads, headers, or credentials.
        if report.timing_enabled:
            directory = result_store.analytics_dir() / "timing"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{trading_day}_{report.run_id}.jsonl"
            with path.open("w", encoding="utf-8") as handle:
                for row in report.symbol_timings:
                    handle.write(json.dumps(row, sort_keys=True) + "\n")
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
    _log_data_error_summary(report.outcomes)


def _log_data_error_summary(outcomes) -> None:
    """One aggregate line per scan: why the DATA_ERRORs happened.

    The count alone could not distinguish a thin overnight market from a
    fetch path that had stopped working. On 2026-09-02 six consecutive
    scans reported DATA_ERROR on 592 of 593 symbols with `rejected=0` --
    the strategy was never consulted -- and the number was equally
    consistent with either. Both had happened before, days apart, and
    they call for opposite responses.

    Aggregate, not per symbol: six hundred lines a scan is how a log
    stops being read.
    """
    from scanners.base import reject_reasons

    for outcome in outcomes or ():
        reasons = getattr(outcome, "data_error_reasons", None)
        if not reasons:
            continue
        total = sum(reasons.values())
        acquisition = sum(count for reason, count in reasons.items()
                          if reject_reasons.is_acquisition_failure(reason))
        ordered = " ".join(
            f"{reason}={reasons[reason]}"
            for reason in reject_reasons.DATA_ERROR_CATEGORIES
            if reasons.get(reason))
        logger.info(
            "DATA_ERROR_SUMMARY scanner=%s total=%s acquisition_failures=%s "
            "insufficient_data=%s %s",
            outcome.scanner_name, total, acquisition, total - acquisition,
            ordered)


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
    if report.publication_status:
        print(f"candidate hand-off: {report.publication_status}"
              + (f" rows={report.published_rows}"
                 if report.publication_status == run_context.PUBLICATION_OK
                 else ""))
        if report.publication_detail:
            print(f"    {report.publication_detail}")
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
    parser.add_argument("--session", choices=list(scan_session.SESSIONS),
                        help="scan session label; defaults to the ET clock")
    parser.add_argument("--trading-day", default=None,
                        help="override the trading day label (YYYY-MM-DD)")
    parser.add_argument("--no-store", action="store_true",
                        help="evaluate and print without writing to the analytics store")
    parser.add_argument("--ignore-market-calendar", action="store_true",
                        help="run even when the US market is closed (backfill/testing)")
    parser.add_argument("--universe",
                        choices=[UNIVERSE_FULL, UNIVERSE_ACTIVE,
                                 UNIVERSE_MANIFEST], default=None,
                        help="which universe to draw from; defaults to the profile's "
                             "own (daily=full, premarket/open=active)")
    parser.add_argument("--manifest-path", default=None,
                        help="the scanner node's candidate manifest; used "
                             "with --universe manifest")
    parser.add_argument("--supplement-size", type=int,
                        default=intraday_supplement.DEFAULT_SUPPLEMENT_SIZE,
                        help="how many intraday-active names may join the "
                             "active universe (0 disables the supplement)")
    parser.add_argument("--active-pool-size", type=int, default=act.DEFAULT_POOL_SIZE,
                        help=f"symbols in the intraday active pool (default "
                             f"{act.DEFAULT_POOL_SIZE})")
    parser.add_argument("--no-eligibility", action="store_true",
                        help="ignore the eligibility and activity caches for this run")
    parser.add_argument("--intraday-interval", default="1m")
    parser.add_argument("--intraday-lookback-days", type=int, default=5)
    parser.add_argument("--daily-lookback-days", type=int, default=400)
    return parser.parse_args(argv)


#: Which scanners publish a candidate file. Publication is a hand-off to
#: the trading runtime, and a hand-off is only worth writing for a
#: strategy that has a consumer on the other side. S3..S6 are
#: DISCOVERY_ONLY with nothing reading them, so publishing for them would
#: create files whose only reader is a future misunderstanding.
PUBLISHING_SCANNERS = {
    "hma_early_trend": "S1_HMA_EARLY_TREND_V1",
    "accumulation": "S2_VOLUME_ACCUMULATION_V1",
    "orb": "S6_ORB_BREAKOUT_V1",
}




def publish_report_candidates(report) -> int:
    """Write one candidate file per publishing scanner. Returns rows written.

    Only scanners that SUCCEEDED publish. A failed scanner's signal list
    is a partial answer -- it stopped somewhere -- and a partial answer
    written into the hand-off file is indistinguishable from a complete
    one once the run is over.

    Raises `CandidateHandoffMisconfigured` when the shared store cannot be
    located. That is deliberately NOT swallowed here: a publishing scan
    whose hand-off has nowhere to go has not "found nothing", and the
    caller records it as a producer failure rather than as a quiet
    session.
    """
    from scanners.publish import candidates as candidate_publisher
    from scanners.publish import scan_cycle

    day = getattr(report, "trading_day", None)
    session = getattr(report, "session", None)
    run_id = getattr(report, "run_id", None)
    started_at = getattr(report, "started_at", None)
    completed_at = datetime.now(timezone.utc).isoformat()
    duration = getattr(report, "duration_seconds", None)

    publishing = [o for o in getattr(report, "outcomes", None) or []
                  if PUBLISHING_SCANNERS.get(str(getattr(o, "scanner_name", None)))]
    if not publishing:
        return 0

    # Resolved ONCE, before anything is written, and allowed to raise.
    # `mark_run` and `publish` are both best-effort by design -- a failed
    # write must not cost a scan its signals -- which means a
    # misconfigured store would otherwise produce nothing but two log
    # warnings and a return value of 0. Indistinguishable from a quiet
    # session, which is the whole failure.
    from scanners.publish import generations

    directory = candidate_publisher.candidate_dir()
    logger.info("candidate hand-off directory: %s", directory)

    written = 0
    for outcome in publishing:
        name = getattr(outcome, "scanner_name", None)
        strategy_id = PUBLISHING_SCANNERS[str(name)]
        failed = bool(getattr(outcome, "failed", False))
        signals = [] if failed else list(getattr(outcome, "signals", None) or [])
        # Marked BEFORE the early return, so a scan that ran and found
        # nothing is distinguishable from a producer that never ran.
        # Those need opposite responses and read identically without it.
        #
        # A FAILED scanner is marked too, and says so. It used to be
        # skipped entirely, which left the previous cycle's marker as the
        # newest one -- so a failed scan was indistinguishable from no
        # scan having happened since the last good one, and the stale
        # rows behind it stayed consumable.
        candidate_publisher.mark_run(
            day, session, strategy_id=strategy_id, candidates=len(signals),
            run_id=run_id,
            status=(scan_cycle.STATUS_FAILED if failed else scan_cycle.STATUS_OK),
            started_at=started_at, completed_at=completed_at,
            duration_seconds=duration)
        variant = None
        if str(name) == "orb":
            from config import s6_sessions

            # The variant is the session's, so a candidate always says
            # which range produced it. S6-R and S6-O are different
            # setups that happen to share a scanner.
            variant = s6_sessions.variant_for(session) or None

        if failed:
            logger.info("not publishing %s candidates: the scanner failed", name)
            # Declared, so a failed attempt cannot read as "no scan since
            # the last good one". FAILED is never consumable and is never
            # read as zero candidates -- it is the absence of a result.
            generations.publish(
                day, session, generation_id=run_id, variant=variant,
                strategy_id=strategy_id, status=generations.STATUS_FAILED,
                candidate_count=0, generated_at=started_at,
                completed_at=completed_at)
            continue

        rows = candidate_publisher.publish(
            signals, strategy_id=strategy_id, trading_day=day,
            session=session, variant=variant, run_id=run_id)
        written += len(rows)

        # LAST, and only now: every row is on disk, so the generation can
        # be declared complete. Published even when there are none --
        # zero is an answer, and a scan that reported it must supersede
        # whatever the previous generation found. Before this, an empty
        # scan wrote nothing at all and the previous generation's rows
        # simply stayed newest.
        generations.publish(
            day, session, generation_id=run_id, variant=variant,
            strategy_id=strategy_id, status=generations.STATUS_COMPLETED,
            candidate_count=len(rows), generated_at=started_at,
            completed_at=completed_at)

        _snapshot_safely(rows, scanner_name=name, report=report)
    return written


def _snapshot_safely(rows, *, scanner_name, report) -> None:
    """Record the first COMMON_STOCK S6-R candidate, if this run has one.

    Guarded the way every other post-scan step here is: a snapshot is an
    observation record, and a failure to write one must not cost the scan
    the candidates it just published.
    """
    if str(scanner_name) != "orb" or not rows:
        return
    try:
        from scanners.publish import s6_snapshot as snapshot

        snapshot.record_from_published(
            [row.as_dict() for row in rows],
            trading_day=getattr(report, "trading_day", None),
            session=getattr(report, "session", None),
            run_id=getattr(report, "run_id", None))
    except Exception:  # noqa: BLE001 - see publish() on why a scan must
        # survive a failed write.
        logger.warning("could not record an S6 candidate snapshot",
                       exc_info=True)


def main(argv=None, *, provider=None) -> int:
    """`provider` is INJECTED by the caller, never built here.

    The extended sessions need KIS-backed bars, and selecting them here
    would mean this module importing a broker --
    `tests/test_scanner_trading_isolation.py` forbids that, because an
    import that does not exist cannot be reached by a path nobody
    thought of. The operational entrypoint lives outside this package
    and is where that choice belongs.
    """
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
        session=args.session,
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

    day = args.trading_day or us_trading_day()
    # Resolved HERE rather than inside run_scanners, because the cycle
    # lock below is keyed on it and the lock is taken before the run
    # starts. Passed down so both agree; `--session` used to be parsed
    # and then never handed to `run_scanners`, so the flag silently did
    # nothing on a real run while working on the market-closed path.
    session = scan_session.normalize(args.session) or scan_session.session_at()

    # One scan per (day, session, publishing scanner) at a time. A cron
    # entry firing while its own previous run is still going gets a
    # refusal, not a place in a queue: two concurrent scans append two
    # answers to one candidate file and nothing downstream could tell
    # which one it read. Scanners that publish nothing take no lock and
    # are unaffected.
    from scanners.publish import scan_cycle

    # `names or ALL_SCANNERS` is what `run_scanners` will actually run --
    # an invocation with no profile and no explicit list runs everything,
    # orb included. Reading `names` alone would leave exactly that
    # invocation unlocked, which is the one a person types by hand while
    # a scheduled scan is already going.
    publishing = [name for name in (names or ALL_SCANNERS)
                  if name in PUBLISHING_SCANNERS]
    with scan_cycle.hold_all(day, session, scanners=publishing) as cycle:
        if cycle.skipped:
            print(f"[SCAN CYCLE] skipped -- {cycle.detail()}")
            logger.warning("scan skipped, previous run still in progress: %s",
                           cycle.detail())
            # Zero: a refused overlap is the guard working, not a fault.
            # A non-zero exit here would page an operator every time a
            # scan ran long.
            return 0
        if cycle.unresolved:
            # No lock, because there is no shared store to hold one in.
            # The scan still runs: its signals belong in the analytics
            # dataset either way, and there is no hand-off to protect.
            # Publication will record PRODUCER_CONFIG_ERROR and the exit
            # code will be non-zero -- which is a different message to an
            # operator than "a scan is already running".
            print(f"[SCAN CYCLE] no shared store -- {cycle.detail()}")
            logger.error("candidate hand-off cannot be locked: %s",
                         cycle.detail())
        return _run_and_report(args, names=names, symbols=symbols, day=day,
                               session=session, provider=provider)


def _run_and_report(args, *, names, symbols, day, session,
                    provider=None) -> int:
    """One scan, its notifications and its publication.

    Split out of `main` so the cycle lock wraps the whole of it -- the
    publication at the end is inside the lock too, which is what makes
    "the scan is running" and "its candidate file is complete" the same
    interval.
    """
    report = run_scanners(
        provider=provider,
        scanners=names or None,
        symbols=symbols,
        limit=args.limit,
        trading_day=day,
        session=session,
        store=not args.no_store,
        daily_lookback_days=args.daily_lookback_days,
        intraday_interval=args.intraday_interval,
        intraday_lookback_days=args.intraday_lookback_days,
        profile=args.profile,
        universe_type=args.universe,
        active_pool_size=args.active_pool_size,
        # getattr, not args.supplement_size: this is an OPT-IN flag, so a
        # caller holding an args object that predates it must keep
        # working with the supplement off rather than raising
        # AttributeError. The default is 0, so "not passed" and
        # "explicitly disabled" produce an identical scan.
        supplement_size=getattr(args, "supplement_size",
                                intraday_supplement.DEFAULT_SUPPLEMENT_SIZE),
        manifest_path=getattr(args, "manifest_path", None),
        use_eligibility=not args.no_eligibility,
        # Publication happens inside the run now, before the manifest is
        # written, so the record of what was handed over is part of the
        # record of the run rather than a log line beside it.
        publish=not args.no_store,
    )
    print_report(report)
    # A scanner that failed is an operational problem worth a non-zero
    # exit so cron/systemd surfaces it -- but only after the other
    # scanners' results have been stored.
    #
    # A publishing scan whose hand-off has nowhere to go is the same kind
    # of problem and was previously reported as success: publication was
    # wrapped in a bare `except` that logged a warning and returned the
    # scan's own exit code. A cron entry cannot act on a warning.
    exit_code = 0 if report.status == run_context.SUCCESS else 1
    if report.publication_status in run_context.PUBLICATION_FAILURES:
        exit_code = 1
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
