"""Vectorised HMA for the scanners. Same numbers, ~100x less time.

Why this exists as a separate module
------------------------------------
`indicators.hma` is the HMA the LIVE technical filter uses --
`technical_entry_filter` gates real orders with it. It is not touched
here, and nothing in the order path imports this module.
`tests/test_scanner_trading_isolation.py` enforces that.

The reference implementation computes its weighted moving average as:

    series.rolling(length).apply(
        lambda v: (v * weights).sum() / weights.sum(), raw=True)

which invokes a Python lambda once per bar. Measured on the server, one
`hma(89)` plus one `hma(200)` over 502 daily bars costs 0.90 s, which is
49% of the entire per-symbol scan cost. Over 13,362 symbols that alone
is more than three hours of CPU.

A weighted moving average is a convolution, so the same numbers come out
of one `np.convolve` call with no Python-level loop at all.

Equivalence, and the honest caveat
----------------------------------
`fast_hma` is not bit-identical to `indicators.hma`, and cannot be:
convolution accumulates the products in a different order than
`(v * weights).sum()`, so the two differ in the last floating-point
bits. Measured agreement is ~1e-12 relative, and
`tests/test_scanner_fast_indicators.py` pins both the numeric tolerance
and -- more importantly -- that every scanner reaches the same
PASS/REJECT verdict, score and reason through either implementation.

That residual matters in exactly one situation: a comparison such as
`price > hma200` where the two sides are equal to within 1e-12. At that
point the "correct" answer is already arbitrary -- a different pandas
version, or one more bar of history, would flip it too. It is recorded
here rather than hidden because a month of data collected through this
path should carry its own known limitations.

NaN semantics are preserved exactly
-----------------------------------
`rolling(length)` yields NaN for the first `length - 1` positions and
for any window containing a NaN. Convolution propagates NaN across a
window the same way, and the leading positions are padded explicitly.
The tests compare NaN POSITIONS, not just values, because getting the
warm-up length wrong would shift every subsequent bar by one and still
look plausible.
"""

import math

import numpy as np
import pandas as pd


def fast_wma(series, length: int) -> pd.Series:
    """Linearly weighted moving average, most recent bar weighted highest.

    Matches `indicators.weighted_moving_average`: weights are 1..length
    over the window, normalised by their sum.
    """
    length = int(length)
    values = pd.to_numeric(series, errors="coerce")
    index = values.index

    if length <= 0:
        return pd.Series(np.full(len(values), np.nan), index=index, dtype=float)

    array = values.to_numpy(dtype=float, copy=False)
    if len(array) < length:
        # Reference behaviour: a frame shorter than the window is all NaN.
        return pd.Series(np.full(len(array), np.nan), index=index, dtype=float)

    weights = np.arange(1, length + 1, dtype=float)
    weights /= weights.sum()

    # `np.convolve(x, w[::-1], "valid")[m]` == sum_k x[m+k] * w[k], which
    # is the window dotted with ascending weights -- i.e. the most recent
    # bar carries the largest weight, as the reference does. The index
    # algebra is deliberately not trusted: the tests assert equality
    # against the reference rather than against this comment.
    convolved = np.convolve(array, weights[::-1], mode="valid")

    out = np.empty(len(array), dtype=float)
    out[: length - 1] = np.nan
    out[length - 1:] = convolved
    return pd.Series(out, index=index, dtype=float)


def fast_hma(series, length: int) -> pd.Series:
    """Hull moving average, matching `indicators.hma`.

    HMA = WMA( 2*WMA(x, n/2) - WMA(x, n), sqrt(n) )

    The half and sqrt lengths use the same `int()` truncation and the
    same `max(1, ...)` floor as the reference, because those choices
    decide the warm-up length and therefore which bars are NaN.
    """
    length = int(length)
    values = pd.to_numeric(series, errors="coerce")
    if length <= 1:
        return values.copy()

    half_length = max(1, int(length / 2))
    sqrt_length = max(1, int(math.sqrt(length)))

    raw = (2 * fast_wma(values, half_length)) - fast_wma(values, length)
    return fast_wma(raw, sqrt_length)


def min_bars_for_hma(length: int) -> int:
    """Bars needed before `fast_hma` produces its first value.

    Kept here as well as in `scanners.base.indicators` so a caller that
    only imports the fast path still gets the right answer; the two are
    asserted equal in the tests.
    """
    length = int(length)
    if length <= 1:
        return 1
    return length + max(1, int(math.sqrt(length))) - 1
