"""The bar-level market-data seam, per spec section 3.

Why a second provider interface exists
--------------------------------------
`market_data/base.py` already defines a `MarketDataProvider`, and it is
deliberately left alone: it has exactly one method, `get_price_quote`,
because the only thing the LIVE ORDER path is allowed to ask a data
source for is "what is this worth right now". Widening that interface to
carry bar history would hand the order path a much larger surface than
it needs, and every implementation of it -- including the KIS validation
provider that re-prices real orders -- would have to grow methods it has
no business having.

So the scanners get their own seam. `BarMarketDataProvider` is a
read-only history interface, used only by analysis code, with no import
path from here into `broker/` or `execution/`.

The name says what it calls
---------------------------
The v1.0 implementation is `YahooFinanceMarketDataProvider`, because
Yahoo Finance is what it fetches from.

An earlier draft called it `AlpacaMarketDataProvider`, following the
original spec's file layout. That name was wrong in a way that matters
for this project specifically: the entire point of month 1 is a dataset
whose provenance is unambiguous a month later, and a class named for one
vendor while calling another guarantees the opposite. In this repository
"Alpaca" has never meant Alpaca's Market Data API -- it is the BROKER
(accounts and orders) plus the asset list that seeds `universe.csv`,
while price and volume have always come from yfinance, in
`daily_candidate_scanner.py`, in `score_scanner/`, and in
`market_data/alpaca_provider.py`.

`AlpacaMarketDataProvider` remains at the bottom of this module as a
DEPRECATED transitional alias so nothing that imported it breaks. New
code must not use it, and it will be removed once nothing references it.

A real Alpaca Market Data implementation, if one is ever written, gets
its own class and its own `provider_name`, and the analytics layer will
treat its results as a separate experiment (see `provider_name` below).

Provider identity is recorded, not inferred
-------------------------------------------
Every provider declares `provider_name` and `feed_name`, and both are
written onto every signal. Month 2 must be able to tell a scanner's
results apart by where the bars came from -- otherwise switching vendors
mid-experiment silently blends two datasets under one scanner's name.

`feed_name` is `None` when the upstream does not tell us which feed
served the request. Yahoo Finance does not expose a feed identifier, so
this provider reports `None` rather than inventing one: spec section 4
is explicit that an unverified feed name must never be guessed.

Swapping in Polygon or Databento later means writing one more subclass;
no scanner imports yfinance directly.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import pandas as pd

from scanners.base.indicators import close_series, to_float, volume_series

logger = logging.getLogger(__name__)

#: The period strings the provider accepts, shortest first. A request is
#: expressed as a period, not as a day count, so a lookback in days has
#: to be mapped onto one of these -- and "7 days" has no period string,
#: which is where the trap below comes from.
PERIODS = ("1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "max")

#: The longest period each intraday interval may actually be requested
#: with.
#:
#: The provider enforces its own per-interval history limits -- 8 days
#: for 1-minute bars, 60 days for 5/15/30-minute -- and REJECTS a
#: request that exceeds them rather than truncating it. The rejection
#: arrives as an empty frame, which to a scanner is indistinguishable
#: from "this symbol did not trade".
#:
#: The subtlety that makes this worth a table rather than a day count:
#: a 7-day 1-minute lookback is inside the 8-day limit, but there is no
#: `7d` period string, so it rounds UP to `1mo` and is refused. The
#: performance tracker asked for exactly that and silently received no
#: minute bars at all -- every intraday return and the whole signal-day
#: excursion window came back null, in production only, with nothing in
#: the logs to say why. Capping by PERIOD rather than by days is what
#: makes that unrepresentable.
MAX_PERIOD_FOR_INTERVAL = {
    "1m": "5d",     # 8-day provider limit; 5d is the longest period under it
    "2m": "1mo",    # 60-day limit
    "5m": "1mo",
    "15m": "1mo",
    "30m": "1mo",
    "60m": "1y",    # 730-day limit
    "1h": "1y",
}


class MarketDataUnavailable(Exception):
    """No usable bars for this symbol right now.

    Always recoverable at the symbol level: the scan skips the symbol
    and continues (spec section 5). Never escalated into a scan-wide
    failure, and never into anything the order path can observe.
    """


#: Why an intraday frame is missing. Two answers that look identical at
#: the call site and mean opposite things.
#:
#: A thinly-traded name with no prints is ordinary and self-correcting.
#: A provider being asked for an interval it does not serve is a WIRING
#: fault: it fails for every symbol, for as long as the wiring stands,
#: and it fails BEFORE any market data is fetched -- so it cannot recover
#: on its own and no amount of waiting will change it.
#:
#: They were indistinguishable until 2026-09-04. `realtime_features.build`
#: asked for "5m" while `KISBarMarketDataProvider` serves 1m only, so
#: every PREMARKET / AFTER_HOURS / OVERNIGHT_DAYTIME candidate got
#: intraday=None, every gate read UNAVAILABLE, and all three sessions sat
#: at WATCHING for four days. The refusal was logged at DEBUG, once per
#: symbol, worded exactly like a quiet stock.
UNSUPPORTED_PROVIDER_CONTRACT = "UNSUPPORTED_PROVIDER_CONTRACT"
NORMAL_SYMBOL_DATA_UNAVAILABLE = "NORMAL_SYMBOL_DATA_UNAVAILABLE"


class UnsupportedIntervalError(MarketDataUnavailable):
    """A provider was asked for an interval it does not serve.

    A subclass rather than a flag, so existing `except
    MarketDataUnavailable` handlers keep working unchanged -- a symbol
    is still skipped, the scan still continues, and nothing new can take
    a cycle down. What the subclass buys is that a handler which CARES
    can tell the two apart, and `get_symbol_data` does.
    """

    reason_code = UNSUPPORTED_PROVIDER_CONTRACT

    def __init__(self, message, *, requested=None, supported=()):
        super().__init__(message)
        self.requested = requested
        self.supported = tuple(supported)


@dataclass(frozen=True)
class PremarketSnapshot:
    """What section S4 needs before the opening bell."""

    symbol: str
    previous_close: Optional[float]
    premarket_price: Optional[float]
    premarket_volume: Optional[float]
    premarket_gain_pct: Optional[float]
    as_of: datetime


@dataclass(frozen=True)
class SymbolData:
    """Every bar set for one symbol, fetched once and shared.

    Six scanners over an 800-name universe is 4,800 symbol-fetches if
    each scanner fetches for itself. They all want the same two frames,
    so the runner fetches once per symbol and hands the same bundle to
    every scanner. That is a ~6x reduction in provider calls and, more
    importantly, it guarantees all six scanners judged the symbol from
    byte-identical data -- without which "HMA and Breakout both flagged
    NVDA" (spec section 17) would be comparing signals taken minutes and
    several ticks apart.

    Scanners receive this and nothing else. They cannot reach the
    network, which is what makes every one of them a pure function that
    a unit test can drive from a fixture.
    """

    symbol: str
    daily: Optional[pd.DataFrame] = None
    intraday: Optional[pd.DataFrame] = None
    premarket: Optional[PremarketSnapshot] = None
    as_of: Optional[datetime] = None

    # --- provenance (spec section 4) ---
    #: Which provider served these bars, and which upstream feed if it
    #: told us. Carried on the bundle rather than looked up later,
    #: because "which vendor produced this signal" has to be answerable
    #: from the stored row a month afterwards, when the process that
    #: fetched it is long gone.
    provider_name: Optional[str] = None
    provider_feed: Optional[str] = None
    daily_interval: str = "1d"
    intraday_interval: Optional[str] = None
    include_prepost: Optional[bool] = None
    #: Why `intraday` is None, when it is: UNSUPPORTED_PROVIDER_CONTRACT
    #: or NORMAL_SYMBOL_DATA_UNAVAILABLE. Carried on the bundle rather
    #: than left in a log line, because the caller that has to explain
    #: "no features" to an operator is several layers from the fetch and
    #: cannot otherwise tell a quiet stock from a mis-wired provider.
    intraday_unavailable_reason: Optional[str] = None

    def require_daily(self, minimum_bars: int = 1) -> pd.DataFrame:
        """The daily frame, or a refusal naming what was missing.

        Raising `ScannerDataError` rather than returning an empty frame
        is what turns "IPO'd three weeks ago, cannot have an HMA200"
        into an explicit logged FAIL instead of a scanner silently
        comparing against NaN and rejecting for the wrong stated reason
        (spec section 28).
        """
        from scanners.base.models import ScannerDataError

        if self.daily is None or len(self.daily) == 0:
            raise ScannerDataError(f"{self.symbol}: no daily bars")
        if len(self.daily) < minimum_bars:
            raise ScannerDataError(
                f"{self.symbol}: {len(self.daily)} daily bars, need {minimum_bars}")
        return self.daily

    def require_intraday(self, minimum_bars: int = 1) -> pd.DataFrame:
        from scanners.base.models import ScannerDataError

        if self.intraday is None or len(self.intraday) == 0:
            raise ScannerDataError(f"{self.symbol}: no intraday bars")
        if len(self.intraday) < minimum_bars:
            raise ScannerDataError(
                f"{self.symbol}: {len(self.intraday)} intraday bars, need {minimum_bars}")
        return self.intraday


class BarMarketDataProvider(ABC):
    """History access for analysis code. Deliberately read-only."""

    name = "base"

    #: Written onto every signal this provider's bars produced (spec
    #: section 4). A subclass MUST override it -- month 2 cannot tell
    #: two vendors' results apart from a provider called "base".
    provider_name = "base"

    #: The upstream feed, when the vendor identifies one (IEX vs SIP, a
    #: consolidated tape, and so on). `None` means the upstream does not
    #: report it. Section 4: never guess a feed name that was not
    #: actually observed -- a fabricated one is worse than a null,
    #: because a null is visibly unknown and a guess is not.
    feed_name = None

    #: What a caller with no opinion should ask this provider for.
    #:
    #: The interval belongs to the PROVIDER, not to the session and not
    #: to the caller. `s6_live/realtime_features.py` hard-coded "5m"
    #: because that is what yfinance served, and when the extended
    #: sessions were switched to a KIS provider serving 1m only, the
    #: request and the source disagreed for exactly the three sessions
    #: that had been swapped. A second session-to-interval mapping
    #: somewhere else is how that happens again; asking the provider
    #: that was actually injected is how it cannot.
    preferred_intraday_interval = "5m"

    #: The intervals this provider serves, or () for "no declared
    #: restriction". Declared once and used BY the guard that enforces
    #: it, so the list and the refusal cannot drift apart.
    supported_intraday_intervals: tuple = ()

    def serves_intraday_interval(self, interval) -> bool:
        return (not self.supported_intraday_intervals
                or str(interval) in self.supported_intraday_intervals)

    @abstractmethod
    def get_daily_bars(self, symbol: str, lookback_days: int = 400) -> pd.DataFrame:
        """Daily OHLCV, oldest first. Empty frame if none."""

    @abstractmethod
    def get_intraday_bars(
        self,
        symbol: str,
        interval: str = "1m",
        lookback_days: int = 5,
        include_prepost: bool = True,
    ) -> pd.DataFrame:
        """Intraday OHLCV, oldest first. Empty frame if none."""

    def get_latest_price(self, symbol: str) -> Optional[float]:
        """Most recent traded price, or None.

        Defaults to the last intraday close and falls back to the last
        daily close, so a subclass only has to override this if its
        upstream offers a genuine real-time quote.
        """
        try:
            intraday = self.get_intraday_bars(symbol, interval="1m", lookback_days=1)
        except MarketDataUnavailable:
            intraday = None
        price = _last_close(intraday)
        if price is not None:
            return price
        try:
            return _last_close(self.get_daily_bars(symbol, lookback_days=5))
        except MarketDataUnavailable:
            return None

    def get_premarket_data(self, symbol: str) -> PremarketSnapshot:
        """Premarket price/volume against the prior regular close.

        Built from the same prepost intraday frame every other caller
        uses, rather than a separate endpoint, so a symbol can never
        show a premarket gain computed from one snapshot and an intraday
        VWAP computed from another.
        """
        daily = self.get_daily_bars(symbol, lookback_days=10)
        previous_close = _previous_daily_close(daily)
        intraday = self.get_intraday_bars(
            symbol, interval="1m", lookback_days=2, include_prepost=True)
        price = _last_close(intraday)
        volume = _sum_volume(intraday)
        gain = None
        if price is not None and previous_close not in (None, 0):
            gain = (price / previous_close - 1.0) * 100.0
        return PremarketSnapshot(
            symbol=symbol,
            previous_close=previous_close,
            premarket_price=price,
            premarket_volume=volume,
            premarket_gain_pct=to_float(gain),
            as_of=datetime.now(timezone.utc),
        )

    def get_symbol_data(
        self,
        symbol: str,
        *,
        daily_lookback_days: int = 400,
        intraday_interval: str = "1m",
        intraday_lookback_days: int = 5,
        include_prepost: bool = True,
        want_premarket: bool = True,
    ) -> SymbolData:
        """One symbol's full bundle.

        Intraday and premarket failures are demoted to None rather than
        propagated: a daily-only scanner (S1/S2/S3) must still run for a
        symbol whose minute bars are missing, which is routine for
        thinly-traded names. A daily failure DOES propagate, because no
        scanner here can do anything useful without it.

        The demotion still happens for a provider asked for an interval
        it does not serve -- one symbol must never take a scan down -- but
        it is NAMED and logged at WARNING rather than DEBUG. That fault
        is not about this symbol at all: it will repeat for every symbol
        until someone changes the wiring, and four days of "quiet
        extended sessions" were exactly this, worded as a thin stock.
        """
        daily = self.get_daily_bars(symbol, lookback_days=daily_lookback_days)
        intraday = None
        unavailable_reason = None
        try:
            intraday = self.get_intraday_bars(
                symbol,
                interval=intraday_interval,
                lookback_days=intraday_lookback_days,
                include_prepost=include_prepost,
            )
        except UnsupportedIntervalError as exc:
            unavailable_reason = UNSUPPORTED_PROVIDER_CONTRACT
            # WARNING, and per symbol. Deliberately not rate-limited and
            # deliberately not raised: the cost of a repeated line is a
            # noisy log, and the cost of a quiet one was three live
            # sessions that could not produce a single tradeable
            # candidate while every dashboard read "no setups".
            logger.warning(
                "%s: %s -- provider %r was asked for %r and serves %s; this "
                "is a wiring fault, not a quiet symbol, and it will repeat "
                "for every symbol until the interval matches",
                symbol, UNSUPPORTED_PROVIDER_CONTRACT,
                getattr(self, "provider_name", "?"),
                getattr(exc, "requested", intraday_interval),
                ", ".join(getattr(exc, "supported", ()) or ("?",)))
        except MarketDataUnavailable as exc:
            unavailable_reason = NORMAL_SYMBOL_DATA_UNAVAILABLE
            logger.debug("%s: intraday unavailable (%s)", symbol, exc)
        premarket = None
        if want_premarket:
            try:
                premarket = _premarket_from_frames(symbol, daily, intraday)
            except Exception as exc:  # noqa: BLE001 - never fail a scan on an extra
                logger.debug("%s: premarket snapshot unavailable (%s)", symbol, exc)
        return SymbolData(
            symbol=symbol,
            daily=daily,
            intraday=intraday,
            premarket=premarket,
            as_of=datetime.now(timezone.utc),
            provider_name=self.provider_name,
            provider_feed=self.feed_name,
            daily_interval="1d",
            intraday_interval=intraday_interval if intraday is not None else None,
            include_prepost=include_prepost if intraday is not None else None,
            intraday_unavailable_reason=unavailable_reason,
        )


def _last_close(df) -> Optional[float]:
    if df is None or len(df) == 0:
        return None
    closes = close_series(df).dropna()
    if closes.empty:
        return None
    return to_float(closes.iloc[-1])


def _previous_daily_close(df) -> Optional[float]:
    if df is None or len(df) == 0:
        return None
    closes = close_series(df).dropna()
    if len(closes) < 2:
        return None
    return to_float(closes.iloc[-2])


def _sum_volume(df) -> Optional[float]:
    if df is None or len(df) == 0:
        return None
    volumes = volume_series(df).dropna()
    if volumes.empty:
        return None
    return to_float(volumes.sum())


def _premarket_from_frames(symbol, daily, intraday) -> PremarketSnapshot:
    previous_close = _previous_daily_close(daily)
    price = _last_close(intraday)
    volume = _sum_volume(intraday)
    gain = None
    if price is not None and previous_close not in (None, 0):
        gain = (price / previous_close - 1.0) * 100.0
    return PremarketSnapshot(
        symbol=symbol,
        previous_close=previous_close,
        premarket_price=price,
        premarket_volume=volume,
        premarket_gain_pct=to_float(gain),
        as_of=datetime.now(timezone.utc),
    )


def _period_for_days(days: int) -> str:
    """The shortest period string that covers `days`."""
    days = max(1, int(days))
    for threshold, period in ((1, "1d"), (5, "5d"), (30, "1mo"), (90, "3mo"),
                              (180, "6mo"), (365, "1y"), (730, "2y"), (1825, "5y")):
        if days <= threshold:
            return period
    return "max"


def _capped_period(days: int, interval: str) -> str:
    """`_period_for_days`, never exceeding what the interval allows.

    Returns the shorter of the requested period and the interval's
    ceiling. Shortening is the only safe direction: a shorter period
    returns fewer bars, which callers already handle, while a longer one
    returns NO bars and looks like a symbol that did not trade.
    """
    period = _period_for_days(days)
    ceiling = MAX_PERIOD_FOR_INTERVAL.get(interval)
    if ceiling is None or period not in PERIODS or ceiling not in PERIODS:
        return period
    return period if PERIODS.index(period) <= PERIODS.index(ceiling) else ceiling


class YahooFinanceMarketDataProvider(BarMarketDataProvider):
    """The v1.0 implementation: the same yfinance path the existing
    scanners already use.

    yfinance is imported lazily inside the call rather than at module
    import, so importing a scanner module (which every unit test does)
    costs nothing and needs no network stack present.

    `feed_name` is None because Yahoo Finance does not identify which
    feed served a request. Section 4: an unverified feed name is not to
    be invented.
    """

    name = "yfinance"
    provider_name = "yfinance"
    feed_name = None

    def __init__(self, *, ticker_factory=None):
        # Injectable purely so tests can drive the retry/empty-frame
        # branches without patching a third-party module's globals.
        self._ticker_factory = ticker_factory

    def _ticker(self, symbol: str):
        if self._ticker_factory is not None:
            return self._ticker_factory(symbol)
        import yfinance as yf

        return yf.Ticker(symbol)

    def _history(self, symbol: str, **kwargs) -> pd.DataFrame:
        try:
            frame = self._ticker(symbol).history(**kwargs)
        except Exception as exc:  # noqa: BLE001 - upstream raises many types
            raise MarketDataUnavailable(f"{symbol}: history({kwargs}) failed: {exc}") from exc
        if frame is None or len(frame) == 0:
            raise MarketDataUnavailable(f"{symbol}: history({kwargs}) returned no bars")
        return frame

    def get_daily_bars(self, symbol: str, lookback_days: int = 400) -> pd.DataFrame:
        return self._history(symbol, period=_period_for_days(lookback_days), interval="1d")

    def get_intraday_bars(
        self,
        symbol: str,
        interval: str = "1m",
        lookback_days: int = 5,
        include_prepost: bool = True,
    ) -> pd.DataFrame:
        period = _capped_period(lookback_days, interval)
        requested = _period_for_days(lookback_days)
        if period != requested:
            logger.debug("%s: %s lookback %sd (%s) capped to %s",
                         symbol, interval, lookback_days, requested, period)
        return self._history(
            symbol,
            period=period,
            interval=interval,
            prepost=bool(include_prepost),
        )


#: DEPRECATED transitional aliases. Do not use in new code.
#:
#: `AlpacaMarketDataProvider` was the original name and is wrong: this
#: class fetches from Yahoo Finance, not from Alpaca. `YFinance...` was
#: an intermediate rename. Both are kept only so that anything importing
#: them keeps working, and both resolve to a provider whose
#: `provider_name` is `"yfinance"` -- so even code using the old name
#: records the truthful vendor on every signal it produces.
#:
#: Remove once nothing references them. A genuine Alpaca Market Data
#: implementation, if written, must be a NEW class with its own
#: `provider_name`, never this alias repointed.
AlpacaMarketDataProvider = YahooFinanceMarketDataProvider
YFinanceMarketDataProvider = YahooFinanceMarketDataProvider


class CachingMarketDataProvider(BarMarketDataProvider):
    """Per-run memoisation in front of another provider.

    The runner already fetches one bundle per symbol, so this is not
    what makes the six-scanner fan-out affordable. It exists for the
    analytics side: the performance tracker (section 12) walks every
    signal from a day, and the same symbol routinely appears under three
    or four scanners (section 6 explicitly keeps those duplicates). One
    fetch per symbol per process instead of one per signal.

    Not time-limited and not persisted -- an instance is meant to live
    for one run and be discarded, so there is no way for it to serve a
    stale bar into a later run.
    """

    def __init__(self, inner: BarMarketDataProvider):
        self._inner = inner
        self._cache: Dict[Tuple, object] = {}
        self.name = f"cached:{getattr(inner, 'name', 'unknown')}"
        # Provenance passes THROUGH the cache unchanged. Caching is a
        # performance decision, not a data source: a signal served from
        # this wrapper came from the same vendor as one served directly,
        # and recording "cached" as the provider would put a value in the
        # month-1 dataset that names no vendor at all. `default_provider`
        # wraps every real run in this class, so getting it wrong would
        # mean production never recorded a usable provider name.
        self.provider_name = getattr(inner, "provider_name", "unknown")
        self.feed_name = getattr(inner, "feed_name", None)
        # The interval contract passes through for the same reason the
        # provenance does. A wrapper that answered the BASE class's "5m"
        # while memoising a 1m-only provider would reintroduce the exact
        # disagreement this declaration exists to remove -- and it would
        # do it only in the cached configuration, which is the one
        # production runs.
        self.preferred_intraday_interval = getattr(
            inner, "preferred_intraday_interval",
            BarMarketDataProvider.preferred_intraday_interval)
        self.supported_intraday_intervals = getattr(
            inner, "supported_intraday_intervals", ())

    def _memo(self, key, produce):
        if key in self._cache:
            value = self._cache[key]
            if isinstance(value, Exception):
                raise value
            return value
        try:
            value = produce()
        except MarketDataUnavailable as exc:
            # Negative results are cached too. Without this, a symbol
            # that is delisted costs one failed network round trip per
            # signal that references it.
            self._cache[key] = exc
            raise
        self._cache[key] = value
        return value

    def get_daily_bars(self, symbol: str, lookback_days: int = 400) -> pd.DataFrame:
        return self._memo(
            ("daily", symbol, int(lookback_days)),
            lambda: self._inner.get_daily_bars(symbol, lookback_days=lookback_days),
        )

    def get_intraday_bars(
        self,
        symbol: str,
        interval: str = "1m",
        lookback_days: int = 5,
        include_prepost: bool = True,
    ) -> pd.DataFrame:
        return self._memo(
            ("intraday", symbol, interval, int(lookback_days), bool(include_prepost)),
            lambda: self._inner.get_intraday_bars(
                symbol,
                interval=interval,
                lookback_days=lookback_days,
                include_prepost=include_prepost,
            ),
        )


class StaticMarketDataProvider(BarMarketDataProvider):
    """A provider backed by in-memory frames, for tests and dry runs.

    Lets the full runner -- exception isolation, result store, reason
    logging, the lot -- be exercised end to end with no network, which
    is how the "API empty response" and "missing bar" cases of section
    28 are tested against the real code path rather than a mock of it.
    """

    name = "static"
    provider_name = "static"
    feed_name = None

    def __init__(self, daily=None, intraday=None):
        self._daily = dict(daily or {})
        self._intraday = dict(intraday or {})

    def get_daily_bars(self, symbol: str, lookback_days: int = 400) -> pd.DataFrame:
        frame = self._daily.get(symbol)
        if frame is None or len(frame) == 0:
            raise MarketDataUnavailable(f"{symbol}: no static daily bars")
        return frame.tail(int(lookback_days))

    def get_intraday_bars(
        self,
        symbol: str,
        interval: str = "1m",
        lookback_days: int = 5,
        include_prepost: bool = True,
    ) -> pd.DataFrame:
        frame = self._intraday.get(symbol)
        if frame is None or len(frame) == 0:
            raise MarketDataUnavailable(f"{symbol}: no static intraday bars")
        return frame


def default_provider(*, cached: bool = True) -> BarMarketDataProvider:
    provider = YahooFinanceMarketDataProvider()
    return CachingMarketDataProvider(provider) if cached else provider
