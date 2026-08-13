"""One indicator pass per symbol, shared by all six scanners.

Why the scanners do not compute their own indicators
----------------------------------------------------
Section 7 requires every scanner to record the same technical field set,
and section 17 wants to compare the symbols two scanners both flagged.
Neither survives six scanners each computing "HMA200" their own way: a
5-bar slope in one and a 10-bar slope in another are different columns
wearing the same name, and a month of data collected that way cannot be
compared at all -- which is the entire point of the exercise.

So the indicator pass happens exactly once, here, from parameters in
`scanners/base/config.json`, and the scanners consume the result. A
scanner decides which features matter and where the thresholds sit; it
does not decide what a feature means.

The second reason is cost. HMA200 over a year of bars for 800 symbols is
the expensive part of a scan. Computing it once per symbol instead of
once per symbol per scanner is a six-fold saving on the dominant cost.

Staleness is checked here, once
-------------------------------
A symbol whose newest daily bar is a week old is not a scanner-specific
problem, and six scanners each deciding independently whether the data
is fresh enough would eventually disagree. `SymbolFeatures` refuses to
build from stale bars, so all six reject such a symbol identically and
for the same recorded reason (spec section 28).
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pandas as pd

from market_hours import EASTERN, MARKET_REGULAR_START as REGULAR_SESSION_START
from scanners.base import indicators as ind
from scanners.base.config import ScannerConfig, load_config
from scanners.base.market_data_provider import SymbolData
from scanners.base.models import ScannerDataError

logger = logging.getLogger(__name__)

_COMMON_CONFIG: Optional[ScannerConfig] = None


def common_config(*, reload: bool = False) -> ScannerConfig:
    """The shared indicator parameters.

    Cached because it is read once per symbol per scan and the file does
    not change mid-run; `reload=True` exists for tests that point
    `SCANNER_CONFIG_DIR` somewhere else.
    """
    global _COMMON_CONFIG
    if _COMMON_CONFIG is None or reload:
        _COMMON_CONFIG = load_config("base", scanner_name="common_features")
    return _COMMON_CONFIG


def reset_common_config() -> None:
    global _COMMON_CONFIG
    _COMMON_CONFIG = None


@dataclass(frozen=True)
class SymbolFeatures:
    """Everything section 7 and section 8 ask for, measured once.

    Every field is Optional. A name with 300 days of history has no
    52-week high and a genuinely absent one; recording it as None rather
    than as the highest high available keeps the month-end dataset from
    containing a "52-week high" that covers eight months.
    """

    symbol: str
    price: Optional[float] = None
    previous_close: Optional[float] = None

    hma89: Optional[float] = None
    hma200: Optional[float] = None
    hma200_slope: Optional[float] = None
    hma89_slope: Optional[float] = None
    hma89_above_hma200: Optional[bool] = None
    hma89_cross_hma200_recent: Optional[bool] = None
    bars_since_hma_cross: Optional[int] = None

    adx: Optional[float] = None
    adx_previous: Optional[float] = None
    adx_rising: Optional[bool] = None

    ema9: Optional[float] = None
    ema21: Optional[float] = None
    vwap: Optional[float] = None

    volume: Optional[float] = None
    avg_volume: Optional[float] = None
    volume_multiple: Optional[float] = None
    price_change_pct: Optional[float] = None
    volume_price_efficiency: Optional[float] = None

    high_20d: Optional[float] = None
    high_50d: Optional[float] = None
    high_52w: Optional[float] = None
    swing_high: Optional[float] = None
    distance_20d_high: Optional[float] = None
    distance_50d_high: Optional[float] = None
    distance_52w_high: Optional[float] = None
    distance_swing_high: Optional[float] = None

    premarket_gain_pct: Optional[float] = None

    extension_hma89_pct: Optional[float] = None
    extension_hma200_pct: Optional[float] = None
    extension_vwap_pct: Optional[float] = None

    daily_bars: int = 0
    intraday_bars: int = 0

    # --- provenance (spec sections 4 and 6) ---
    #: Newest bar timestamp in each frame, ISO-8601 with an offset. Both
    #: are recorded because a scanner reading minute bars and one
    #: reading daily bars have different answers to "how fresh was
    #: this", and the signal picks the one matching its own timeframe.
    daily_data_timestamp: Optional[str] = None
    intraday_data_timestamp: Optional[str] = None
    #: When this feature pass finished, UTC. Section 6 defines it as the
    #: completion of feature computation, NOT the bar time -- the gap
    #: between the two is how a stale-data run becomes visible.
    feature_timestamp: Optional[str] = None
    #: Premarket bars present in the intraday frame (before 09:30 ET).
    #: Section 18: Yahoo Finance extended-hours coverage is not
    #: guaranteed, so whether it was actually there is recorded rather
    #: than assumed.
    premarket_bars: int = 0

    extras: Dict[str, Any] = field(default_factory=dict)

    def data_timestamp_for(self, timeframe: Optional[str]) -> Optional[str]:
        """The bar timestamp matching a scanner's own timeframe.

        A daily scanner that reported its newest MINUTE bar would claim
        a freshness it did not use, and an intraday scanner reporting
        its newest daily bar would claim staleness it did not suffer.
        """
        if timeframe and timeframe != "1d":
            return self.intraday_data_timestamp or self.daily_data_timestamp
        return self.daily_data_timestamp

    def schema_fields(self) -> Dict[str, Any]:
        """The subset that maps onto `ScannerSignal`'s common columns.

        Kept as one method so a scanner cannot accidentally populate
        half the shared schema; every scanner splats this into its
        signal and then adds only its own extras.
        """
        return {
            "hma89": self.hma89,
            "hma200": self.hma200,
            "hma200_slope": self.hma200_slope,
            "ema9": self.ema9,
            "ema21": self.ema21,
            "vwap": self.vwap,
            "adx": self.adx,
            "volume": self.volume,
            "avg_volume": self.avg_volume,
            "volume_multiple": self.volume_multiple,
            "price_change_pct": self.price_change_pct,
            "high_20d": self.high_20d,
            "high_50d": self.high_50d,
            "high_52w": self.high_52w,
            "distance_20d_high": self.distance_20d_high,
            "distance_50d_high": self.distance_50d_high,
            "distance_52w_high": self.distance_52w_high,
            "premarket_gain_pct": self.premarket_gain_pct,
            "extension_hma89_pct": self.extension_hma89_pct,
            "extension_hma200_pct": self.extension_hma200_pct,
            "extension_vwap_pct": self.extension_vwap_pct,
        }

    def shared_metrics(self) -> Dict[str, Any]:
        """Measured values with no column in the common schema.

        These go into every signal's `metrics`, because section 22's
        month-end questions ("did ADX actually contribute?", "is high
        extension bad?") need the supporting variables, not just the
        ones the schema happened to name.
        """
        return {
            "hma89_slope": self.hma89_slope,
            "hma89_above_hma200": self.hma89_above_hma200,
            "hma89_cross_hma200_recent": self.hma89_cross_hma200_recent,
            "bars_since_hma_cross": self.bars_since_hma_cross,
            "adx_previous": self.adx_previous,
            "adx_rising": self.adx_rising,
            "volume_price_efficiency": self.volume_price_efficiency,
            "swing_high": self.swing_high,
            "distance_swing_high": self.distance_swing_high,
            "previous_close": self.previous_close,
            "daily_bars": self.daily_bars,
            "intraday_bars": self.intraday_bars,
            "daily_data_timestamp": self.daily_data_timestamp,
            "intraday_data_timestamp": self.intraday_data_timestamp,
            "premarket_bars": self.premarket_bars,
        }


def minimum_daily_bars(config: Optional[ScannerConfig] = None) -> int:
    """Bars needed before the slow HMA and its slope both exist.

    `min_bars_for_hma(200)` is when HMA200 produces its first value; the
    slope needs `hma_slope_lookback` more on top. A scanner asked to
    judge "HMA200 rising" from a frame one bar short of that would get
    None and reject the symbol -- correct, but for a reason that reads
    like a market judgement instead of a data shortfall, which is what
    section 28 says to avoid.
    """
    config = config or common_config()
    slow = config.require_int("hma_slow_length")
    lookback = config.require_int("hma_slope_lookback")
    return ind.min_bars_for_hma(slow) + lookback


def newest_bar_timestamp(frame) -> Optional[str]:
    """The newest bar's timestamp, ISO-8601 with an explicit offset.

    A naive index is assumed to be US/Eastern -- the convention
    everywhere in this repository and what the provider returns -- and
    is localised rather than left naive. An offset-less timestamp in the
    month-1 dataset would be unreadable a month later: 09:45 is either
    inside the session or hours outside it depending on a zone nobody
    wrote down.

    Returns None for a frame with no datetime index (the unit-test
    fixtures), rather than fabricating one.
    """
    if frame is None or len(frame) == 0:
        return None
    stamp = frame.index[-1]
    if not isinstance(stamp, pd.Timestamp):
        if hasattr(stamp, "isoformat"):
            return str(stamp.isoformat())
        return None
    if stamp.tzinfo is None:
        try:
            stamp = stamp.tz_localize(EASTERN)
        except (TypeError, ValueError):
            return None
    return stamp.isoformat()


def _count_premarket_bars(intraday) -> int:
    """Bars in the intraday frame that fall before 09:30 ET.

    Section 18: the premarket profile runs at 09:20 ET, before the
    regular session, and Yahoo Finance's extended-hours coverage is not
    something to assume. Counting them means "the premarket scanner
    found nothing" and "there were no premarket bars to look at" are
    distinguishable in the stored data rather than only in a log line.
    """
    if intraday is None or len(intraday) == 0:
        return 0
    if not isinstance(intraday.index, pd.DatetimeIndex):
        return 0
    index = intraday.index
    if index.tz is None:
        try:
            index = index.tz_localize(EASTERN)
        except (TypeError, ValueError):
            return 0
    else:
        index = index.tz_convert(EASTERN)
    return int(sum(1 for stamp in index if stamp.time() < REGULAR_SESSION_START))


def _daily_bar_age_days(daily: pd.DataFrame) -> Optional[int]:
    if daily is None or len(daily) == 0:
        return None
    stamp = daily.index[-1]
    if hasattr(stamp, "to_pydatetime"):
        stamp = stamp.to_pydatetime()
    if not isinstance(stamp, datetime):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).days


def _hma_cross_state(fast: pd.Series, slow: pd.Series, window: int):
    """Whether the fast HMA crossed above the slow one recently, and when.

    Section S1 wants this recorded ("hma89_cross_hma200_recent"). It is
    a record, not a filter, in v1.0: a fresh cross and a cross four
    months ago are both "price above a rising HMA200", and which of them
    actually precedes better outcomes is a month-end question, not one
    to guess at now.

    Returns (crossed_recently, bars_since_cross). `bars_since_cross` is
    None when no cross is visible in the available history at all --
    distinct from a large number, which means the cross happened long
    ago but is still on the chart.
    """
    aligned = pd.concat([fast, slow], axis=1).dropna()
    if len(aligned) < 2:
        return None, None
    above = aligned.iloc[:, 0] > aligned.iloc[:, 1]
    if not bool(above.iloc[-1]):
        return False, None
    # Walk back to the last bar where fast was NOT above slow; the cross
    # is the bar after it.
    below_positions = [i for i, value in enumerate(above.tolist()) if not value]
    if not below_positions:
        return False, None
    bars_since = len(above) - 1 - below_positions[-1]
    return bool(bars_since <= int(window)), int(bars_since)


def _swing_high(daily: pd.DataFrame, window: int) -> Optional[float]:
    """The most recent local peak: highest high of the last `window`
    bars, excluding today.

    S3 lists "recent swing high" alongside the fixed 20/50/252-day
    highs. A short rolling maximum is the cheap, deterministic version
    of that, and being deterministic matters more here than being
    sophisticated -- a pivot detector with its own parameters would be
    one more thing that could quietly change mid-month.
    """
    return ind.rolling_high(daily, window, exclude_current=True)


#: pandas exceptions that mean "this frame's data is unusable", not
#: "this code is broken" (spec section 21).
#:
#: `DataError: No numeric types to aggregate` is raised by `.rolling()`
#: when a column that should hold prices holds none -- an all-null or
#: object-dtype OHLCV frame, which the provider does return for some
#: thinly-traded symbols. Three of 193 symbols hit it in the 200-symbol
#: benchmark.
#:
#: Left unclassified these surfaced as ERROR with a full traceback, which
#: is wrong twice over: it counts a data problem as a scanner fault in
#: the run summary, and at 13,362 symbols a 1.6% rate is ~200 tracebacks
#: per run burying anything real (section 22).
#:
#: Deliberately a NARROW list. A blanket `except Exception ->
#: ScannerDataError` would hide genuine bugs as data problems, which is
#: the failure this classification is meant to prevent, inverted.
_DATA_SHAPED_ERRORS = (pd.errors.DataError,)


def _classify_data_error(symbol: str, exc: Exception) -> Optional[ScannerDataError]:
    """Convert an expected data-shape failure, or return None.

    Returning None means "not recognised" and the caller re-raises, so
    an unexpected exception keeps its traceback and its ERROR status.
    """
    if isinstance(exc, _DATA_SHAPED_ERRORS):
        return ScannerDataError(
            f"{symbol}: OHLCV columns are not numeric "
            f"(reason=non_numeric_ohlcv): {exc}")
    return None


def build_features(
    data: SymbolData,
    *,
    config: Optional[ScannerConfig] = None,
    require_intraday: bool = False,
) -> SymbolFeatures:
    """Compute the shared feature set for one symbol.

    Raises `ScannerDataError` when the daily history is absent, too
    short, or stale. Intraday-derived fields are simply left None when
    minute bars are missing, unless `require_intraday` -- a daily-only
    scanner must not be stopped by an absent minute feed.
    """
    config = config or common_config()
    symbol = data.symbol
    try:
        return _build_features(data, config=config, require_intraday=require_intraday)
    except ScannerDataError:
        raise
    except Exception as exc:  # noqa: BLE001 - narrowed immediately below
        classified = _classify_data_error(symbol, exc)
        if classified is None:
            # Not a recognised data shape problem. Re-raise untouched so
            # it keeps its traceback and is counted as a scanner fault.
            raise
        raise classified from exc


def _build_features(
    data: SymbolData,
    *,
    config: ScannerConfig,
    require_intraday: bool,
) -> SymbolFeatures:
    symbol = data.symbol

    minimum = minimum_daily_bars(config)
    daily = data.require_daily(minimum_bars=minimum)

    max_age = config.require_int("max_daily_bar_age_days")
    age = _daily_bar_age_days(daily)
    if age is not None and age > max_age:
        raise ScannerDataError(f"{symbol}: newest daily bar is {age}d old, limit {max_age}d")

    closes = ind.close_series(daily).dropna()
    if closes.empty:
        raise ScannerDataError(f"{symbol}: daily bars contain no usable closes")

    price = ind.to_float(closes.iloc[-1])
    if price is None or price <= 0:
        raise ScannerDataError(f"{symbol}: latest close is not a usable price")
    previous_close = ind.to_float(closes.iloc[-2]) if len(closes) >= 2 else None

    fast_length = config.require_int("hma_fast_length")
    slow_length = config.require_int("hma_slow_length")
    slope_lookback = config.require_int("hma_slope_lookback")
    adx_period = config.require_int("adx_period")
    volume_window = config.require_int("volume_window")

    hma_fast = ind.hma_series(daily, fast_length)
    hma_slow = ind.hma_series(daily, slow_length)
    hma89 = ind.last_valid(hma_fast)
    hma200 = ind.last_valid(hma_slow)
    if hma200 is None:
        raise ScannerDataError(
            f"{symbol}: HMA{slow_length} not computable from {len(daily)} daily bars")

    hma200_slope = ind.hma_slope_pct(hma_slow, slope_lookback)
    hma89_slope = ind.hma_slope_pct(hma_fast, slope_lookback)
    cross_recent, bars_since_cross = _hma_cross_state(
        hma_fast, hma_slow, int(config.get("hma_cross_recent_bars", 20)))

    adx_values = ind.adx_series(daily, adx_period)
    adx = ind.last_valid(adx_values)
    adx_previous = ind.nth_last_valid(adx_values, 1)
    adx_rising = ind.series_rising(adx_values, 1)

    volumes = ind.volume_series(daily).dropna()
    volume = ind.to_float(volumes.iloc[-1]) if len(volumes) else None
    avg_volume = ind.average_volume(daily, volume_window, exclude_current=True)
    volume_multiple = ind.safe_ratio(volume, avg_volume)
    change_pct = ind.price_change_pct(daily)
    efficiency = ind.volume_price_efficiency(volume_multiple, change_pct)

    high_20d = ind.rolling_high(daily, config.require_int("high_20d_window"))
    high_50d = ind.rolling_high(daily, config.require_int("high_50d_window"))
    high_52w = ind.rolling_high(daily, config.require_int("high_52w_window"))
    swing = _swing_high(daily, int(config.get("swing_high_window", 10)))

    intraday = data.intraday
    if require_intraday and (intraday is None or len(intraday) == 0):
        raise ScannerDataError(f"{symbol}: intraday bars required but unavailable")

    ema9 = ema21 = vwap = None
    intraday_bars = 0
    premarket_bars = 0
    if intraday is not None and len(intraday) > 0:
        intraday_bars = len(intraday)
        premarket_bars = _count_premarket_bars(intraday)
        intraday_close = ind.close_series(intraday)
        if not intraday_close.dropna().empty:
            ema9 = ind.last_valid(ind.ema(intraday_close, config.require_int("ema_fast_span")))
            ema21 = ind.last_valid(ind.ema(intraday_close, config.require_int("ema_slow_span")))
        vwap = ind.last_valid(ind.session_vwap(intraday))

    premarket_gain = None
    if data.premarket is not None:
        premarket_gain = data.premarket.premarket_gain_pct

    return SymbolFeatures(
        symbol=symbol,
        price=price,
        previous_close=previous_close,
        hma89=hma89,
        hma200=hma200,
        hma200_slope=hma200_slope,
        hma89_slope=hma89_slope,
        hma89_above_hma200=(None if hma89 is None else bool(hma89 > hma200)),
        hma89_cross_hma200_recent=cross_recent,
        bars_since_hma_cross=bars_since_cross,
        adx=adx,
        adx_previous=adx_previous,
        adx_rising=adx_rising,
        ema9=ema9,
        ema21=ema21,
        vwap=vwap,
        volume=volume,
        avg_volume=avg_volume,
        volume_multiple=volume_multiple,
        price_change_pct=change_pct,
        volume_price_efficiency=efficiency,
        high_20d=high_20d,
        high_50d=high_50d,
        high_52w=high_52w,
        swing_high=swing,
        distance_20d_high=ind.distance_pct(price, high_20d),
        distance_50d_high=ind.distance_pct(price, high_50d),
        distance_52w_high=ind.distance_pct(price, high_52w),
        distance_swing_high=ind.distance_pct(price, swing),
        premarket_gain_pct=premarket_gain,
        extension_hma89_pct=ind.extension_pct(price, hma89),
        extension_hma200_pct=ind.extension_pct(price, hma200),
        extension_vwap_pct=ind.extension_pct(price, vwap),
        daily_bars=len(daily),
        intraday_bars=intraday_bars,
        daily_data_timestamp=newest_bar_timestamp(daily),
        intraday_data_timestamp=newest_bar_timestamp(intraday),
        # Stamped LAST, after every indicator above has been computed,
        # so it means what section 6 says it means: the moment feature
        # computation finished, not the moment it started.
        feature_timestamp=datetime.now(timezone.utc).isoformat(),
        premarket_bars=premarket_bars,
    )
