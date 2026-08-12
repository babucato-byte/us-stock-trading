"""The contract every scanner implements, and the isolation around it.

Section 5 requires that one scanner failing does not stop the others.
That is enforced at two levels here, because there are two independent
ways a scan goes wrong:

* One SYMBOL breaks a scanner -- a delisted name, a frame with a NaN
  column, an arithmetic edge nobody predicted. `scan()` catches per
  symbol, records it, and continues to the next one. A single bad ticker
  cannot cost a scanner its other 799 evaluations.
* One SCANNER breaks entirely -- a config typo, a bad import, a bug in
  its scoring. `runner.py` catches per scanner, so the other five still
  run and still store their signals.

The split matters: without the inner catch, one poisoned symbol would
take out a whole scanner via the outer catch, and the day's data for
that scanner would be lost rather than reduced by one row.

Scanners are pure functions of their input
------------------------------------------
`evaluate()` receives a `SymbolData` bundle and returns a signal or
None. It has no provider, no network, no clock of its own, and it does
not write anything. Every branch of every scanner is therefore reachable
from a unit test holding a hand-built DataFrame -- which is what section
28's list (NaN, zero volume, missing bar, insufficient history) requires
in order to be testable at all.

Rejections carry reasons
------------------------
`Rejected` is raised with a sentence, not a bare False, so section 27's
"do not store only PASS/FAIL" holds on the reject side too. Knowing that
`adx 14.2 below 20` rejected 600 names, while `price below HMA200`
rejected 40, is what makes month-two calibration possible; a count of
rejections tells you nothing.
"""

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from scanners.base.config import ScannerConfig, load_config
from scanners.base.features import SymbolFeatures, build_features, minimum_daily_bars
from scanners.base.market_data_provider import SymbolData
from scanners.base.models import ScannerDataError, ScannerSignal
from scanners.base.scanner_logging import get_scanner_logger, log_decision


class Rejected(Exception):
    """This symbol does not qualify, and here is the condition that
    failed. Control flow, not an error."""


@dataclass
class ScanOutcome:
    """What one scanner did over one symbol list."""

    scanner_name: str
    scanner_version: str
    config_fingerprint: str
    trading_day: str
    signals: List[ScannerSignal] = field(default_factory=list)
    rejected: int = 0
    data_errors: int = 0
    exceptions: int = 0
    symbols_seen: int = 0
    duration_seconds: float = 0.0
    reject_reasons: Dict[str, int] = field(default_factory=dict)
    error_samples: List[str] = field(default_factory=list)
    failed: bool = False
    failure_reason: Optional[str] = None

    #: Longest run of consecutive per-symbol exceptions seen. The
    #: runner's circuit breaker fires on this; recording the peak means
    #: a run that came CLOSE to tripping is visible afterwards, not just
    #: one that did (spec section 13).
    consecutive_error_peak: int = 0
    circuit_breaker_triggered: bool = False
    circuit_breaker_reason: Optional[str] = None

    @property
    def status(self) -> str:
        """SUCCESS or FAILED for this one scanner (spec section 14)."""
        from scanners.base import run_context

        return run_context.FAILED if self.failed else run_context.SUCCESS

    @property
    def candidate_count(self) -> Optional[int]:
        """Signals found, or None if this scanner did not complete.

        None rather than 0 is the whole point of section 14: a scanner
        that crashed found no candidates in the same sense that an
        unopened envelope contains no letter. Reporting 0 would put it
        in the month-1 averages as a genuine quiet day.
        """
        from scanners.base import run_context

        return run_context.candidate_count_for(self.status, len(self.signals))

    def summary(self) -> Dict[str, Any]:
        return {
            "scanner_name": self.scanner_name,
            "scanner_version": self.scanner_version,
            "config_fingerprint": self.config_fingerprint,
            "trading_day": self.trading_day,
            "status": self.status,
            "candidate_count": self.candidate_count,
            "signal_count": len(self.signals),
            "symbols_seen": self.symbols_seen,
            "rejected": self.rejected,
            "data_errors": self.data_errors,
            "exceptions": self.exceptions,
            "consecutive_error_peak": self.consecutive_error_peak,
            "circuit_breaker_triggered": self.circuit_breaker_triggered,
            "circuit_breaker_reason": self.circuit_breaker_reason,
            "duration_seconds": round(self.duration_seconds, 3),
            "top_reject_reasons": sorted(
                self.reject_reasons.items(), key=lambda item: item[1], reverse=True)[:10],
            "error_samples": self.error_samples[:5],
            "failed": self.failed,
            "failure_reason": self.failure_reason,
        }


