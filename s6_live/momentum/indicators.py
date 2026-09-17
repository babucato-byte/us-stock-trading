"""HMA20 and the HMA-MACD histogram. The only file that knows the maths.

Swapping the pair means rewriting this file and nothing else: `model.py`
carries generic directions, `evaluator.py` calls `measure()`, and the S6
callers see neither.

Both implementations are reused rather than restated. The MACD keeps the
existing production 12/26/9 HMA-MACD configuration but computes its two
lines with `hma_series`, the framework's convolution HMA:
`indicators.calculate_hma_macd` uses a per-bar rolling `apply` and
measured 51.9ms per symbol, which at ~40 watched symbols on a one-minute
tick is ~2s of arithmetic for something that only observes. Equivalence
to the reference is pinned by a test at 1e-9.
"""

import logging
import math
from typing import Any, Dict, Optional, Tuple

from s6_live.momentum.model import DOWN, FLAT, UNKNOWN, UP

logger = logging.getLogger(__name__)

HMA_LENGTH = 20
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9


def min_bars_for_hma(length=HMA_LENGTH) -> int:
    from scanners.base.indicators import min_bars_for_hma as _minimum

    return int(_minimum(int(length)))


def _finite(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def frame_from(bars):
    """The bars S6 already holds, as the frame both indicators expect.

    `Close` is the column both `indicators.get_close_series` and
    `scanners.base.indicators.close_series` look for first. Bars with no
    usable close or minute are SKIPPED, never interpolated -- §6: no
    synthetic bars, sparse stays sparse.
    """
    import pandas as pd

    rows, stamps = [], []
    for bar in bars or ():
        close = _finite(getattr(bar, "close", None))
        minute = getattr(bar, "minute", None)
        if close is None or minute is None:
            continue
        rows.append({"Close": close})
        stamps.append(minute)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows, index=pd.DatetimeIndex(stamps, name="Datetime"))


def hma_macd(frame):
    """The production HMA-MACD configuration, on the fast HMA."""
    import pandas as pd

    from scanners.base.indicators import hma_series

    line = hma_series(frame, MACD_FAST) - hma_series(frame, MACD_SLOW)
    signal = line.ewm(span=MACD_SIGNAL, adjust=False,
                      min_periods=MACD_SIGNAL).mean()
    return pd.DataFrame({"hma_macd_line": line, "hma_macd_signal": signal,
                         "hma_macd_histogram": line - signal})


def measure(frame) -> Tuple[Dict[str, Any], Optional[str], Dict[str, float]]:
    """`(fields, reason, timings_ms)` for one frame.

    `fields` uses the indicator-prefixed names in `MomentumResult` plus
    the generic `combined_direction`. A measurement that cannot be taken
    is a reason, never a direction.
    """
    import time

    empty = dict(_empty_hma(), **_empty_macd())
    empty["combined_direction"] = UNKNOWN
    if frame is None or len(frame) == 0:
        return empty, "NO_BARS", {"HMA_COMPUTE_MS": 0.0, "MACD_COMPUTE_MS": 0.0}

    started = time.perf_counter()
    hma_fields, hma_reason = _hma(frame)
    hma_ms = (time.perf_counter() - started) * 1000.0

    started = time.perf_counter()
    macd_fields, macd_reason = _macd(frame)
    macd_ms = (time.perf_counter() - started) * 1000.0

    fields = dict(hma_fields, **macd_fields)
    fields["combined_direction"] = _combined(
        fields["hma_direction"], fields["macd_direction"],
        fields["hma_available"], fields["macd_available"])
    return (fields, hma_reason or macd_reason,
            {"HMA_COMPUTE_MS": round(hma_ms, 3),
             "MACD_COMPUTE_MS": round(macd_ms, 3)})


def _combined(hma_direction, macd_direction, hma_ok, macd_ok) -> str:
    """UP or DOWN only when BOTH agree.

    One side alone is never a direction: that is the whole reason the
    pair is used rather than either indicator by itself.
    """
    if not (hma_ok and macd_ok):
        return UNKNOWN
    if hma_direction == UP and macd_direction == UP:
        return UP
    if hma_direction == DOWN and macd_direction == DOWN:
        return DOWN
    return FLAT


def _hma(frame):
    try:
        from scanners.base.indicators import hma_series

        values = [v for v in (_finite(x) for x in list(hma_series(frame, HMA_LENGTH)))
                  if v is not None]
    except Exception:  # noqa: BLE001 - an observation, never a failure
        logger.debug("HMA%d unavailable", HMA_LENGTH, exc_info=True)
        return _empty_hma(), "COMPUTE_FAILED"
    if len(values) < 2:
        return _empty_hma(), "INSUFFICIENT_HISTORY"
    current, previous = values[-1], values[-2]
    slope = current - previous
    direction = UP if slope > 0 else (DOWN if slope < 0 else FLAT)
    return ({"hma_available": True, "hma_value": current,
             "hma_previous": previous, "hma_slope": slope,
             "hma_direction": direction}, None)


def _empty_hma():
    return {"hma_available": False, "hma_value": None, "hma_previous": None,
            "hma_slope": None, "hma_direction": UNKNOWN}


def _macd(frame):
    try:
        table = hma_macd(frame)
        histogram = [v for v in (_finite(x) for x in
                                 list(table["hma_macd_histogram"]))
                     if v is not None]
    except Exception:  # noqa: BLE001
        logger.debug("HMA-MACD unavailable", exc_info=True)
        return _empty_macd(), "COMPUTE_FAILED"
    if len(histogram) < 2:
        return _empty_macd(), "INSUFFICIENT_HISTORY"
    current, previous = histogram[-1], histogram[-2]
    delta = current - previous
    direction = UP if delta > 0 else (DOWN if delta < 0 else FLAT)
    return ({"macd_available": True,
             "macd": _finite(list(table["hma_macd_line"])[-1]),
             "signal": _finite(list(table["hma_macd_signal"])[-1]),
             "histogram": current, "histogram_previous": previous,
             "histogram_delta": delta, "macd_direction": direction}, None)


def _empty_macd():
    return {"macd_available": False, "macd": None, "signal": None,
            "histogram": None, "histogram_previous": None,
            "histogram_delta": None, "macd_direction": UNKNOWN}
