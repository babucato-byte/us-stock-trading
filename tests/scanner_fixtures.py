"""Synthetic bars for the scanner tests.

Every scanner in `scanners/` is a pure function of a `SymbolData`
bundle, which is what lets these fixtures drive real scanner code with
no network, no yfinance, and no clock dependency beyond the staleness
check. That matters for section 28's list -- NaN, zero volume, empty
response, stale data, missing bars -- because those cases have to be
tested against the actual code path an 800-name universe will hit, not
against a mock of it.

Two conventions worth knowing before reading a test:

Bars end TODAY. `SymbolFeatures` refuses daily bars older than
`max_daily_bar_age_days`, and rightly so -- a scan must not silently
judge a symbol from last week's prices. Fixtures therefore anchor their
index to the current date, so the tests exercise the same fresh-data
path production does rather than routing every case into the staleness
branch.

Trends are shaped, not straight. A perfectly linear ramp produces ADX
pinned at 100 and a slope that never changes, which quietly makes
"ADX rising" untestable and every scanner that requires it unreachable.
`accelerating_uptrend` deliberately spends most of its length choppy and
only resolves into a clean trend near the end, which is both what an
early-trend setup actually looks like and what makes the rising
conditions reachable.
"""

import math
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from market_hours import EASTERN
from scanners.base.market_data_provider import PremarketSnapshot, SymbolData

#: Comfortably above `minimum_daily_bars()` (HMA200 needs ~214 bars plus
#: the slope lookback), with room for a 252-bar 52-week high.
DEFAULT_DAILY_BARS = 320


def business_index(periods, end=None, tz=EASTERN):
    """A business-day index ending on `end` (default: today)."""
    end = end or date.today()
    return pd.date_range(end=pd.Timestamp(end), periods=periods, freq="B", tz=tz)


def daily_frame(closes, *, volumes=None, highs=None, lows=None, opens=None, end=None):
    """Build a daily OHLCV frame from a close series.

    Highs and lows default to a tight band around the close. Tight on
    purpose: several scanners key off the 20-day high, and a wide random
    band would let an unrelated bar's spike decide whether a test passes.
    """
    closes = np.asarray(closes, dtype=float)
    count = len(closes)
    if volumes is None:
        volumes = np.full(count, 1_000_000.0)
    volumes = np.asarray(volumes, dtype=float)
    highs = np.asarray(highs, dtype=float) if highs is not None else closes * 1.005
    lows = np.asarray(lows, dtype=float) if lows is not None else closes * 0.995
    opens = np.asarray(opens, dtype=float) if opens is not None else closes * 0.999
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=business_index(count, end=end),
    )


def accelerating_uptrend(count=DEFAULT_DAILY_BARS, *, start=20.0, resolve_after=0.92,
                         chop_amplitude=0.6, trend_step=0.28, dip_every=4, dip=0.35):
    """Choppy for most of its length, then a clean uptrend at the end.

    Two details make the S1 pass path reachable, and both were arrived
    at by measuring rather than guessing:

    `resolve_after` leaves only the last ~8% of bars trending. ADX over
    14 periods saturates at 100 after roughly 30 bars of uninterrupted
    direction, and a saturated ADX is FLAT -- so a longer trend leg
    would make "ADX rising" permanently false and leave S1 with no
    reachable pass case at all.

    `dip_every` puts a small down bar into the trend leg. Without one,
    DX is exactly 100 on every bar and ADX converges to a constant. The
    dips keep it climbing, which is also what a real early trend looks
    like.

    With the defaults this yields ADX ~42 and rising, HMA89 above a
    rising HMA200, and price comfortably above both.
    """
    pivot = int(count * resolve_after)
    closes = []
    for index in range(count):
        if index < pivot:
            # Drift up slowly with an oscillation, so HMA200 rises but
            # directional strength stays modest.
            closes.append(start + index * 0.02 + chop_amplitude * math.sin(index / 3.0))
        else:
            previous = closes[-1]
            closes.append(previous - dip if index % dip_every == 0
                          else previous + trend_step)
    return np.asarray(closes, dtype=float)


def uptrend_bundle(symbol="TEST", *, count=DEFAULT_DAILY_BARS, volumes=None,
                   intraday=None, premarket=None, end=None, **kwargs):
    """A bundle whose daily bars satisfy the trend conditions of S1/S2/S3."""
    closes = accelerating_uptrend(count, **kwargs)
    daily = daily_frame(closes, volumes=volumes, end=end)
    return SymbolData(symbol=symbol, daily=daily, intraday=intraday, premarket=premarket)


def volume_surge(count=DEFAULT_DAILY_BARS, *, base=1_000_000.0, multiple=2.4):
    """Flat volume with a single spike on the most recent bar.

    `average_volume` excludes the current bar, so the spike lands
    entirely in the numerator of `volume_multiple` and the resulting
    ratio is exactly `multiple` -- which lets a test assert the value,
    not just that it is "high".
    """
    volumes = np.full(count, base, dtype=float)
    volumes[-1] = base * multiple
    return volumes


