"""Indicator helpers shared by the six scanners.

Reuse first (spec section 24)
------------------------------
Nothing here reimplements an indicator this repository already has. HMA
comes from `indicators.hma` -- the same function `technical_entry_filter`
uses to gate real orders -- and EMA/VWAP/ADX/52-week-high come from
`score_scanner.premarket_momentum_score`, the existing premarket scanner.
Those modules are imported, not copied, so a future fix to the HMA maths
reaches the new scanners and the live technical filter together instead
of drifting apart.

What IS new here is narrow and additive:

* `adx_series` -- the existing `calculate_adx` returns only the latest
  scalar, and "ADX rising" (spec S1) needs two consecutive values. The
  series is computed with byte-identical maths to the existing function
  so that `adx_series(df).dropna().iloc[-1] == calculate_adx(df)`; a
  test pins that equality, because two ADX definitions in one repository
  would silently split the meaning of "ADX > 20" between the live filter
  and the new scanners.
* `session_vwap` -- the existing `calculate_vwap` accumulates over the
  whole frame it is handed. That is correct for the premarket scanner,
  which hands it a single session. The ORB and gap-pullback scanners
  hold multi-day intraday frames, where a running cumulative VWAP across
  a day boundary is not a VWAP of anything. This resets per session.
* slope, rolling highs, distance-to-high, and the extension metrics of
  section 8.

Every function tolerates short, empty, all-NaN and zero-volume input by
returning `None` rather than raising, because "we could not compute it"
is a normal daily outcome for some symbols in an 800-name universe and
must not take a scan down (spec section 28).
"""

import math
from typing import Optional

import pandas as pd

from indicators import hma  # reused: same HMA the live technical filter uses
from score_scanner.premarket_momentum_score import (  # reused: existing premarket scanner maths
    calculate_adx,
    calculate_vwap,
    ema,
    week52_high,
)

__all__ = [
    "adx_series",
    "calculate_adx",
    "calculate_vwap",
    "distance_pct",
    "ema",
    "extension_pct",
    "hma",
    "hma_series",
    "hma_slope_pct",
    "last_valid",
    "min_bars_for_hma",
    "rolling_high",
    "safe_ratio",
    "series_rising",
    "session_vwap",
    "to_float",
    "week52_high",
]


def to_float(value) -> Optional[float]:
    """A finite float or None. NaN is an absence, not a number."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def numeric(series) -> pd.Series:
    if series is None:
        return pd.Series(dtype=float)
    return pd.to_numeric(series, errors="coerce")


def column(df, *names) -> pd.Series:
    """Fetch a column by any of its capitalisations.

    yfinance returns `Close`/`Volume`; several fixtures and CSV round
    trips produce `close`/`volume`. Accepting both here keeps every
    caller from repeating the same two-way lookup.
    """
    if df is None or not hasattr(df, "columns"):
        return pd.Series(dtype=float)
    for name in names:
        if name in df.columns:
            return numeric(df[name])
    return pd.Series(dtype=float)


def close_series(df) -> pd.Series:
    return column(df, "Close", "close")


def high_series(df) -> pd.Series:
    return column(df, "High", "high")


def low_series(df) -> pd.Series:
    return column(df, "Low", "low")


def open_series(df) -> pd.Series:
    return column(df, "Open", "open")


def volume_series(df) -> pd.Series:
    return column(df, "Volume", "volume")


def last_valid(series) -> Optional[float]:
    """The most recent non-NaN value, or None."""
    if series is None or len(series) == 0:
        return None
    valid = numeric(series).dropna()
    if valid.empty:
        return None
    return to_float(valid.iloc[-1])


def nth_last_valid(series, offset: int) -> Optional[float]:
    """`offset` values back from the end, skipping NaN. offset=0 is the
    latest."""
    if series is None:
        return None
    valid = numeric(series).dropna()
    if len(valid) <= offset:
        return None
    return to_float(valid.iloc[-1 - offset])


def safe_ratio(numerator, denominator) -> Optional[float]:
    """Division that refuses rather than producing inf.

    Every ratio in these scanners has a denominator that is legitimately
    zero for some symbol on some day -- a halted name has zero volume, a
    freshly-listed one has no average. Returning None there is what
    keeps `volume_multiple` from arriving in the month-end dataset as
    `inf` (spec section S2 explicitly calls out the divide-by-zero).
    """
    top = to_float(numerator)
    bottom = to_float(denominator)
    if top is None or bottom is None or bottom == 0:
        return None
    return to_float(top / bottom)


def min_bars_for_hma(length: int) -> int:
    """Bars required before an HMA of `length` has any value at all.

    HMA is WMA(2*WMA(n/2) - WMA(n), sqrt(n)), so the outer smoothing
    needs `length` bars for its input plus `sqrt(length) - 1` more to
    fill its own window. Getting this wrong is the difference between
    "FAIL: insufficient history" (correct, spec section 28) and a
    silently NaN HMA200 that every comparison against it evaluates
    False -- which looks like a legitimate rejection and is not.
    """
    length = int(length)
    if length <= 1:
        return 1
    return length + int(math.sqrt(length)) - 1


def hma_series(df, length: int) -> pd.Series:
    """HMA of the close, using the repository's existing `indicators.hma`."""
    close = close_series(df)
    if close.empty:
        return pd.Series(dtype=float)
    return hma(close, int(length))


