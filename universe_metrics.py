"""T8: bulk price / average-dollar-volume lookup for the whole asset pool.

`daily_candidate_scanner.analyze()` already fetches per-symbol history via
`yf.Ticker(symbol).history(period="1y")`, but that is one HTTP round trip
per symbol and a full year of bars -- ~12,900 of those just to decide
which symbols are worth scanning would cost far more than the scan it is
meant to narrow. This module reuses the SAME data source (yfinance, which
is what `market_data/alpaca_provider.py` documents as this codebase's
actual price feed) through its batch endpoint instead, and asks only for
the trailing window the two thresholds need.

The provider is an injectable interface. `universe_builder` depends on
the interface, so the entire filtered-universe build is exercised in
tests with a fake provider and zero network access.

Averaging window (CODEX-015 convention, same as
`config/scalping_watchlist_config.py`): the most recent bar is dropped
before averaging volume, because while the market is open that bar's
volume is necessarily partial and would understate liquidity. The latest
*price* still comes from that most recent bar -- a partial-day close is
the freshest price available and is not distorted the way partial volume
is.
"""

import math
from typing import Dict, Iterable, List

from universe_filter import SymbolMetrics

DEFAULT_CHUNK_SIZE = 200
DEFAULT_HISTORY_PERIOD = "3mo"
DEFAULT_VOLUME_WINDOW_DAYS = 20
MIN_VALID_VOLUME_DAYS = 5
# How far back a usable close may be found before the symbol counts as
# having no price at all. See _latest_usable_close().
MAX_STALE_CLOSE_BARS = 3


class UniverseMetricsProvider:
    """Interface. `get_metrics()` must return a dict keyed by uppercase
    symbol; a symbol it could not resolve is simply absent (the filter
    then records EXCLUDED_NO_PRICE_DATA), never present with a guessed
    value."""

    def get_metrics(self, symbols: Iterable[str]) -> Dict[str, SymbolMetrics]:
        raise NotImplementedError


def chunk_symbols(symbols, chunk_size=DEFAULT_CHUNK_SIZE) -> List[List[str]]:
    if chunk_size < 1:
        raise ValueError(f"chunk_size must be >= 1, got {chunk_size!r}")
    ordered = []
    seen = set()
    for symbol in symbols:
        key = str(symbol or "").strip().upper()
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    return [ordered[i:i + chunk_size] for i in range(0, len(ordered), chunk_size)]


