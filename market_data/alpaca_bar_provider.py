"""Scanner bars for PREMARKET and AFTER_HOURS, fetched for the whole
universe at once.

The shape of the change
-----------------------
`KISBarMarketDataProvider` answers one symbol per call because its
upstream does. This one PREFETCHES: `prime()` pulls every symbol the scan
is about to ask for in a handful of batched requests, and
`get_intraday_bars` then serves each symbol from memory. The
`BarMarketDataProvider` contract is unchanged -- the scanner still asks
for one symbol and gets a frame -- so nothing downstream of this file
knows the difference.

That is the entire performance story. 13,409 symbols in 104.7s (68 pages)
against ~5.93s PER SYMBOL on the KIS path, measured 2026-09-16.

Coverage, and why this provider does not need a backfill
--------------------------------------------------------
`s6_live/origin_backfill.py` exists because the KIS collector connects a
few seconds AFTER the session opens, so the official origin minute may
never have been watched. A historical tape has no such hole: the window
is requested by time, and the feed either published a minute or the
market was quiet in it. `coverage_started_at` reports the window this
provider actually asked for, so a caller can check origin coverage the
same way -- and it is the requested start, not the first bar seen, which
is exactly the distinction that lets "quiet" and "unwatched" stay apart.

What is NOT claimed
-------------------
This account's SIP entitlement is HISTORICAL ONLY -- measured lag 15.3
to 17.3 minutes (2026-09-16), with Alpaca answering 403 "subscription
does not permit querying recent SIP data" on the realtime endpoint.
So this provider must not be made authoritative for a live breakout
decision: the freshness gate would be reading a tape that is a quarter of
an hour behind. `is_fresh_enough()` states that in code rather than in a
comment, and `SHADOW_ONLY` marks the intended role until the entitlement
changes.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Sequence

import pandas as pd

from market_data.alpaca_data_client import (
    AlpacaDataClient, AlpacaDataError, FRESH_WITHIN_SECONDS,
)
from scanners.base.market_data_provider import (
    BarMarketDataProvider, MarketDataUnavailable, UnsupportedIntervalError,
)

logger = logging.getLogger(__name__)

#: The sessions this provider is built for. REGULAR is absent because it
#: works today on the Yahoo path and produced every live S6 trade so far;
#: OVERNIGHT_DAYTIME is absent because 미국주간거래 is a KIS-native venue
#: (BAQ/BAY/BAA) that the US consolidated tape does not carry at all.
BROAD_PROVIDER_SESSIONS = frozenset({"PREMARKET", "AFTER_HOURS"})

#: Until the subscription serves recent SIP, this provider compares
#: against the authoritative path; it does not replace it.
SHADOW_ONLY = True


class AlpacaBarMarketDataProvider(BarMarketDataProvider):
    """Bulk-prefetched 1-minute SIP bars; daily from `fallback`."""

    name = "alpaca_sip"
    provider_name = "alpaca_sip"
    #: Observed, not guessed. The client asks for `feed=sip` explicitly
    #: and refuses to substitute another, so this name is a fact about
    #: the request rather than an inference about the response.
    feed_name = "sip"

    supported_intraday_intervals = ("1m", "1min", "1")
    preferred_intraday_interval = "1m"

    def __init__(self, *, client=None, fallback=None, session=None,
                 window=None, now_fn=None):
        self._client = client
        self._fallback = fallback
        self._session = str(session or "").upper() or None
        self._window = window          # (start, end), both tz-aware UTC
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._bars: Dict[str, list] = {}
        self._primed = False
        self._coverage_started_at: Optional[datetime] = None
        #: Parity/observability counters (§12).
        self.symbols_requested = 0
        self.symbols_returned = 0
        self.duplicate_rows_dropped = 0
        self.partial_bars_dropped = 0

    # -- bulk prefetch ---------------------------------------------------

    def prime(self, symbols: Sequence[str], *, start=None, end=None) -> dict:
        """Fetch every symbol for the session window, in bulk.

        The universe is whatever is handed in. Nothing here caps, pads or
        truncates it -- a scan of 300 and a scan of 13,409 differ only in
        how many batches the client sends, and no strategy result may
        change because of that count.
        """
        if self._client is None:
            raise MarketDataUnavailable("no Alpaca data client configured")

        window_start, window_end = self._resolve_window(start, end)
        wanted = [str(s).strip().upper() for s in symbols if str(s).strip()]
        self.symbols_requested = len(set(wanted))

        raw = self._client.fetch_bars(wanted, start=window_start,
                                      end=window_end)
        self._coverage_started_at = window_start
        self._bars = {symbol: self._prepare(rows)
                      for symbol, rows in raw.items()}
        # A symbol whose every row was dropped is quiet, not covered --
        # it must not linger as an empty entry that reads like a frame.
        self._bars = {s: rows for s, rows in self._bars.items() if rows}
        self.symbols_returned = len(self._bars)
        self._primed = True

        return {
            "symbols_requested": self.symbols_requested,
            "symbols_returned": self.symbols_returned,
            "bar_points": sum(len(r) for r in self._bars.values()),
            "pages": getattr(self._client, "page_count", 0),
            "requests": getattr(self._client, "request_count", 0),
            "fetch_seconds": getattr(self._client, "fetch_seconds", 0.0),
            "coverage_started_at": window_start,
            "coverage_ended_at": window_end,
            "duplicate_rows_dropped": self.duplicate_rows_dropped,
            "partial_bars_dropped": self.partial_bars_dropped,
        }

    def _resolve_window(self, start, end):
        if start is not None:
            return _aware(start), _aware(end) if end is not None else None
        if self._window:
            return _aware(self._window[0]), _aware(self._window[1])
        raise MarketDataUnavailable(
            "no session window supplied; this provider fetches by time and "
            "cannot guess which session it is serving")

    def _prepare(self, rows) -> list:
        """Closed bars only, de-duplicated, oldest first.

        Duplicates are handled EXPLICITLY rather than by trusting the
        feed: Alpaca republishes a corrected bar under the same timestamp,
        and the later row is the corrected one. Keeping both would double
        that minute's volume; keeping the first would keep the version the
        exchange has already retracted.
        """
        by_minute = {}
        for row in rows:
            minute = row["at"]
            if minute in by_minute:
                self.duplicate_rows_dropped += 1
            by_minute[minute] = row       # last wins: the correction

        cutoff = self._now() - timedelta(seconds=60)
        kept = []
        for minute in sorted(by_minute):
            # A bar stamped at the top of the CURRENT minute is still
            # accumulating. Feeding a partial bar to an opening-range or
            # a volume-expansion test produces a number that changes
            # under the caller's feet.
            if minute > cutoff:
                self.partial_bars_dropped += 1
                continue
            kept.append(by_minute[minute])
        return kept

    # -- the provider contract -------------------------------------------

    def get_daily_bars(self, symbol: str, lookback_days: int = 400):
        """Delegated, for the same reason the KIS provider delegates.

        This class exists to fix extended-hours INTRADAY breadth. Serving
        daily history from a second vendor would change the eligibility
        and ATR inputs too, which is a different change wearing this
        one's justification.
        """
        if self._fallback is None:
            raise MarketDataUnavailable(
                f"{symbol}: no daily-bar provider configured")
        return self._fallback.get_daily_bars(symbol, lookback_days=lookback_days)

    def get_intraday_bars(self, symbol: str, interval: str = "1m",
                          lookback_days: int = 5,
                          include_prepost: bool = True):
        """The prefetched 1-minute frame for `symbol`, oldest first."""
        if not self.serves_intraday_interval(interval):
            raise UnsupportedIntervalError(
                f"{symbol}: Alpaca provider serves 1m bars, not {interval!r}",
                requested=interval,
                supported=self.supported_intraday_intervals)
        if not self._primed:
            raise MarketDataUnavailable(
                f"{symbol}: provider not primed; call prime() with the "
                "universe before asking for a symbol")

        rows = self._bars.get(str(symbol).upper())
        if not rows:
            # Covered, and quiet. The distinction matters: the window WAS
            # fetched for this symbol, so this is a measured absence of
            # prints and not an absence of coverage.
            raise MarketDataUnavailable(
                f"{symbol}: no SIP prints in the session window "
                f"(covered from {self._coverage_started_at})")

        return pd.DataFrame([{
            "Open": r["open"], "High": r["high"], "Low": r["low"],
            "Close": r["close"], "Volume": r["volume"],
        } for r in rows], index=pd.DatetimeIndex(
            [r["at"] for r in rows], name="Datetime"))

    # -- coverage and freshness ------------------------------------------

    @property
    def coverage_started_at(self) -> Optional[datetime]:
        """The first minute this provider ASKED for.

        Deliberately the requested start rather than the first bar seen.
        A symbol whose first print is late is quiet at the open; a
        provider whose coverage begins late cannot tell. Reporting the
        request keeps those two apart.
        """
        return self._coverage_started_at

    def covers_origin(self, official_origin) -> Optional[bool]:
        if self._coverage_started_at is None or official_origin is None:
            return None
        return self._coverage_started_at <= _aware(official_origin)

    def is_fresh_enough(self, *, within_seconds=FRESH_WITHIN_SECONDS):
        """Whether the newest available bar is recent enough to trade on.

        Returns (ok, lag_seconds). On this subscription the answer is
        False by ~15-17 minutes, which is precisely why authority has not
        been switched -- see the module docstring.
        """
        if self._client is None:
            return (False, None)
        try:
            lag = self._client.newest_bar_lag_seconds(now=self._now())
        except AlpacaDataError:
            return (False, None)
        if lag is None:
            return (False, None)
        return (lag <= float(within_seconds), lag)


def _aware(moment):
    if moment is None:
        return None
    if getattr(moment, "tzinfo", None) is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def broad_provider_for_session(session, *, fallback=None, window=None,
                               client=None) -> Optional[AlpacaBarMarketDataProvider]:
    """The broad provider for `session`, or None if it does not apply.

    Explicit selection (§6): PREMARKET and AFTER_HOURS only. REGULAR and
    OVERNIGHT_DAYTIME return None and keep the providers they have. This
    function does NOT read a feature flag and does not decide authority
    -- it builds the object; `scripts/run_scanners.py` still chooses what
    production actually uses, and today that choice is unchanged.
    """
    name = str(session or "").upper()
    if name not in BROAD_PROVIDER_SESSIONS:
        return None
    if client is None:
        try:
            client = AlpacaDataClient()
        except AlpacaDataError as exc:
            logger.warning("no Alpaca data client for %s: %s", name, exc)
            return None
    return AlpacaBarMarketDataProvider(
        client=client, fallback=fallback, session=name, window=window)
