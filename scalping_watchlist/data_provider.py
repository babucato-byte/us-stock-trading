"""Market data access for the scalping watchlist engine.

A thin interface (`MarketDataProvider`) so the rest of the pipeline never
imports yfinance directly — tests inject a `FakeMarketDataProvider`
instead, guaranteeing zero real network calls. `snapshot=None` or a raised
exception both mean "this symbol's data is unavailable"; the pipeline
treats that as a per-symbol rejection (NOT_AVAILABLE / rejected), never as
a reason to fail the whole run — an unrelated symbol's API hiccup must not
block every other candidate (Phase 2 instructions, section 6).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
UNIVERSE_FILE = BASE_DIR / "universe.csv"


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
    data_is_stale: bool = False


class MarketDataProvider:
    """Interface. Subclasses must implement both methods."""

    def get_universe_symbols(self):
        raise NotImplementedError

    def get_symbol_snapshot(self, symbol, session):
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

    def get_symbol_snapshot(self, symbol, session):
        import yfinance as yf

        from daily_candidate_scanner import calculate_atr  # reuse, not reimplement (pure function)

        df = yf.Ticker(symbol).history(period=self.history_period)
        if df.empty or len(df) < 2:
            return None

        price = float(df["Close"].iloc[-1])
        previous_close = float(df["Close"].iloc[-2])
        current_volume = float(df["Volume"].iloc[-1])
        window = df["Volume"].tail(self.avg_volume_window)
        average_volume = float(window.mean()) if not window.empty else None
        atr_series = calculate_atr(df)
        atr = float(atr_series.iloc[-1]) if not atr_series.empty and pd.notna(atr_series.iloc[-1]) else None

        premarket_volume = None
        if session == "premarket":
            premarket_volume = self._fetch_premarket_volume(symbol)

        return SymbolSnapshot(
            symbol=symbol,
            price=price,
            previous_close=previous_close,
            current_volume=current_volume,
            average_volume=average_volume,
            atr=atr,
            premarket_volume=premarket_volume,
        )

    def _fetch_premarket_volume(self, symbol):
        import yfinance as yf
        from market_hours import eastern_now

        try:
            intraday = yf.Ticker(symbol).history(period="1d", interval="1m", prepost=True)
        except Exception as exc:
            print(f"{symbol}: premarket volume fetch failed: {exc}")
            return None
        if intraday.empty:
            return None
        today = eastern_now().date()
        premarket_rows = intraday[
            (intraday.index.tz_convert("America/New_York").date == today)
            & (intraday.index.tz_convert("America/New_York").hour < 9)
        ]
        if premarket_rows.empty:
            return None
        return float(premarket_rows["Volume"].sum())


class FakeMarketDataProvider(MarketDataProvider):
    """Test double: fully scripted, no network access whatsoever."""

    def __init__(self, universe_symbols=None, snapshots=None):
        self._universe_symbols = universe_symbols or []
        self._snapshots = snapshots or {}  # symbol -> SymbolSnapshot or Exception or None
        self.requested_symbols = []

    def get_universe_symbols(self):
        return list(self._universe_symbols)

    def get_symbol_snapshot(self, symbol, session):
        self.requested_symbols.append(symbol)
        result = self._snapshots.get(symbol)
        if isinstance(result, Exception):
            raise result
        return result