def hma_slope_pct(series, lookback: int) -> Optional[float]:
    """Percentage change in an HMA over `lookback` valid bars.

    Percent rather than absolute, so a $400 stock and a $4 stock are on
    the same scale in the month-end comparison. Signed: negative means
    the long trend is still rolling over, which is precisely what S1
    exists to exclude.
    """
    current = last_valid(series)
    previous = nth_last_valid(series, int(lookback))
    if current is None or previous is None or previous == 0:
        return None
    return to_float((current / previous - 1.0) * 100.0)


def series_rising(series, lookback: int = 1) -> Optional[bool]:
    """Is the latest value above the one `lookback` valid bars back?

    None when it cannot be determined -- which callers must treat as
    "condition not met", never as True.
    """
    current = last_valid(series)
    previous = nth_last_valid(series, int(lookback))
    if current is None or previous is None:
        return None
    return bool(current > previous)


def adx_series(df, period: int = 14) -> pd.Series:
    """Full ADX series, matching `score_scanner`'s existing `calculate_adx`.

    The maths below is a line-for-line lift of that function with the
    final `.dropna().iloc[-1]` removed. It is duplicated rather than
    refactored because refactoring it would mean editing the existing
    premarket scanner, and spec section 1 forbids changing working
    behaviour for the convenience of new code. `tests/test_scanner_
    indicators.py` asserts the two agree, so the duplication cannot
    silently diverge.
    """
    period = int(period)
    if df is None or len(df) < period + 2:
        return pd.Series(dtype=float)
    high = high_series(df)
    low = low_series(df)
    close = close_series(df)
    if high.empty or low.empty or close.empty:
        return pd.Series(dtype=float)

    plus_dm = (high.diff()).where((high.diff() > -low.diff()) & (high.diff() > 0), 0.0)
    minus_dm = (-low.diff()).where((-low.diff() > high.diff()) & (-low.diff() > 0), 0.0)
    true_range = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = true_range.rolling(period).mean()
    plus_di = 100 * plus_dm.rolling(period).mean() / atr
    minus_di = 100 * minus_dm.rolling(period).mean() / atr
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, pd.NA)) * 100
    return dx.rolling(period).mean()


def session_vwap(df) -> pd.Series:
    """VWAP that restarts at each session boundary.

    `score_scanner.calculate_vwap` accumulates over whatever frame it is
    given, which is right when the frame is one session. The intraday
    frames the ORB and gap-pullback scanners work with span several
    days, and a cumulative sum carried across midnight produces a number
    that is neither yesterday's VWAP nor today's. Grouping by the bar's
    calendar date (in the index's own timezone -- yfinance returns
    US/Eastern-localised intraday bars) restarts it correctly.

    Falls back to the plain cumulative form for a frame with no
    datetime index, which is what the unit-test fixtures use.
    """
    if df is None or len(df) == 0:
        return pd.Series(dtype=float)
    index = df.index
    if not isinstance(index, pd.DatetimeIndex):
        return calculate_vwap(df)
    dates = pd.Series(index.date, index=index)
    parts = []
    for _, session in df.groupby(dates.values, sort=False):
        parts.append(calculate_vwap(session))
    if not parts:
        return pd.Series(dtype=float)
    return pd.concat(parts).reindex(index)


