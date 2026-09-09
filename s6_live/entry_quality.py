"""Is this ORB momentum still fresh, or is the entry a late chase?

The question S6 was not asking
------------------------------
The precision watch re-asks every strategy condition against the market
as it is now -- price above VWAP, EMA structure, breakout holds, volume
expansion over the whole post-range, extension. Every one of those can
still be true forty minutes after the first spike, with the last fifteen
minutes of volume dead and price drifting under the session high. PLUG on
2026-09-08: full post-range expansion 1.36x, the most recent fifteen
minutes 0.94x, subsequent loss. Recent fills entered 24, 27, 44, 65 and
113 minutes after the breakout. Nothing in the READY state distinguished
those from a breakout that happened three minutes ago.

This module computes, at the exact decision instant and from the bars
the decision is made from, the measurements that DO distinguish them:

    breakout freshness      first_breakout_at, breakout_age_minutes/bars
    recent-high freshness   post_range_high, last_session_high_at,
                            minutes_since_session_high
    recent-volume persistence  recent_volume_5m/10m/15m/30m, rvol_* (vs the
                            opening range's per-minute volume), the
                            5m/15m and 5m/30m ratios, the volume slope,
                            and a time-of-session RVOL against the same
                            premarket bucket on prior sessions
    price momentum          return_5m/10m/15m
    location                extension_pct, price / vwap / ema9 / ema21
    liquidity               dollar_volume_5m, spread_bps (when supplied)

and `assess()` turns the configured subset of them into ONE verdict with
ONE reason code. Every threshold is null (off) until the historical
review selects it; a configured threshold whose input cannot be
computed refuses (UNAVAILABLE), it never passes by default.

Provenance travels with every value: which provider produced the bars,
the timestamp of the newest bar, how old it was at the decision, and the
bar interval -- a 5-minute-bar window and a 1-minute-bar window are not
the same measurement and are never mixed inside one snapshot. A value
that cannot be computed is None with the reason in `unavailable`, never
a fabricated number.

Nothing here reads the order path or writes anything.
"""

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# -- reason codes (stable, English, internal) ---------------------------------
S6_BREAKOUT_STALE = "S6_BREAKOUT_STALE"
S6_RECENT_VOLUME_WEAK = "S6_RECENT_VOLUME_WEAK"
S6_VOLUME_DECAY = "S6_VOLUME_DECAY"
S6_SESSION_HIGH_STALE = "S6_SESSION_HIGH_STALE"
S6_MOMENTUM_WEAKENING = "S6_MOMENTUM_WEAKENING"
S6_PREMARKET_LIQUIDITY_WEAK = "S6_PREMARKET_LIQUIDITY_WEAK"
S6_QUALITY_UNAVAILABLE = "S6_QUALITY_UNAVAILABLE"

REASON_CODES = (S6_BREAKOUT_STALE, S6_RECENT_VOLUME_WEAK, S6_VOLUME_DECAY,
                S6_SESSION_HIGH_STALE, S6_MOMENTUM_WEAKENING,
                S6_PREMARKET_LIQUIDITY_WEAK, S6_QUALITY_UNAVAILABLE)

#: A stale breakout only gets older: the candidate is invalidated rather
#: than watched. Every other code can recover on a later minute.
TERMINAL_CODES = frozenset({S6_BREAKOUT_STALE})

#: Threshold keys `assess()` understands, in the order they are judged.
#: The first failing one names the reason.
THRESHOLD_KEYS = (
    ("max_breakout_age_minutes", S6_BREAKOUT_STALE, "breakout_age_minutes", "le"),
    ("max_minutes_since_session_high", S6_SESSION_HIGH_STALE, "minutes_since_session_high", "le"),
    ("min_rvol_5m", S6_RECENT_VOLUME_WEAK, "rvol_5m", "ge"),
    ("min_rvol_tb_5m", S6_RECENT_VOLUME_WEAK, "rvol_tb_5m", "ge"),
    ("min_volume_ratio_5m_15m", S6_VOLUME_DECAY, "volume_ratio_5m_15m", "ge"),
    ("min_volume_ratio_5m_30m", S6_VOLUME_DECAY, "volume_ratio_5m_30m", "ge"),
    ("min_return_5m_pct", S6_MOMENTUM_WEAKENING, "return_5m", "ge"),
    ("min_return_10m_pct", S6_MOMENTUM_WEAKENING, "return_10m", "ge"),
    ("min_dollar_volume_5m", S6_PREMARKET_LIQUIDITY_WEAK, "dollar_volume_5m", "ge"),
    ("max_spread_bps", S6_PREMARKET_LIQUIDITY_WEAK, "spread_bps", "le"),
)

