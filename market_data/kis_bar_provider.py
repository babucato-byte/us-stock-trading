"""The scanner's market data, from KIS instead of yfinance.

Why
---
S6's premarket scan on 2026-08-31: universe 83, DATA_ERROR 77, evaluated
6, signals 0. The provider has no usable premarket intraday data, so 93%
of the candidates died before a single strategy rule ran. It was read as
a quiet market for weeks.

KIS has the data. Measured live at 05:02 ET, premarket bars with real
volume: RIG 12 bars / 1,708 shares, F 10 / 2,146, AAPL 66 / 43,442 --
every S6 feature (VWAP, EMA9, EMA21, ORB, expansion) computes from them.

This implements the SAME `BarMarketDataProvider` interface the yfinance
one does, so nothing downstream changes: the scanner asks for bars and
gets bars. What changes is who answers, and in which sessions.

Daily bars still come from the fallback
---------------------------------------
KIS's daily endpoint returns 100 rows, which is fine for most things and
short for a 400-day lookback. Rather than quietly serve a truncated
history where a long one was asked for -- eligibility and ATR-style
measures read these -- daily requests go to the wrapped provider. This
class exists to fix intraday extended-hours data, which is the actual
defect, and widening it further would be a second change wearing the
first one's justification.
"""

import logging
from typing import Optional

import pandas as pd

from scanners.base.market_data_provider import (
    BarMarketDataProvider, MarketDataUnavailable, UnsupportedIntervalError,
)

logger = logging.getLogger(__name__)


class KISBarMarketDataProvider(BarMarketDataProvider):
    """Intraday bars from KIS; everything else from `fallback`."""

    name = "kis"
    provider_name = "kis"
    #: KIS reports no upstream feed name for the chart endpoint, and a
    #: guessed one is worse than a null because a null is visibly
    #: unknown.
    feed_name = None

    #: The whole contract, declared once.
    #:
    #: `HHDFS76950200` takes an NMIN and this asks for 1. Everything that
    #: needs to know -- the guard below, and any caller with no opinion
    #: about resolution -- reads these two, so the interval this provider
    #: SERVES and the interval a caller REQUESTS cannot drift apart. They
    #: did between 2026-08-31 and 2026-09-04, and the three extended
    #: sessions produced no tradeable candidate for the duration.
    supported_intraday_intervals = ("1m", "1min", "1")
    preferred_intraday_interval = "1m"

    def __init__(self, *, broker=None, fallback=None, exchange_for=None,
                 trading_day=None):
        self._broker = broker
        self._fallback = fallback
        self._exchange_for = exchange_for
        self._trading_day = trading_day

    # -- daily -----------------------------------------------------------

    def get_daily_bars(self, symbol: str, lookback_days: int = 400):
        if self._fallback is None:
            raise MarketDataUnavailable(
                f"{symbol}: no daily-bar provider configured")
        return self._fallback.get_daily_bars(symbol, lookback_days=lookback_days)

    # -- intraday --------------------------------------------------------

    def _exchange(self, symbol):
        if self._exchange_for is not None:
            return self._exchange_for(symbol)
        from market_data.bootstrap_watchlist import _exchange_for

        return _exchange_for(symbol)

    def get_intraday_bars(self, symbol: str, interval: str = "1m",
                          lookback_days: int = 5,
                          include_prepost: bool = True):
        """One-minute bars for `symbol`, oldest first.

        Only 1-minute is served. KIS's chart endpoint takes an NMIN and
        this could ask for 5 or 15, but every other interval would then
        be silently resampled from a different upstream than the caller
        expects -- so an unsupported interval is refused rather than
        approximated.

        The refusal is `UnsupportedIntervalError`, which is still a
        `MarketDataUnavailable` and still costs at most one symbol. What
        the specific type carries is that this is a wiring fault rather
        than a thin book, so `get_symbol_data` can say so out loud
        instead of filing it beside every quiet stock.
        """
        if not self.serves_intraday_interval(interval):
            raise UnsupportedIntervalError(
                f"{symbol}: KIS provider serves 1m bars, not {interval!r}",
                requested=interval,
                supported=self.supported_intraday_intervals)

        broker = self._broker
        if broker is None:
            raise MarketDataUnavailable(f"{symbol}: no KIS broker configured")

        exchange = self._exchange(symbol)
        if not exchange:
            raise MarketDataUnavailable(
                f"{symbol}: no exchange mapping, so KIS cannot be asked")

        from market_data import kis_minute_chart

        # Unfiltered by day on purpose. The caller's own session logic
        # decides what belongs to this session; an EMA legitimately seeds
        # from earlier bars, while VWAP and the opening range must not.
        # Filtering here would take that choice away from both.
        records = kis_minute_chart.fetch(
            broker, symbol=symbol, exchange=exchange,
            trading_day=self._trading_day)
        if not records:
            raise MarketDataUnavailable(
                f"{symbol}: KIS returned no minute bars")

        frame = pd.DataFrame([{
            "Open": r["open"], "High": r["high"], "Low": r["low"],
            "Close": r["close"], "Volume": r["volume"],
        } for r in records], index=pd.DatetimeIndex(
            [r["at"] for r in records], name="Datetime"))
        return frame

    def get_latest_price(self, symbol: str) -> Optional[float]:
        try:
            frame = self.get_intraday_bars(symbol, interval="1m")
        except MarketDataUnavailable:
            return None
        if frame is None or len(frame) == 0:
            return None
        return float(frame["Close"].iloc[-1])


#: Sessions where the yfinance path is known to fail and KIS is the
#: authority.
#:
#: REGULAR is deliberately absent. It is not that KIS would be worse
#: there -- it is that REGULAR currently WORKS, and produced every live
#: S6 trade so far. Swapping a working data path buys nothing this phase
#: needs and risks the one session with a track record; the extended
#: sessions are where 77 of 83 candidates were dying.
KIS_AUTHORITATIVE_SESSIONS = frozenset({
    "PREMARKET", "AFTER_HOURS", "OVERNIGHT_DAYTIME",
})


def provider_for_session(session, *, broker=None, fallback=None,
                         trading_day=None) -> BarMarketDataProvider:
    """The right provider for `session`, falling back when KIS cannot be
    reached at all."""
    from scanners.base.market_data_provider import default_provider

    base = fallback if fallback is not None else default_provider()
    if str(session or "").upper() not in KIS_AUTHORITATIVE_SESSIONS:
        return base
    if broker is None:
        logger.warning(
            "session %s wants KIS bars but no broker was supplied; falling "
            "back to %s, whose extended-hours data is why this exists",
            session, getattr(base, "provider_name", "?"))
        return base
    return KISBarMarketDataProvider(broker=broker, fallback=base,
                                    trading_day=trading_day)
