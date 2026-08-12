"""Slicing an intraday frame into "today's regular session".

Both intraday scanners depend on getting this exactly right, and for the
same reason: an opening range and a gap-pullback are both statements
about ONE session, measured from ITS open.

What goes wrong without it
--------------------------
The provider returns several days of minute bars, with prepost included,
in US/Eastern. Handing that whole frame to an opening-range calculation
produces the high of the first fifteen minutes of the OLDEST day in the
frame. Handing it to a gap calculation produces a "session open" that is
a 04:00 premarket print, which is not the price the gap is measured to.
Both failures are silent -- they yield plausible numbers that are simply
about the wrong bars.

So sessions are cut here, once, with the boundaries `market_hours`
already defines for the rest of this system (09:30-16:00 Eastern), and
both scanners share the result.

Naive-index fallback
--------------------
A frame with no DatetimeIndex is treated as a single session. That is
what the unit-test fixtures are -- plain RangeIndex frames of synthetic
bars -- and it lets every branch of both scanners be tested without
constructing timezone-aware indices for each case.
"""

from datetime import date, time
from typing import Optional, Tuple

import pandas as pd

from market_hours import EASTERN, MARKET_REGULAR_END, MARKET_REGULAR_START


def has_datetime_index(df) -> bool:
    return df is not None and isinstance(getattr(df, "index", None), pd.DatetimeIndex)


def to_eastern(df) -> pd.DataFrame:
    """The frame with its index expressed in US/Eastern.

    yfinance already returns Eastern-localised intraday bars, but a
    provider swapped in later (section 3 anticipates Polygon/Databento)
    may well return UTC. Converting here means the session boundaries
    below are compared in the only timezone they are defined in, no
    matter what the provider handed over. A naive index is assumed to be
    Eastern already, which is the existing convention everywhere in this
    repository.
    """
    if not has_datetime_index(df):
        return df
    index = df.index
    if index.tz is None:
        return df.tz_localize(EASTERN)
    return df.tz_convert(EASTERN)


def session_dates(df) -> list:
    if not has_datetime_index(df):
        return []
    frame = to_eastern(df)
    return sorted({stamp.date() for stamp in frame.index})


def latest_session_date(df) -> Optional[date]:
    dates = session_dates(df)
    return dates[-1] if dates else None


def slice_session(
    df,
    *,
    session_date: Optional[date] = None,
    regular_only: bool = True,
    start: time = MARKET_REGULAR_START,
    end: time = MARKET_REGULAR_END,
) -> pd.DataFrame:
    """One session's bars, oldest first.

    `regular_only` drops premarket and after-hours bars. An opening
    range built from prepost bars is not an opening range, and a
    gap-pullback measured against a 04:15 premarket high is measuring
    something else entirely -- so both callers ask for regular only.

    Returns an EMPTY frame, not None, when the session has no bars.
    Callers turn that into an explicit `ScannerDataError` naming the
    session, which is section 28's "missing bar" case.
    """
    if df is None or len(df) == 0:
        return pd.DataFrame()
    if not has_datetime_index(df):
        # Fixture-shaped frame: the whole thing is the session.
        return df
    frame = to_eastern(df)
    target = session_date or latest_session_date(frame)
    if target is None:
        return pd.DataFrame()
    same_day = frame[[stamp.date() == target for stamp in frame.index]]
    if not regular_only or len(same_day) == 0:
        return same_day
    in_hours = [start <= stamp.time() < end for stamp in same_day.index]
    return same_day[in_hours]


def opening_range(df, minutes: int) -> Tuple[Optional[float], Optional[float], pd.DataFrame]:
    """(high, low, bars) of the first `minutes` of the given session.

    The window is taken from the FIRST BAR'S timestamp rather than from
    a hardcoded 09:30, so a session that opened late (a halt, a
    half-day) still gets a range measured from when trading actually
    started rather than an empty one.

    For a frame with no datetime index the first `minutes` ROWS are
    used, which makes the one-minute-bar fixtures in the tests behave the
    way their name says.
    """
    if df is None or len(df) == 0:
        return None, None, pd.DataFrame()
    if not has_datetime_index(df):
        window = df.iloc[: max(1, int(minutes))]
    else:
        first = df.index[0]
        cutoff = first + pd.Timedelta(minutes=int(minutes))
        window = df[df.index < cutoff]
        if len(window) == 0:
            window = df.iloc[:1]
    highs = pd.to_numeric(window.get("High", window.get("high")), errors="coerce").dropna()
    lows = pd.to_numeric(window.get("Low", window.get("low")), errors="coerce").dropna()
    high = float(highs.max()) if not highs.empty else None
    low = float(lows.min()) if not lows.empty else None
    return high, low, window


def previous_daily_close(daily, *, before: Optional[date]) -> Optional[float]:
    """The close of the last daily bar STRICTLY BEFORE `before`.

    Not `close.iloc[-2]`. Whether the daily frame already contains a
    partial bar for today depends on the time of day the scan runs, so
    a fixed offset picks yesterday's close in the morning and the day
    before's in the afternoon. A gap measured against the wrong prior
    close is wrong by roughly a day's move -- large enough to move a
    name in and out of the 2-8% band without anything looking amiss.
    """
    if daily is None or len(daily) == 0:
        return None
    closes = pd.to_numeric(daily.get("Close", daily.get("close")), errors="coerce").dropna()
    if closes.empty:
        return None
    if before is None or not isinstance(closes.index, pd.DatetimeIndex):
        return float(closes.iloc[-2]) if len(closes) >= 2 else None
    index = closes.index
    if index.tz is not None:
        stamps = [value.tz_convert(EASTERN).date() for value in index]
    else:
        stamps = [value.date() for value in index]
    earlier = [value for stamp, value in zip(stamps, closes.tolist()) if stamp < before]
    if not earlier:
        return None
    return float(earlier[-1])