def coiled_under_high(count=DEFAULT_DAILY_BARS, *, gap_pct=2.0):
    """An uptrend that sets a high a few bars back and eases just under it.

    The 20-day high is computed with today's bar excluded, so the peak
    has to be in the recent history rather than on the final bar for
    `distance_20d_high` to be a small positive number -- which is the
    Breakout-Ready shape.
    """
    closes = accelerating_uptrend(count)
    peak = closes[-1] * (1.0 + gap_pct / 100.0)
    closes = closes.copy()
    closes[-4] = peak
    closes[-3] = peak * 0.995
    closes[-2] = peak * 0.99
    closes[-1] = peak / (1.0 + gap_pct / 100.0)
    return closes


def session_index(bars, *, day=None, start_hour=9, start_minute=30, tz=EASTERN):
    day = day or date.today()
    first = pd.Timestamp(datetime.combine(day, datetime.min.time()), tz=tz).replace(
        hour=start_hour, minute=start_minute)
    return pd.date_range(start=first, periods=bars, freq="1min")


def intraday_frame(closes, *, volumes=None, day=None, highs=None, lows=None, opens=None,
                   spread=0.02):
    closes = np.asarray(closes, dtype=float)
    count = len(closes)
    if volumes is None:
        volumes = np.full(count, 10_000.0)
    volumes = np.asarray(volumes, dtype=float)
    highs = np.asarray(highs, dtype=float) if highs is not None else closes + spread
    lows = np.asarray(lows, dtype=float) if lows is not None else closes - spread
    opens = np.asarray(opens, dtype=float) if opens is not None else np.concatenate(
        [[closes[0]], closes[:-1]])
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=session_index(count, day=day),
    )


def gap_pullback_session(previous_close, *, gap_pct=4.0, run_pct=2.0, pullback_pct=1.5,
                         impulse_volume=40_000.0, pullback_volume=12_000.0,
                         impulse_bars=25, pullback_bars=25, day=None):
    """A gap up, an impulse leg, then a pullback on lighter volume.

    The volume asymmetry is the substance of S5: the pullback leg trades
    at a fraction of the impulse leg's per-bar volume, which is what the
    scanner's `pullback_volume_ratio` check is measuring.

    `pullback_pct` defaults to 1.5 rather than something deeper because
    VWAP over this shape sits near +1% of the session open, and a 2.5%
    retreat from the peak drops price ~1.5% BELOW it -- outside the
    scanner's VWAP tolerance, so the fixture would exercise the reject
    path instead of the pass path. A shallow retreat that holds VWAP is
    also what "a good pullback" means, so the pass fixture and the real
    setup coincide.
    """
    open_price = previous_close * (1.0 + gap_pct / 100.0)
    peak = open_price * (1.0 + run_pct / 100.0)
    trough = peak * (1.0 - pullback_pct / 100.0)
    impulse = np.linspace(open_price, peak, impulse_bars)
    pullback = np.linspace(peak, trough, pullback_bars + 1)[1:]
    closes = np.concatenate([impulse, pullback])
    volumes = np.concatenate([
        np.full(impulse_bars, impulse_volume),
        np.full(pullback_bars, pullback_volume),
    ])
    opens = np.concatenate([[open_price], closes[:-1]])
    return intraday_frame(closes, volumes=volumes, opens=opens, day=day, spread=0.01)


def orb_session(*, base=100.0, range_pct=0.8, breakout_pct=1.2, range_bars=15,
                post_bars=30, range_volume=8_000.0, post_volume=24_000.0,
                confirm_close=True, retest=False, day=None):
    """An opening range, then a breakout above it on expanding volume.

    `confirm_close=False` produces the wick-only case: the range high is
    exceeded intrabar but no bar closes above it, which is the
    distinction section S6 singles out and which the scanner reports as
    `breakout_touched` without `breakout_confirmed`.
    """
    high = base * (1.0 + range_pct / 100.0)
    low = base * (1.0 - range_pct / 100.0)
    range_closes = np.linspace(low * 1.001, high * 0.999, range_bars)
    range_highs = np.full(range_bars, high)
    range_lows = np.full(range_bars, low)

    target = high * (1.0 + breakout_pct / 100.0)
    if confirm_close:
        post_closes = np.linspace(high * 1.001, target, post_bars)
        post_highs = post_closes + 0.02
    else:
        # Highs poke above the range, closes never do. The closes still
        # DRIFT UP toward the range high rather than sitting flat: the
        # scanner's relaxed branch is only reachable if the other
        # conditions (price above VWAP, EMA9 over EMA21) are also
        # satisfiable, and a flat close path leaves price sitting on top
        # of its own VWAP, which rejects for an unrelated reason and
        # makes the wick-only branch untestable.
        post_closes = np.linspace(high * 0.990, high * 0.9995, post_bars)
        post_highs = np.full(post_bars, high * 1.004)
    post_lows = post_closes - 0.05

    if retest and confirm_close:
        # Break, dip back to touch the range high, then push above again.
        midpoint = post_bars // 2
        post_lows = post_lows.copy()
        post_lows[midpoint] = high * 0.999
        post_closes = post_closes.copy()
        post_closes[midpoint] = high * 1.0005

    closes = np.concatenate([range_closes, post_closes])
    highs = np.concatenate([range_highs, post_highs])
    lows = np.concatenate([range_lows, post_lows])
    volumes = np.concatenate([
        np.full(range_bars, range_volume),
        np.full(post_bars, post_volume),
    ])
    opens = np.concatenate([[range_closes[0]], closes[:-1]])
    return intraday_frame(closes, volumes=volumes, highs=highs, lows=lows, opens=opens,
                          day=day)


