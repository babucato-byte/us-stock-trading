"""Market data access for the scalping watchlist engine.

A thin interface (`MarketDataProvider`) so the rest of the pipeline never
imports yfinance directly — tests inject a `FakeMarketDataProvider`
instead, guaranteeing zero real network calls. `snapshot=None` or a raised
exception both mean "this symbol's data is unavailable"; the pipeline
treats that as a per-symbol rejection (NOT_AVAILABLE / rejected), never as
a reason to fail the whole run — an unrelated symbol's API hiccup must not
block every other candidate (Phase 2 instructions, section 6).

CODEX-011: a snapshot carries two distinct timestamps, and confusing them
was the root cause of the original stale-data gate being inert:

- `data_as_of`: when the underlying bar actually happened (the market
  data's own timestamp). This is what freshness is measured against.
- `provider_fetched_at`: when this process asked the provider for data.
  Always "now" relative to the call — recording *this* as freshness would
  make every response look fresh regardless of how old the bar itself is,
  which is exactly the bug CODEX-011 found in the pre-fix provider.

The provider's only job is to report both accurately; freshness.py (not
this module) decides whether the gap between them is acceptable.
"""

from dataclasses import dataclass
from datetime import datetime, time as _time, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
UNIVERSE_FILE = BASE_DIR / "universe.csv"

_PREMARKET_START = _time(4, 0)
_PREMARKET_END = _time(9, 30)


@dataclass
class SymbolSnapshot:
    """Raw market data for one symbol. None fields mean "could not be
    computed" — callers must not invent a value for them."""

    symbol: str
    price: Optional[float]
    previous_close: Optional[float]
    current_volume: Optional[float]
    average_volume: Optional[float]
    atr: Optional[float]
    premarket_volume: Optional[float] = None  # only meaningful in the premarket session
    data_as_of: Optional[datetime] = None       # timezone-aware; timestamp of the actual last bar
    provider_fetched_at: Optional[datetime] = None  # timezone-aware; when this snapshot was built
    source: str = "unknown"
    # Provider-asserted staleness (e.g. the provider itself detected a
    # malformed response it can't quantify an age for). The pipeline's own
    # freshness.py check is the primary gate and does not depend on this
    # flag being set correctly — it independently compares data_as_of.
    data_is_stale: bool = False
    premarket_coverage_start: Optional[str] = None  # ISO time-of-day, e.g. "04:00"
    premarket_coverage_end: Optional[str] = None
    premarket_coverage_complete: bool = False


class MarketDataProvider:
    """Interface. Subclasses must implement both methods.

    `now` is the pipeline's own evaluation time (timezone-aware), passed
    through so a provider can stamp `provider_fetched_at` consistently;
    real providers should still derive `data_as_of` strictly from the
    underlying data, never from `now`.
    """

    def get_universe_symbols(self):
        raise NotImplementedError

    def get_symbol_snapshot(self, symbol, session, now=None):
        raise NotImplementedError


def load_universe_symbols(universe_file=UNIVERSE_FILE):
    """Read universe.csv's tradable symbol list (reuses the same file/format
    daily_candidate_scanner.py and universe_builder.py already produce —
    does not duplicate the OTC-filtering logic beyond the same dedup/column
    read, since that is trivial I/O, not a formula)."""
    try:
        df = pd.read_csv(universe_file)
    except Exception as exc:
        print(f"Failed to read {universe_file}: {exc}")
        return []
    if "symbol" not in df.columns:
        return []
    if "exchange" in df.columns:
        df = df[df["exchange"] != "OTC"]
    return df["symbol"].dropna().astype(str).unique().tolist()