def rolling_high(df, window: int, *, exclude_current: bool = True) -> Optional[float]:
    """Highest high over the last `window` bars.

    `exclude_current=True` drops the newest bar, which is what "the
    high it has to break" means: a bar cannot break a level it is itself
    setting. Including it would make `distance_to_20d_high` collapse to
    0% for every symbol printing a new high today -- i.e. exactly the
    already-broken-out names S3 is built to exclude would score as the
    closest to breaking out.
    """
    highs = high_series(df).dropna()
    if exclude_current and len(highs) > 0:
        highs = highs.iloc[:-1]
    window = int(window)
    if len(highs) < window:
        return None
    return to_float(highs.tail(window).max())


def distance_pct(price, level) -> Optional[float]:
    """How far `price` sits BELOW `level`, in percent.

    Positive means below (still to go), negative means already above.
    Chosen this way round because S3's rule reads "within 5% of the 20d
    high", and a positive-is-below convention lets that be `0 <= d <= 5`
    with no sign juggling at the call site.
    """
    price_value = to_float(price)
    level_value = to_float(level)
    if price_value is None or level_value is None or level_value == 0:
        return None
    return to_float((level_value - price_value) / level_value * 100.0)


def extension_pct(price, reference) -> Optional[float]:
    """Spec section 8: `(price / reference - 1) * 100`.

    How stretched a name is above its own anchor. Recorded for every
    signal from every scanner and deliberately NOT filtered on in v1.0 --
    the whole point is to find out, from a month of data, what range of
    extension actually precedes good outcomes rather than guessing a cut
    now and baking the guess into the dataset.
    """
    price_value = to_float(price)
    reference_value = to_float(reference)
    if price_value is None or reference_value is None or reference_value == 0:
        return None
    return to_float((price_value / reference_value - 1.0) * 100.0)


def average_volume(df, window: int = 20, *, exclude_current: bool = True) -> Optional[float]:
    """Mean volume over `window` bars, excluding today by default.

    Today's volume is the numerator of `volume_multiple`; leaving it in
    the denominator's average damps exactly the spike the ratio is
    supposed to measure, and damps it more the shorter the window.
    """
    volumes = volume_series(df).dropna()
    if exclude_current and len(volumes) > 0:
        volumes = volumes.iloc[:-1]
    window = int(window)
    if len(volumes) < window:
        return None
    mean = to_float(volumes.tail(window).mean())
    if mean is None or mean <= 0:
        return None
    return mean


def price_change_pct(df) -> Optional[float]:
    """Latest close against the prior close, in percent."""
    closes = close_series(df).dropna()
    if len(closes) < 2:
        return None
    previous = to_float(closes.iloc[-2])
    current = to_float(closes.iloc[-1])
    if previous is None or current is None or previous == 0:
        return None
    return to_float((current / previous - 1.0) * 100.0)


def volume_price_efficiency(volume_multiple, price_change) -> Optional[float]:
    """S2's accumulation tell: volume expansion per unit of price move.

    A name trading 3x its normal volume while barely moving is being
    accumulated by someone who does not want to pay up; the same 3x on a
    +9% day is just a stampede. This ratio separates them.

    `price_change` is taken as an absolute value with a floor, because
    the interesting cases sit exactly where the denominator goes to zero
    -- a flat day on huge volume is the signal, not a division error.
    The floor (0.1%) caps the ratio instead of letting it run to
    infinity, so the value stays comparable across symbols. Spec section
    S2 marks this as an analysis field, not a filter, in v1.0.
    """
    multiple = to_float(volume_multiple)
    change = to_float(price_change)
    if multiple is None or change is None:
        return None
    magnitude = max(abs(change), 0.1)
    return to_float(multiple / magnitude)