def _finite_positive(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _finite_non_negative(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _latest_usable_close(frame):
    """The most recent usable close, searching back at most
    MAX_STALE_CLOSE_BARS rows.

    yfinance routinely returns a trailing NaN row for the in-progress
    session, so requiring the very last row to be numeric would drop
    healthy symbols. Walking back without a bound would do the opposite
    and price a long-halted symbol off a weeks-old bar, so the search is
    capped: past that, the symbol has no usable price and is excluded
    (EXCLUDED_NO_PRICE_DATA) rather than sized off stale data.
    """
    try:
        closes = frame["Close"]
    except (KeyError, TypeError):
        return None
    for offset in range(1, MAX_STALE_CLOSE_BARS + 1):
        if offset > len(closes):
            return None
        price = _finite_positive(closes.iloc[-offset])
        if price is not None:
            return price
    return None


def metrics_from_frame(symbol, frame, *, volume_window_days=DEFAULT_VOLUME_WINDOW_DAYS):
    """Derives (price, average dollar volume) from one symbol's daily OHLCV
    frame. Returns None when the frame cannot support BOTH figures --
    a symbol with a price but no usable volume history is reported as
    "price known, liquidity unknown" (SymbolMetrics with
    avg_dollar_volume_usd=None) so the filter can distinguish that from
    "no data at all"; a symbol with no usable price at all returns None.
    """
    if frame is None:
        return None
    try:
        if frame.empty:
            return None
        frame["Close"]
        frame["Volume"]
    except (AttributeError, KeyError, TypeError):
        return None

    price = _latest_usable_close(frame)
    if price is None:
        return None

    # Drop the most recent bar before averaging volume (partial session).
    complete = frame.iloc[:-1] if len(frame) > 1 else frame.iloc[0:0]
    try:
        window = complete.tail(volume_window_days)
        window_closes = window["Close"]
        window_volumes = window["Volume"]
    except (AttributeError, KeyError, TypeError):
        return SymbolMetrics(symbol=symbol, price_usd=price, avg_dollar_volume_usd=None)

    dollar_volumes = []
    for close_value, volume_value in zip(window_closes, window_volumes):
        close_number = _finite_positive(close_value)
        volume_number = _finite_non_negative(volume_value)
        if close_number is None or volume_number is None:
            continue
        dollar_volumes.append(close_number * volume_number)

    if len(dollar_volumes) < MIN_VALID_VOLUME_DAYS:
        return SymbolMetrics(symbol=symbol, price_usd=price, avg_dollar_volume_usd=None)

    average = sum(dollar_volumes) / len(dollar_volumes)
    if not math.isfinite(average):  # pragma: no cover -- inputs already finite
        return SymbolMetrics(symbol=symbol, price_usd=price, avg_dollar_volume_usd=None)
    return SymbolMetrics(symbol=symbol, price_usd=price, avg_dollar_volume_usd=average)


def _extract_symbol_frame(downloaded, symbol, single_symbol_request):
    """yfinance returns a flat frame for a one-ticker request and a
    ticker-keyed MultiIndex for a multi-ticker one. Both shapes are
    handled here rather than by the caller."""
    if downloaded is None:
        return None
    if single_symbol_request:
        return downloaded
    try:
        return downloaded[symbol]
    except (KeyError, IndexError, TypeError):
        return None


class YFinanceUniverseMetricsProvider(UniverseMetricsProvider):
    """Batched yfinance provider. `download_fn` is injectable purely so
    tests never reach the network -- production uses yfinance.download."""

    def __init__(self, *, download_fn=None, chunk_size=DEFAULT_CHUNK_SIZE,
                 period=DEFAULT_HISTORY_PERIOD,
                 volume_window_days=DEFAULT_VOLUME_WINDOW_DAYS, logger=print):
        self._download_fn = download_fn
        self._chunk_size = chunk_size
        self._period = period
        self._volume_window_days = volume_window_days
        self._log = logger

    def _download(self, chunk):
        if self._download_fn is not None:
            return self._download_fn(chunk, self._period)
        import yfinance as yf  # imported lazily so tests never need the network stack

        return yf.download(
            tickers=" ".join(chunk),
            period=self._period,
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            actions=False,
            progress=False,
            threads=True,
        )

    def get_metrics(self, symbols):
        chunks = chunk_symbols(symbols, self._chunk_size)
        results = {}
        for index, chunk in enumerate(chunks, start=1):
            try:
                downloaded = self._download(chunk)
            except Exception as exc:  # noqa: BLE001
                # One failed chunk must not abort the build -- its symbols
                # simply have no metrics and are excluded with
                # EXCLUDED_NO_PRICE_DATA, which the report makes visible.
                self._log(f"[UNIVERSE METRICS] chunk {index}/{len(chunks)} failed: {exc}")
                continue
            single = len(chunk) == 1
            for symbol in chunk:
                frame = _extract_symbol_frame(downloaded, symbol, single)
                metrics = metrics_from_frame(
                    symbol, frame, volume_window_days=self._volume_window_days,
                )
                if metrics is not None:
                    results[symbol] = metrics
            self._log(
                f"[UNIVERSE METRICS] chunk {index}/{len(chunks)} resolved "
                f"{sum(1 for s in chunk if s in results)}/{len(chunk)}"
            )
        return results


class StaticUniverseMetricsProvider(UniverseMetricsProvider):
    """A provider backed by an in-memory mapping. Used by tests and by
    `scripts/` dry runs; keeps `universe_builder` free of test-only
    branches."""

    def __init__(self, metrics_by_symbol):
        self._metrics = {
            str(k).strip().upper(): v for k, v in dict(metrics_by_symbol).items()
        }

    def get_metrics(self, symbols):
        wanted = {str(s or "").strip().upper() for s in symbols}
        return {k: v for k, v in self._metrics.items() if k in wanted}