class BaseScanner(ABC):
    """Subclass contract: set the three class attributes, implement
    `check()`, and optionally `score()` and `extra_metrics()`."""

    #: Directory under `scanners/` holding this scanner's config.json.
    scanner_dir: str = ""
    #: Stable identifier written to every signal and every log line.
    #: Never change it -- section 6 keys the duplicate-allowed analysis
    #: on this, and renaming would split one scanner's history in two.
    scanner_name: str = ""
    #: Does `evaluate()` need intraday bars to mean anything?
    requires_intraday: bool = False

    #: The bar interval this scanner's DECISION rests on (spec section
    #: 19). Not "every interval it touches": the intraday scanners also
    #: read daily HMA and the 52-week high, but their verdict is an
    #: intraday one, and `data_timestamp` has to describe the bars the
    #: verdict actually used. A month later this is the only record of
    #: whether a given scanner's numbers were daily or minute.
    source_timeframe: str = "1d"

    def __init__(self, config: Optional[ScannerConfig] = None, *, logger=None):
        if not self.scanner_name or not self.scanner_dir:
            raise ValueError(f"{type(self).__name__} must set scanner_name and scanner_dir")
        self.config = config or load_config(self.scanner_dir, scanner_name=self.scanner_name)
        self.log: logging.Logger = logger or get_scanner_logger(self.scanner_name)

    @property
    def version(self) -> str:
        """The version comes from the config file, not from a constant
        in the code.

        Section 19 pairs "parameters live in a file" with "changing a
        parameter changes the version". Sourcing the version from the
        same file as the parameters is what makes that pairing possible
        to honour in one edit -- and the config fingerprint on every
        signal catches the case where someone forgets.
        """
        return self.config.version

    @property
    def config_fingerprint(self) -> str:
        return self.config.fingerprint

    # ---- subclass hooks -------------------------------------------------

    @abstractmethod
    def check(self, features: SymbolFeatures, data: SymbolData,
              context: Dict[str, Any]) -> List[str]:
        """Apply this scanner's conditions.

        Return the list of reasons the symbol PASSED (section 27), or
        raise `Rejected` with the condition that failed. Raising rather
        than returning None keeps the reject reason attached to the
        rejection at the point it happens, instead of being reconstructed
        afterwards from whichever condition looks most likely.

        `context` is a scratch dict, created fresh per symbol and handed
        on to `score()` and `extra_metrics()`. Half the scanners compute
        something expensive or scanner-specific while checking -- the
        opening range, the gap and its impulse/pullback volumes, the
        existing premarket scanner's whole result dict -- and all three
        hooks need it. Passing it through is what keeps those values
        from being recomputed (twice more, differently) or cached on
        `self`, where a reused scanner instance would leak one symbol's
        state into the next one's score.
        """

    @abstractmethod
    def score(self, features: SymbolFeatures, data: SymbolData,
              context: Dict[str, Any]) -> float:
        """This scanner's own 0-100 score.

        Section 9 is explicit that these are NOT comparable between
        scanners -- 80 from the ORB scanner and 80 from the HMA scanner
        do not mean the same thing, and nothing in this codebase treats
        them as if they did. The normalisation to 0-100 exists so that a
        single scanner's signals can be ranked against each other and so
        that a distribution is readable in a report, not to create a
        cross-scanner currency.
        """

    def extra_metrics(self, features: SymbolFeatures, data: SymbolData,
                      context: Dict[str, Any]) -> Dict[str, Any]:
        """Scanner-specific values with no column in the common schema."""
        return {}

    def override_schema_fields(self, features: SymbolFeatures, data: SymbolData,
                               context: Dict[str, Any]) -> Dict[str, Any]:
        """Common-schema values this scanner measured for itself.

        Almost every scanner returns nothing here and takes the shared
        feature pass as-is, which is what keeps the six comparable.

        The exception is a scanner that judged the symbol from numbers
        it computed itself -- the premarket adapter (S4) wraps an
        existing module that derives its own price, VWAP and EMAs from
        the intraday frame, and those are the values its verdict rests
        on. Storing the framework's daily close as that signal's
        `signal_price` would anchor every forward return in section 12
        to a price the scanner never saw.

        Accepted keys are the common schema's own field names, plus
        `signal_price`. Anything else is ignored rather than silently
        creating a new column.
        """
        return {}

    def minimum_daily_bars(self) -> int:
        return minimum_daily_bars()

    # ---- framework ------------------------------------------------------

    def build_features(self, data: SymbolData) -> SymbolFeatures:
        return build_features(data, require_intraday=self.requires_intraday)

    def evaluate(
        self,
        data: SymbolData,
        *,
        trading_day: str,
        timestamp: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> Optional[ScannerSignal]:
        """One symbol. A signal, or None if it did not qualify.

        Raises `ScannerDataError` when the bars were unusable, which the
        caller treats differently from a rejection: "we could not judge
        this symbol" is not the same finding as "we judged it and it
        failed", and conflating them would make month one's rejection
        statistics meaningless.
        """
        stamp = timestamp or datetime.now(timezone.utc).isoformat()
        features = self.build_features(data)
        context: Dict[str, Any] = {}
        try:
            reasons = self.check(features, data, context)
        except Rejected as rejection:
            log_decision(self.log, scanner=self.scanner_name, version=self.version,
                         symbol=data.symbol, result="FAIL", reason=str(rejection))
            return None

        raw_score = self.score(features, data, context)
        score = max(0.0, min(100.0, float(raw_score)))

        metrics = dict(features.shared_metrics())
        metrics.update(self.extra_metrics(features, data, context) or {})
        metrics["config_fingerprint"] = self.config_fingerprint

        schema = features.schema_fields()
        signal_price = features.price
        overrides = self.override_schema_fields(features, data, context) or {}
        if "signal_price" in overrides:
            override_price = overrides["signal_price"]
            # A None override would silently null the anchor every
            # forward return is measured from; keep the feature pass's
            # price in that case.
            if override_price is not None:
                signal_price = override_price
        for key, value in overrides.items():
            if key in schema:
                schema[key] = value

        signal = ScannerSignal(
            timestamp=stamp,
            trading_day=trading_day,
            symbol=data.symbol,
            scanner_name=self.scanner_name,
            scanner_version=self.version,
            scanner_score=score,
            signal_price=signal_price,
            market_data_provider=data.provider_name,
            market_data_feed=data.provider_feed,
            data_timestamp=features.data_timestamp_for(self.source_timeframe),
            feature_timestamp=features.feature_timestamp,
            scanner_run_id=run_id,
            source_timeframe=self.source_timeframe,
            reasons=list(reasons),
            metrics=metrics,
            **schema,
        )
        log_decision(self.log, scanner=self.scanner_name, version=self.version,
                     symbol=data.symbol, result="PASS", reason=signal.reason)
        return signal

    def scan(
        self,
        bundles: Iterable[SymbolData],
        *,
        trading_day: str,
        timestamp: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> ScanOutcome:
        """Evaluate every symbol, surviving anything one of them does.

        `timestamp` is taken once for the whole scan rather than per
        symbol, so every signal from a run shares an instant. The
        performance tracker measures forward returns from that instant;
        letting it drift across the loop would mean two scanners' "1
        hour return" for the same symbol covered different hours.
        """
        stamp = timestamp or datetime.now(timezone.utc).isoformat()
        outcome = ScanOutcome(
            scanner_name=self.scanner_name,
            scanner_version=self.version,
            config_fingerprint=self.config_fingerprint,
            trading_day=trading_day,
        )
        started = time.monotonic()
        for data in bundles:
            self.evaluate_into(outcome, data, trading_day=trading_day, timestamp=stamp,
                               run_id=run_id)
        outcome.duration_seconds += time.monotonic() - started
        return outcome

    def new_outcome(self, trading_day: str) -> ScanOutcome:
        return ScanOutcome(
            scanner_name=self.scanner_name,
            scanner_version=self.version,
            config_fingerprint=self.config_fingerprint,
            trading_day=trading_day,
        )

    def evaluate_into(
        self,
        outcome: ScanOutcome,
        data: SymbolData,
        *,
        trading_day: str,
        timestamp: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> Optional[ScannerSignal]:
        """One isolated evaluation, accumulated into `outcome`.

        Factored out of `scan()` because the runner drives the loop
        symbol-major rather than scanner-major -- it fetches one
        symbol's bars, offers them to all six scanners, then discards
        them, so an 800-name universe never holds 800 symbols' worth of
        minute bars in memory at once. Both loops share this method so
        there is exactly one implementation of the per-symbol isolation
        rule, and a fix to it cannot reach one caller and miss the other.
        """
        outcome.symbols_seen += 1
        try:
            signal = self.evaluate(data, trading_day=trading_day, timestamp=timestamp,
                                   run_id=run_id)
        except ScannerDataError as exc:
            outcome.data_errors += 1
            _count_reason(outcome.reject_reasons, "insufficient_or_stale_data")
            log_decision(self.log, scanner=self.scanner_name, version=self.version,
                         symbol=data.symbol, result="FAIL", reason=str(exc))
            return None
        except Exception as exc:  # noqa: BLE001 - one symbol must not end a scan
            outcome.exceptions += 1
            message = f"{data.symbol}: {type(exc).__name__}: {exc}"
            outcome.error_samples.append(message)
            self.log.exception("scanner=%s version=%s symbol=%s result=ERROR reason=%s",
                               self.scanner_name, self.version, data.symbol, message)
            return None
        if signal is None:
            outcome.rejected += 1
            return None
        outcome.signals.append(signal)
        return signal


def _count_reason(counter: Dict[str, int], reason: str) -> None:
    counter[reason] = counter.get(reason, 0) + 1


def require(condition: bool, message: str) -> None:
    """Assert a scanner condition, or reject with the reason.

    Used instead of `if not x: raise Rejected(...)` so the condition and
    its explanation sit on one line and cannot drift apart -- a reject
    reason that no longer describes the condition it accompanies is
    worse than no reason at all, because month-two calibration would act
    on it.
    """
    if not condition:
        raise Rejected(message)


def fmt(value, digits: int = 2) -> str:
    """Format a possibly-None number for a reason string."""
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)