class YFinanceMarketDataProvider(MarketDataProvider):
    """Real provider. Never imported by tests — only by the pipeline's
    production entrypoint."""

    def __init__(self, universe_file=UNIVERSE_FILE, history_period="60d", avg_volume_window=20):
        self.universe_file = universe_file
        self.history_period = history_period
        self.avg_volume_window = avg_volume_window

    def get_universe_symbols(self):
        return load_universe_symbols(self.universe_file)

    def get_symbol_snapshot(self, symbol, session, now=None):
        import yfinance as yf

        from daily_candidate_scanner import calculate_atr  # reuse, not reimplement (pure function)

        fetched_at = datetime.now(timezone.utc)  # always real wall-clock; `now` is not used to derive data_as_of

        df = yf.Ticker(symbol).history(period=self.history_period)

        # CODEX-011 fail-closed conditions: never guess at a timestamp.
        if df.empty or len(df) < 2:
            print(f"{symbol}: empty or insufficient history from provider")
            return None
        if not isinstance(df.index, pd.DatetimeIndex):
            print(f"{symbol}: history index is not a timestamp index")
            return None
        if df.index.tz is None:
            print(f"{symbol}: history index has no timezone")
            return None
        if not df.index.is_monotonic_increasing:
            print(f"{symbol}: history index is not sorted ascending")
            return None
        if df.index.duplicated().any():
            print(f"{symbol}: history contains duplicate bar timestamps")
            return None

        try:
            data_as_of = df.index[-1].to_pydatetime().astimezone(timezone.utc)
        except Exception as exc:
            print(f"{symbol}: failed to parse last bar timestamp: {exc}")
            return None

        if data_as_of > fetched_at:
            print(f"{symbol}: last bar timestamp {data_as_of} is in the future relative to fetch time")
            return None

        price = float(df["Close"].iloc[-1])
        previous_close = float(df["Close"].iloc[-2])
        current_volume = float(df["Volume"].iloc[-1])
        average_volume = self._compute_average_volume(df, symbol)
        atr_series = calculate_atr(df)
        atr = float(atr_series.iloc[-1]) if not atr_series.empty and pd.notna(atr_series.iloc[-1]) else None

        premarket_volume = None
        coverage_start = coverage_end = None
        coverage_complete = False
        if session == "premarket":
            premarket_volume, coverage_start, coverage_end, coverage_complete = self._fetch_premarket_volume(symbol)

        return SymbolSnapshot(
            symbol=symbol,
            price=price,
            previous_close=previous_close,
            current_volume=current_volume,
            average_volume=average_volume,
            atr=atr,
            premarket_volume=premarket_volume,
            data_as_of=data_as_of,
            provider_fetched_at=fetched_at,
            source="yfinance",
            premarket_coverage_start=coverage_start,
            premarket_coverage_end=coverage_end,
            premarket_coverage_complete=coverage_complete,
        )

    def _compute_average_volume(self, df, symbol):
        """CODEX-015: the current/most recent bar is assumed to be the
        current (possibly still-open) trading day and is always excluded
        before averaging — including it would let a partial day's volume
        drag the average down mid-session, which then inflates
        relative_volume (current_volume / average_volume) artificially.
        Requires at least MIN_VALID_VOLUME_DAYS of completed history;
        returns None (never a value computed from too little data) if
        that isn't met, which features.py's finite-number validation
        (CODEX-010) will correctly turn into AVERAGE_VOLUME_UNAVAILABLE.
        """
        from config import scalping_watchlist_config as cfg

        completed_days = df["Volume"].iloc[:-1]
        lookback = completed_days.tail(cfg.AVERAGE_VOLUME_LOOKBACK_DAYS)
        if len(lookback) < cfg.MIN_VALID_VOLUME_DAYS:
            print(
                f"{symbol}: insufficient completed trading days for average volume "
                f"({len(lookback)} < {cfg.MIN_VALID_VOLUME_DAYS})"
            )
            return None
        return float(lookback.mean())

    def _fetch_premarket_volume(self, symbol):
        import yfinance as yf
        from market_hours import eastern_now

        try:
            intraday = yf.Ticker(symbol).history(period="1d", interval="1m", prepost=True)
        except Exception as exc:
            print(f"{symbol}: premarket volume fetch failed: {exc}")
            return None, None, None, False
        if intraday.empty:
            return None, None, None, False
        today = eastern_now().date()
        return filter_premarket_rows(intraday, today)


def filter_premarket_rows(intraday, today_et_date):
    """CODEX-015: pure function isolating the premarket time-boundary
    filtering (04:00 inclusive through 09:30 exclusive ET) so it can be
    unit tested with a constructed DataFrame, without invoking yfinance.

    `intraday` must have a tz-aware DatetimeIndex (any timezone; converted
    to America/New_York here) with a "Volume" column. Returns
    (volume_sum_or_None, "04:00", "09:30", coverage_complete) — mirrors
    the tuple shape `_fetch_premarket_volume` returns to callers.
    """
    et_index = intraday.index.tz_convert("America/New_York")
    premarket_mask = (
        (et_index.date == today_et_date)
        & (et_index.time >= _PREMARKET_START)
        & (et_index.time < _PREMARKET_END)
    )
    premarket_rows = intraday[premarket_mask]
    if premarket_rows.empty:
        return None, "04:00", "09:30", False
    observed_start = et_index[premarket_mask].min().time()
    coverage_complete = observed_start <= _PREMARKET_START
    return float(premarket_rows["Volume"].sum()), "04:00", "09:30", coverage_complete


class FakeMarketDataProvider(MarketDataProvider):
    """Test double: fully scripted, no network access whatsoever."""

    def __init__(self, universe_symbols=None, snapshots=None):
        self._universe_symbols = universe_symbols or []
        self._snapshots = snapshots or {}  # symbol -> SymbolSnapshot or Exception or None
        self.requested_symbols = []

    def get_universe_symbols(self):
        return list(self._universe_symbols)

    def get_symbol_snapshot(self, symbol, session, now=None):
        import dataclasses

        self.requested_symbols.append(symbol)
        result = self._snapshots.get(symbol)
        if isinstance(result, Exception):
            raise result
        if result is not None and now is not None:
            # Tests that don't care about freshness don't have to set
            # data_as_of/provider_fetched_at explicitly — default to "as of
            # right now" (whatever `now` the test injected into this call).
            # A copy is returned so the stored fixture is never mutated:
            # the same FakeMarketDataProvider is often reused across
            # several run_scan_cycle() calls at different `now` values
            # (repeat-tracker tests), and each call must independently see
            # "fresh as of its own now", not a value frozen from the first
            # call. Staleness tests set data_as_of explicitly, which this
            # never overrides.
            updates = {}
            if result.data_as_of is None:
                updates["data_as_of"] = now
            if result.provider_fetched_at is None:
                updates["provider_fetched_at"] = now
            if updates:
                result = dataclasses.replace(result, **updates)
        return result
