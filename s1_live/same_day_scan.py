"""S1 candidates computed on demand, from completed bars only.

Why this exists
---------------
`s1_live/publisher.py` builds the candidate file from a COMPLETED scanner
run's manifest. That works for the evening pass, but it makes the live
path depend on a file written hours earlier: if last night's run produced
nothing, the account cannot trade today no matter what the market does,
and an opportunity that forms at 11:00 is invisible until tomorrow.

This module answers the same question without that dependency. Given a
universe and today's trading day, it computes S1 from scratch, so every
session can ask again and get an answer that reflects the market as of
the last completed bar.

The one rule that makes it sound
-------------------------------
The bar window ends at `previous_trading_day(current_trading_day)`.
Today's bar is excluded ALWAYS -- not because it is unavailable, but
because it is unfinished. HMA200, HMA89 and ADX are daily-close
indicators; a partial close makes them move under their own feet, so the
same symbol would pass at 10:00 and fail at 15:00 and neither answer
would be the one S1 was measured on. Truncating in one place, here, is
what keeps every session's re-scan comparable to every other.

Realtime price belongs to the ORDER decision, not this one. The entry
gate reads the current session's price to size and price the order;
nothing in this module does, and a test asserts the truncation cannot be
switched off.

Nothing about S1 itself changes
-------------------------------
The scanner instance, its config, `check()`, `score()` and the score
threshold are the existing ones, untouched. This module decides only
WHICH BARS the existing logic sees. It also runs exactly one scanner --
S1 -- so no S2..S6 condition can enter the live path through it.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from scanners.base.trading_calendar import previous_trading_day, us_trading_day

logger = logging.getLogger(__name__)

#: The one scanner this module is allowed to run. S2..S6 are
#: DISCOVERY_ONLY and must not be able to reach the order path.
S1_SCANNER_NAME = "hma_early_trend"

STATUS_OK = "OK"
STATUS_NO_CANDIDATE = "NO_CANDIDATE"
STATUS_DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


class SameDayScanError(Exception):
    """The scan could not be performed. Callers must not treat this as
    "no candidates" -- an unanswerable scan is not an empty one."""


@dataclass
class S1SameDayCandidate:
    symbol: str
    score: float
    signal_price: float
    signal_day: str
    trading_day: str
    session: Optional[str] = None
    reasons: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "score": self.score,
            "signal_price": self.signal_price, "signal_day": self.signal_day,
            "trading_day": self.trading_day, "session": self.session,
            "reasons": list(self.reasons), "metrics": dict(self.metrics),
        }


@dataclass
class S1SameDayScan:
    trading_day: str
    signal_day: str
    session: Optional[str]
    candidates: List[S1SameDayCandidate] = field(default_factory=list)
    evaluated: int = 0
    rejected: int = 0
    unavailable: int = 0

    @property
    def status(self) -> str:
        if self.candidates:
            return STATUS_OK
        if self.evaluated == 0:
            return STATUS_DATA_UNAVAILABLE
        return STATUS_NO_CANDIDATE

    def as_dict(self) -> Dict[str, Any]:
        return {
            "trading_day": self.trading_day, "signal_day": self.signal_day,
            "session": self.session, "status": self.status,
            "evaluated": self.evaluated, "rejected": self.rejected,
            "unavailable": self.unavailable,
            "candidates": [c.as_dict() for c in self.candidates],
        }


def signal_day_for(trading_day=None) -> str:
    """The last COMPLETED session, which is what S1 may be computed on.

    Deliberately not "today minus one calendar day" and deliberately not
    today: see the module docstring.
    """
    day = trading_day or us_trading_day()
    if isinstance(day, (date, datetime)):
        day = day.strftime("%Y-%m-%d") if isinstance(day, datetime) else day.isoformat()
    return previous_trading_day(day)


def daily_through(frame, signal_day: str):
    """`frame` truncated to bars at or before `signal_day`.

    This is the single point where the incomplete current bar is dropped.
    A frame whose index is not datetime-like (the unit-test fixtures) is
    returned unchanged -- there is no date to compare against, and
    inventing one would silently drop real bars.
    """
    if frame is None or len(frame) == 0:
        return frame
    try:
        import pandas as pd
    except ImportError:  # pragma: no cover - pandas is a hard dependency
        return frame
    index = frame.index
    if not isinstance(index, pd.DatetimeIndex):
        return frame
    cutoff = pd.Timestamp(signal_day)
    if index.tz is not None:
        cutoff = cutoff.tz_localize(index.tz)
    # `normalize()` compares by CALENDAR DAY, so an intraday-stamped
    # daily bar on the cutoff date is kept rather than dropped for being
    # a few hours "late".
    return frame[index.normalize() <= cutoff]


def _truncated_bundle(data, signal_day: str):
    """A copy of `data` whose daily frame stops at `signal_day`.

    A copy rather than a mutation: the same bundle is shared with the
    analytics scanners (spec section 17 depends on all of them judging
    byte-identical data), so truncating in place would silently change
    what S2..S6 see.
    """
    import dataclasses

    # `SymbolData` is frozen, so this is a replace() rather than an
    # assignment -- which is the behaviour we want anyway.
    #
    # The intraday frame is dropped rather than truncated: S1 declares
    # `requires_intraday = False`, so no premarket or in-session bar can
    # reach the daily calculation by another route.
    return dataclasses.replace(
        data, daily=daily_through(data.daily, signal_day),
        intraday=None, premarket=None)


def evaluate_symbol(scanner, data, *, signal_day: str, trading_day: str,
                    session: Optional[str] = None,
                    score_threshold: Optional[float] = None
                    ) -> Optional[S1SameDayCandidate]:
    """Run the EXISTING S1 logic against completed bars only.

    Returns a candidate, or None when the symbol fails S1's own
    conditions or its history cannot support them. Raises nothing for an
    ordinary rejection -- that is the common case, not an error.
    """
    from scanners.base import features as feature_builder
    from scanners.base.models import ScannerDataError
    from scanners.base.scanner_base import Rejected

    bundle = _truncated_bundle(data, signal_day)
    try:
        # No `config=` argument, so this uses `common_config()` -- the
        # SHARED indicator lengths (hma_slow_length, adx_period, ...),
        # exactly as `scanners/runner.py` does. The scanner's own config
        # holds only its thresholds and score weights, and passing it
        # here would leave the shared lengths undefined.
        computed = feature_builder.build_features(bundle, require_intraday=False)
    except ScannerDataError as exc:
        logger.debug("S1 same-day: %s has insufficient completed history: %s",
                     data.symbol, exc)
        return None

    context: Dict[str, Any] = {"trading_day": trading_day, "signal_day": signal_day}
    try:
        reasons = scanner.check(computed, bundle, context)
    except Rejected as exc:
        # An ordinary S1 rejection -- the common case. Anything OTHER
        # than `Rejected` is a real fault and is re-raised, because a
        # bug must not be silently reported as "no candidate".
        logger.debug("S1 same-day: %s rejected: %s", data.symbol, exc)
        return None

    score = float(scanner.score(computed, bundle, context))
    if score_threshold is not None and score < score_threshold:
        logger.debug("S1 same-day: %s scored %.2f below threshold %.2f",
                     data.symbol, score, score_threshold)
        return None

    price = getattr(computed, "price", None)
    if price is None or not isinstance(price, (int, float)) or price <= 0:
        logger.debug("S1 same-day: %s has no usable signal price", data.symbol)
        return None

    metrics = {}
    try:
        metrics = dict(scanner.extra_metrics(computed, bundle, context))
    except Exception:
        logger.debug("S1 same-day: extra metrics unavailable for %s", data.symbol,
                     exc_info=True)
    metrics.update({
        "hma200": getattr(computed, "hma200", None),
        "hma89": getattr(computed, "hma89", None),
        "hma200_slope": getattr(computed, "hma200_slope", None),
        "adx": getattr(computed, "adx", None),
        "daily_bars_used": (0 if bundle.daily is None else len(bundle.daily)),
    })
    return S1SameDayCandidate(
        symbol=data.symbol, score=score, signal_price=float(price),
        signal_day=signal_day, trading_day=trading_day, session=session,
        reasons=list(reasons or []), metrics=metrics)


def build_s1_scanner():
    """The EXISTING S1 scanner and its EXISTING config. Nothing tuned."""
    from scanners.hma_early_trend.scanner import HmaEarlyTrendScanner

    return HmaEarlyTrendScanner()


def s1_score_threshold(scanner=None) -> Optional[float]:
    """S1's configured score threshold, which is None -- and that is the
    correct answer, not a lookup failure.

    `scanners/hma_early_trend/config.json` sets no minimum score. S1's
    FILTER is `check()`; `score()` exists to RANK the names that already
    passed it. Inventing a floor here would silently make S1 stricter
    than the version whose behaviour is being measured, which section 9
    forbids -- and the direction of that mistake (fewer candidates) is
    the one that looks like "the market was quiet" rather than a bug.

    It is read from the config rather than hardcoded as None so that if
    a threshold is ever added there, this path picks it up instead of
    ignoring it.
    """
    scanner = scanner or build_s1_scanner()
    params = getattr(getattr(scanner, "config", None), "params", None) or {}
    for key in ("min_score", "score_threshold", "signal_min_score"):
        if key in params:
            try:
                return float(params[key])
            except (TypeError, ValueError):
                raise SameDayScanError(
                    f"S1 config sets {key}={params[key]!r}, which is not a number")
    return None


def scan(symbols, *, bundles=None, provider=None, trading_day=None,
         session: Optional[str] = None, scanner=None,
         score_threshold: Optional[float] = None, limit: Optional[int] = None,
         daily_lookback_days: Optional[int] = None) -> S1SameDayScan:
    """Compute S1 candidates for `symbols`, highest score first.

    Stateless on purpose: §2 requires every session to be able to ask
    again, so there is no cached answer and no "already scanned today"
    short-circuit. A session that finds nothing does not prevent the next
    one from finding something.

    `bundles` (a {symbol: SymbolData} mapping) is accepted so a caller
    that has already fetched -- and the tests -- need no provider.
    """
    day = trading_day or us_trading_day()
    signal_day = signal_day_for(day)
    scanner = scanner or build_s1_scanner()
    if score_threshold is None:
        score_threshold = s1_score_threshold(scanner)

    if daily_lookback_days is None:
        # Enough completed bars for HMA200 plus the slope window, taken
        # from the shared config rather than guessed, and widened for
        # weekends/holidays since the provider counts CALENDAR days.
        from scanners.base.features import minimum_daily_bars
        daily_lookback_days = int(minimum_daily_bars() * 1.6) + 30

    result = S1SameDayScan(trading_day=day, signal_day=signal_day, session=session)
    for symbol in symbols:
        data = None
        if bundles is not None:
            data = bundles.get(symbol)
        elif provider is not None:
            try:
                # `get_symbol_data`, the same call scanners/runner.py makes.
                # No intraday and no premarket: S1 declares
                # `requires_intraday = False`, and asking for bars we must
                # not feed into a daily indicator only invites them being
                # used by accident.
                data = provider.get_symbol_data(
                    symbol, daily_lookback_days=daily_lookback_days,
                    intraday_interval="5m", intraday_lookback_days=0,
                    want_premarket=False)
            except Exception as exc:
                logger.debug("S1 same-day: could not fetch %s: %s", symbol, exc)
                data = None
        if data is None or getattr(data, "daily", None) is None:
            result.unavailable += 1
            continue
        result.evaluated += 1
        try:
            candidate = evaluate_symbol(
                scanner, data, signal_day=signal_day, trading_day=day,
                session=session, score_threshold=score_threshold)
        except Exception:
            # A fault on one symbol must not silently shrink the scan.
            logger.error("S1 same-day: evaluation failed for %s", symbol, exc_info=True)
            result.unavailable += 1
            result.evaluated -= 1
            continue
        if candidate is None:
            result.rejected += 1
        else:
            result.candidates.append(candidate)

    # Ties break on symbol ascending, the convention the watchlist and
    # the publisher already use, so the same market produces the same
    # ordering wherever it is ranked.
    result.candidates.sort(key=lambda c: (-c.score, c.symbol))
    if limit is not None:
        result.candidates = result.candidates[:limit]
    logger.info("S1 same-day scan: day=%s signal_day=%s session=%s -> %s "
                "(evaluated=%d rejected=%d unavailable=%d)",
                day, signal_day, session, result.status, result.evaluated,
                result.rejected, result.unavailable)
    return result