PASS = "PASS"
FAIL = "FAIL"
UNAVAILABLE = "UNAVAILABLE"

RECENT_WINDOWS = (5, 10, 15, 30)
MOMENTUM_WINDOWS = (5, 10, 15)
SLOPE_WINDOW_MINUTES = 15

#: Time-bucket RVOL: prior sessions to look back, and how many of them
#: must actually hold the symbol before a baseline is claimed.
TB_LOOKBACK_DAYS = 5
TB_MIN_DAYS = 3


@dataclass(frozen=True)
class SimpleBar:
    """One bar, provider-agnostic. `minute` is the bar OPEN in UTC."""

    minute: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class EntryQuality:
    symbol: str
    session: Optional[str]
    scanner_variant: Optional[str]
    orb_minutes: Optional[int]
    evaluated_at: Optional[datetime] = None
    # provenance
    provider: Optional[str] = None
    source_timestamp: Optional[datetime] = None
    data_age_seconds: Optional[float] = None
    bar_interval_minutes: Optional[float] = None
    bar_count: int = 0
    # ORB
    or_high: Optional[float] = None
    or_low: Optional[float] = None
    range_origin_timestamp: Optional[datetime] = None
    origin_covered: Optional[bool] = None
    closed_bar_only: bool = False
    range_end: Optional[datetime] = None
    first_breakout_at: Optional[datetime] = None
    breakout_age_minutes: Optional[float] = None
    breakout_age_bars: Optional[int] = None
    # recent high
    post_range_high: Optional[float] = None
    last_session_high_at: Optional[datetime] = None
    minutes_since_session_high: Optional[float] = None
    # recent volume
    recent_volume_5m: Optional[float] = None
    recent_volume_10m: Optional[float] = None
    recent_volume_15m: Optional[float] = None
    recent_volume_30m: Optional[float] = None
    rvol_5m: Optional[float] = None
    rvol_10m: Optional[float] = None
    rvol_15m: Optional[float] = None
    rvol_30m: Optional[float] = None
    # Explicit names. The legacy rvol_* fields above remain for stored
    # record compatibility; they compare recent pace with THIS session's
    # opening range, not with historical clock-time volume.
    recent_vs_opening_pace_5m: Optional[float] = None
    recent_vs_opening_pace_10m: Optional[float] = None
    recent_vs_opening_pace_15m: Optional[float] = None
    recent_vs_opening_pace_30m: Optional[float] = None
    rvol_tb_5m: Optional[float] = None
    rvol_tb_15m: Optional[float] = None
    rvol_tb_baseline_days: int = 0
    rvol_tb_status: Optional[str] = None
    volume_ratio_5m_15m: Optional[float] = None
    volume_ratio_5m_30m: Optional[float] = None
    volume_slope: Optional[float] = None
    volume_decay: Optional[bool] = None
    # momentum
    return_5m: Optional[float] = None
    return_10m: Optional[float] = None
    return_15m: Optional[float] = None
    # location
    current_price: Optional[float] = None
    extension_pct: Optional[float] = None
    extension_atr: Optional[float] = None
    vwap: Optional[float] = None
    ema9: Optional[float] = None
    ema21: Optional[float] = None
    # liquidity
    dollar_volume_5m: Optional[float] = None
    spread_bps: Optional[float] = None
    unavailable: Dict[str, str] = field(default_factory=dict)

    def as_record(self) -> Dict[str, Any]:
        record = asdict(self)
        for key, value in list(record.items()):
            if isinstance(value, datetime):
                record[key] = value.isoformat()
        return record

    def compact(self) -> Dict[str, Any]:
        """The handful of numbers a Slack line can carry."""
        return {
            "orb_minutes": self.orb_minutes,
            "breakout_age_minutes": self.breakout_age_minutes,
            "minutes_since_session_high": self.minutes_since_session_high,
            "rvol_5m": self.rvol_5m,
            "rvol_15m": self.rvol_15m,
            "rvol_tb_5m": self.rvol_tb_5m,
            "return_5m": self.return_5m,
            "extension_pct": self.extension_pct,
        }