def premarket_momentum_session(previous_close, *, gain_pct=9.0, bars=40,
                               volume_multiple=4.0, base_volume=10_000.0, day=None):
    """A session the EXISTING score scanner accepts.

    Its checks are price > VWAP, EMA9 > EMA21, volume multiple over the
    configured floor, and a premarket gain over the configured floor --
    so the frame ramps steadily upward (putting price above VWAP and the
    fast EMA above the slow one) and ends on a volume spike. Its rolling
    20-bar average volume needs at least 20 bars before it produces a
    value at all, hence the default length.
    """
    target = previous_close * (1.0 + gain_pct / 100.0)
    closes = np.linspace(previous_close * 1.001, target, bars)
    volumes = np.full(bars, base_volume, dtype=float)
    volumes[-1] = base_volume * volume_multiple
    return intraday_frame(closes, volumes=volumes, day=day, spread=0.01)


def premarket(symbol="TEST", *, previous_close=100.0, price=109.0, volume=500_000.0):
    gain = (price / previous_close - 1.0) * 100.0 if previous_close else None
    return PremarketSnapshot(
        symbol=symbol,
        previous_close=previous_close,
        premarket_price=price,
        premarket_volume=volume,
        premarket_gain_pct=gain,
        as_of=datetime.now(),
    )


def newest_iso(frame):
    """The frame's newest bar timestamp, the way a signal records it.

    Deliberately calls the production helper rather than re-deriving the
    string: a test that formatted the timestamp its own way would pass
    while the stored value was formatted differently, which is precisely
    the mismatch `data_timestamp` exists to make impossible.
    """
    from scanners.base.features import newest_bar_timestamp

    return newest_bar_timestamp(frame)


def last_session_date(daily):
    """The calendar date of the daily frame's newest bar.

    Every intraday bundle below anchors its session to this rather than
    to `date.today()`. The two differ whenever the suite runs on a
    weekend or a holiday -- a business-day index ending "today" actually
    ends on the previous Friday -- and a session dated a day after the
    newest daily bar would make `previous_daily_close` return that
    newest bar instead of the one before it. The gap would then be
    measured against the wrong close and the fixture would pass or fail
    according to which day of the week the tests were run.
    """
    return daily.index[-1].date()


def gap_pullback_bundle(symbol="TEST", *, count=DEFAULT_DAILY_BARS, **kwargs):
    """Daily uptrend plus a gap-and-pullback session, dates consistent."""
    daily = daily_frame(accelerating_uptrend(count))
    day = last_session_date(daily)
    previous_close = float(daily["Close"].iloc[-2])
    intraday = gap_pullback_session(previous_close, day=day, **kwargs)
    return SymbolData(symbol=symbol, daily=daily, intraday=intraday)


def orb_bundle(symbol="TEST", *, count=DEFAULT_DAILY_BARS, **kwargs):
    """Daily uptrend plus an opening-range-breakout session."""
    daily = daily_frame(accelerating_uptrend(count))
    day = last_session_date(daily)
    kwargs.setdefault("base", float(daily["Close"].iloc[-1]))
    intraday = orb_session(day=day, **kwargs)
    return SymbolData(symbol=symbol, daily=daily, intraday=intraday)


def premarket_momentum_bundle(symbol="TEST", *, count=DEFAULT_DAILY_BARS, **kwargs):
    """Daily uptrend plus a session the existing score scanner accepts."""
    daily = daily_frame(accelerating_uptrend(count))
    day = last_session_date(daily)
    previous_close = float(daily["Close"].iloc[-2])
    intraday = premarket_momentum_session(previous_close, day=day, **kwargs)
    return SymbolData(symbol=symbol, daily=daily, intraday=intraday,
                      premarket=premarket(symbol, previous_close=previous_close,
                                          price=float(intraday["Close"].iloc[-1])))


def forward_daily(signal_day, *, sessions, start_price, highs, lows, closes):
    """Daily bars for the sessions AFTER a signal, for the tracker tests.

    Index starts the day after `signal_day` so `_sessions_after` sees
    exactly `sessions` forward bars -- the tracker deliberately excludes
    the signal day's own bar, because its high includes the part of the
    session that happened before the scanner spoke.
    """
    start = pd.Timestamp(signal_day, tz=EASTERN) + pd.Timedelta(days=1)
    index = pd.date_range(start=start, periods=sessions, freq="B", tz=EASTERN)
    return pd.DataFrame(
        {
            "Open": [start_price] * sessions,
            "High": list(highs),
            "Low": list(lows),
            "Close": list(closes),
            "Volume": [1_000_000.0] * sessions,
        },
        index=index,
    )