# -- bar adapters ----------------------------------------------------------

def _utc(moment) -> Optional[datetime]:
    if moment is None:
        return None
    if hasattr(moment, "to_pydatetime"):
        moment = moment.to_pydatetime()
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _num(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def bars_from_store(bars) -> List[SimpleBar]:
    """`market_data.realtime_bars.Bar` objects, oldest first."""
    out = []
    for bar in bars or ():
        minute = _utc(getattr(bar, "minute", None))
        if minute is None:
            continue
        out.append(SimpleBar(minute=minute, open=_num(bar.open) or 0.0,
                             high=_num(bar.high) or 0.0, low=_num(bar.low) or 0.0,
                             close=_num(bar.close) or 0.0,
                             volume=_num(bar.volume) or 0.0))
    return sorted(out, key=lambda b: b.minute)


def bars_from_frame(frame) -> List[SimpleBar]:
    """A provider DataFrame (Open/High/Low/Close/Volume, datetime index)."""
    out = []
    if frame is None or len(frame) == 0:
        return out
    cols = {str(c).lower(): c for c in frame.columns}
    try:
        for stamp, row in frame.iterrows():
            minute = _utc(stamp)
            if minute is None:
                continue
            close = _num(row.get(cols.get("close", "Close")))
            if close is None:
                continue
            out.append(SimpleBar(
                minute=minute,
                open=_num(row.get(cols.get("open", "Open"))) or close,
                high=_num(row.get(cols.get("high", "High"))) or close,
                low=_num(row.get(cols.get("low", "Low"))) or close,
                close=close,
                volume=_num(row.get(cols.get("volume", "Volume"))) or 0.0))
    except Exception:  # noqa: BLE001 - a frame this cannot read yields nothing
        logger.debug("entry_quality: frame not readable", exc_info=True)
        return []
    return sorted(out, key=lambda b: b.minute)


# -- helpers ---------------------------------------------------------------

def _interval_minutes(bars: Sequence[SimpleBar]) -> Optional[float]:
    if len(bars) < 2:
        return None
    deltas = sorted((b.minute - a.minute).total_seconds() / 60.0
                    for a, b in zip(bars, bars[1:]) if b.minute > a.minute)
    return deltas[len(deltas) // 2] if deltas else None


def _window(bars: Sequence[SimpleBar], minutes: int) -> List[SimpleBar]:
    """Bars whose OPEN lies within the last `minutes` before the newest
    bar's close (open + interval). Exclusive of the far end."""
    if not bars:
        return []
    last = bars[-1].minute
    cutoff = last - timedelta(minutes=minutes)
    return [b for b in bars if b.minute > cutoff]


def _ratio(numerator, denominator) -> Optional[float]:
    if numerator is None or denominator in (None, 0):
        return None
    try:
        return float(numerator) / float(denominator)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _slope(values: Sequence[float]) -> Optional[float]:
    """Least-squares slope per bar, normalised by the mean, so +0.05
    reads as "volume rising 5% of its average per minute"."""
    n = len(values)
    if n < 3:
        return None
    mean = sum(values) / n
    if mean <= 0:
        return None
    xs = list(range(n))
    x_mean = (n - 1) / 2.0
    cov = sum((x - x_mean) * (v - mean) for x, v in zip(xs, values))
    var = sum((x - x_mean) ** 2 for x in xs)
    if var == 0:
        return None
    return (cov / var) / mean


def _close_minutes_ago(bars: Sequence[SimpleBar], minutes: int) -> Optional[float]:
    """The close of the newest bar that opened at or before `minutes`
    before the newest bar."""
    if len(bars) < 2:
        return None
    target = bars[-1].minute - timedelta(minutes=minutes)
    earlier = [b for b in bars if b.minute <= target]
    if not earlier:
        return None
    return earlier[-1].close


# -- time-bucket RVOL baseline --------------------------------------------------

def time_bucket_baseline(symbol, session, *, window_end: datetime, window_minutes: int,
                         lookback_days: int = TB_LOOKBACK_DAYS,
                         min_days: int = TB_MIN_DAYS, env=None,
                         store_loader=None) -> Tuple[Optional[float], int, str]:
    """Mean volume of the same clock window on prior sessions.

    Returns (baseline_volume, days_used, status). The window is the ET
    clock interval [window_end - W, window_end) applied to each prior
    trading day's stored bars for this symbol and session. A day counts
    only if the store held the symbol at all; a day that held it with no
    prints in the window is a real zero. Fewer than `min_days` usable
    days -> (None, days, "INSUFFICIENT_HISTORY"): recorded, never faked.
    """
    from zoneinfo import ZoneInfo

    from scanners.base.trading_calendar import previous_trading_day, us_trading_day

    loader = store_loader
    if loader is None:
        from s6_live import kis_bar_features

        loader = lambda sess, day: kis_bar_features.load_store(sess, day, env=env)  # noqa: E731
    eastern = ZoneInfo("America/New_York")
    end_et = window_end.astimezone(eastern)
    start_clock = (end_et - timedelta(minutes=window_minutes)).time()
    end_clock = end_et.time()
    day = us_trading_day(window_end)
    sums: List[float] = []
    scanned = 0
    while scanned < lookback_days:
        try:
            day = previous_trading_day(day)
        except Exception:  # noqa: BLE001
            break
        scanned += 1
        try:
            store = loader(session, day)
        except Exception:  # noqa: BLE001
            store = None
        if store is None:
            continue
        try:
            bars = store.bars(symbol, session)
        except Exception:  # noqa: BLE001
            bars = None
        if not bars:
            continue
        total = 0.0
        for bar in bars:
            minute_et = _utc(bar.minute).astimezone(eastern).time()
            if start_clock <= minute_et < end_clock:
                total += _num(bar.volume) or 0.0
        sums.append(total)
    if len(sums) < min_days:
        return None, len(sums), "INSUFFICIENT_HISTORY"
    return sum(sums) / len(sums), len(sums), "OK"


# -- the computation -----------------------------------------------------------

def compute(bars: Sequence[SimpleBar], *, symbol, session, orb_minutes, now,
            provider=None, scanner_variant=None, vwap=None, ema9=None, ema21=None,
            spread_bps=None, atr=None, require_close_breakout=True,
            range_origin_timestamp=None, require_official_origin=False,
            origin_covered=None,
            closed_bar_only=False,
            baseline: Optional[Callable[..., Tuple[Optional[float], int, str]]] = None,
            ) -> EntryQuality:
    """Every measurement, from the bars as they stood at `now`.

    `bars` must already be this session's bars, oldest first, and must
    not include anything after `now` (the caller owns that; a replay that
    passes future bars would be measuring hindsight).
    """
    moment = _utc(now) or datetime.now(timezone.utc)
    origin = _utc(range_origin_timestamp)
    current_minute = moment.replace(second=0, microsecond=0)
    bars = [b for b in (bars or [])
            if b.minute < current_minute] if closed_bar_only else [
                b for b in (bars or []) if b.minute <= moment]
    missing: Dict[str, str] = {}
    base: Dict[str, Any] = {
        "symbol": str(symbol or "").upper(), "session": session,
        "scanner_variant": scanner_variant, "orb_minutes": orb_minutes,
        "evaluated_at": moment, "provider": provider, "vwap": _num(vwap),
        "ema9": _num(ema9), "ema21": _num(ema21), "spread_bps": _num(spread_bps),
        "bar_count": len(bars), "range_origin_timestamp": origin,
        "closed_bar_only": bool(closed_bar_only),
    }
    if not bars:
        base["unavailable"] = {"bars": "no session bars"}
        return EntryQuality(**base)
    interval = _interval_minutes(bars)
    base["bar_interval_minutes"] = interval
    last = bars[-1]
    base["source_timestamp"] = last.minute
    base["data_age_seconds"] = (moment - last.minute).total_seconds()
    base["current_price"] = last.close

    # -- opening range: the first `orb_minutes` of clock time from the
    # first bar, the same anchor the scanner and the watch use.
    try:
        span = int(orb_minutes)
    except (TypeError, ValueError):
        span = None
    if not span or span <= 0:
        missing["or_high"] = "orb_minutes not set"
        base["unavailable"] = missing
        return EntryQuality(**base)
    covered = (bool(origin_covered) if origin_covered is not None else
               bool(origin is None or bars[0].minute <= origin))
    base["origin_covered"] = covered if origin is not None else None
    if require_official_origin and (origin is None or not covered):
        missing["or_high"] = "OFFICIAL_ORIGIN_NOT_COVERED"
        base["unavailable"] = missing
        return EntryQuality(**base)
    range_start = origin or bars[0].minute
    range_cutoff = range_start + timedelta(minutes=span)
    opening = [b for b in bars if range_start <= b.minute < range_cutoff]
    post = [b for b in bars if b.minute >= range_cutoff]
    if not opening:
        missing["or_high"] = "no opening-range bars"
    else:
        base["or_high"] = max(b.high for b in opening)
        base["or_low"] = min(b.low for b in opening)
        base["range_end"] = opening[-1].minute
    or_high = base.get("or_high")
    if or_high is not None and last.close and or_high > 0:
        base["extension_pct"] = (last.close / or_high - 1.0) * 100.0
        if atr:
            base["extension_atr"] = (last.close - or_high) / float(atr)
    range_per_minute = None
    if opening:
        range_volume = sum(b.volume for b in opening)
        range_span = max(1.0, len(opening) * (interval or 1.0))
        range_per_minute = range_volume / range_span if range_volume > 0 else None

    # -- breakout freshness
    if or_high is None:
        missing["first_breakout_at"] = "no opening range"
    elif not post:
        missing["first_breakout_at"] = "no post-range bars"
    else:
        first = None
        for index, bar in enumerate(post):
            level = bar.close if require_close_breakout else bar.high
            if level > or_high:
                first = (index, bar)
                break
        if first is None:
            missing["first_breakout_at"] = "no breakout yet"
        else:
            index, bar = first
            base["first_breakout_at"] = bar.minute
            base["breakout_age_minutes"] = (last.minute - bar.minute).total_seconds() / 60.0
            base["breakout_age_bars"] = len(post) - 1 - index

    # -- recent-high freshness
    if post:
        top = max(b.high for b in post)
        at = [b.minute for b in post if b.high == top][-1]
        base["post_range_high"] = top
        base["last_session_high_at"] = at
        base["minutes_since_session_high"] = (last.minute - at).total_seconds() / 60.0
    else:
        missing["post_range_high"] = "no post-range bars"

    # -- recent volume, absolute and relative to the opening range's pace
    recent: Dict[int, float] = {}
    for window in RECENT_WINDOWS:
        rows = _window(bars, window)
        total = sum(b.volume for b in rows) if rows else None
        recent[window] = total
        base[f"recent_volume_{window}m"] = total
        if total is None:
            missing[f"recent_volume_{window}m"] = "no bars in window"
            continue
        if range_per_minute is None:
            missing[f"rvol_{window}m"] = "opening range volume is zero or missing"
        else:
            pace_ratio = _ratio(total / float(window), range_per_minute)
            base[f"rvol_{window}m"] = pace_ratio
            base[f"recent_vs_opening_pace_{window}m"] = pace_ratio
    r5, r15, r30 = recent.get(5), recent.get(15), recent.get(30)
    if r5 is not None and r15:
        base["volume_ratio_5m_15m"] = _ratio(r5 / 5.0, r15 / 15.0)
    else:
        missing["volume_ratio_5m_15m"] = "needs 5m and 15m volume"
    if r5 is not None and r30:
        base["volume_ratio_5m_30m"] = _ratio(r5 / 5.0, r30 / 30.0)
    else:
        missing["volume_ratio_5m_30m"] = "needs 5m and 30m volume"
    slope_rows = _window(bars, SLOPE_WINDOW_MINUTES)
    slope = _slope([b.volume for b in slope_rows]) if len(slope_rows) >= 3 else None
    base["volume_slope"] = slope
    if slope is None:
        missing["volume_slope"] = "fewer than three bars in the slope window"
    ratio = base.get("volume_ratio_5m_15m")
    if ratio is not None:
        base["volume_decay"] = bool(ratio < 1.0 and (slope is None or slope < 0))
    else:
        missing["volume_decay"] = "no 5m/15m ratio"
    if r5 is not None:
        rows5 = _window(bars, 5)
        base["dollar_volume_5m"] = sum(b.close * b.volume for b in rows5)

    # -- time-of-session RVOL against prior sessions
    if baseline is not None and session:
        for window in (5, 15):
            today = recent.get(window)
            if today is None:
                continue
            try:
                value, days, status = baseline(
                    symbol=symbol, session=session,
                    window_end=last.minute + timedelta(minutes=interval or 1.0),
                    window_minutes=window)
            except Exception:  # noqa: BLE001
                logger.debug("time-bucket baseline failed", exc_info=True)
                value, days, status = None, 0, "BASELINE_ERROR"
            base[f"rvol_tb_{window}m"] = _ratio(today, value) if value else None
            if window == 5:
                base["rvol_tb_baseline_days"] = days
                base["rvol_tb_status"] = status
            if value in (None, 0):
                missing[f"rvol_tb_{window}m"] = status if value is None else "baseline is zero"
    else:
        base["rvol_tb_status"] = "NO_BASELINE_SOURCE"
        missing["rvol_tb_5m"] = "no baseline source"
        missing["rvol_tb_15m"] = "no baseline source"

    # -- price momentum
    for window in MOMENTUM_WINDOWS:
        earlier = _close_minutes_ago(bars, window)
        if earlier in (None, 0):
            missing[f"return_{window}m"] = "no bar that far back"
        else:
            base[f"return_{window}m"] = (last.close / earlier - 1.0) * 100.0

    if base.get("spread_bps") is None:
        missing["spread_bps"] = "no quote source"
    base["unavailable"] = missing
    return EntryQuality(**base)


# -- the verdict -------------------------------------------------------------

def thresholds_for(config, session) -> Dict[str, Any]:
    """The `entry_quality` block for one session, or {} (everything off).

    Shape in scanners/orb/config.json:
        "entry_quality": {"PREMARKET": {"max_breakout_age_minutes": 20, ...},
                          "default": {}}
    """
    try:
        block = config.get("entry_quality") if hasattr(config, "get") else None
    except Exception:  # noqa: BLE001
        block = None
    if not isinstance(block, dict):
        return {}
    key = str(session or "").strip().upper()
    chosen = block.get(key)
    if not isinstance(chosen, dict):
        chosen = block.get("default")
    return dict(chosen) if isinstance(chosen, dict) else {}


def assess(quality: Optional[EntryQuality], thresholds: Dict[str, Any]
           ) -> Tuple[str, Optional[str], Dict[str, Any]]:
    """(verdict, reason_code, detail). PASS when nothing configured fails.

    Judged in THRESHOLD_KEYS order so the first failing dimension names
    the reason. A configured threshold whose measurement is missing is
    UNAVAILABLE with S6_QUALITY_UNAVAILABLE -- refusing is the only safe
    reading of "we could not measure freshness".
    """
    active = {k: v for k, v in (thresholds or {}).items() if v is not None}
    detail: Dict[str, Any] = {"thresholds": active}
    if not active:
        return PASS, None, detail
    if quality is None:
        detail["missing"] = "entry quality not computed"
        return UNAVAILABLE, S6_QUALITY_UNAVAILABLE, detail
    for key, code, metric, mode in THRESHOLD_KEYS:
        if key not in active:
            continue
        limit = _num(active[key])
        value = getattr(quality, metric, None)
        if limit is None:
            continue
        if value is None:
            detail["missing"] = metric
            detail["missing_reason"] = quality.unavailable.get(metric, "not computed")
            return UNAVAILABLE, S6_QUALITY_UNAVAILABLE, detail
        ok = (value <= limit) if mode == "le" else (value >= limit)
        detail[metric] = value
        if not ok:
            detail["failed"] = key
            detail["limit"] = limit
            return FAIL, code, detail
    return PASS, None, detail
