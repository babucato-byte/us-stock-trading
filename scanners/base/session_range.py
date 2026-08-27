"""Each session's own opening range.

Why not one range for the day
-----------------------------
A breakout of 09:30's range means nothing at 20:00. The participants,
the liquidity and the reference price are all different, so each session
forms its range from its OWN opening. Reusing REGULAR's range elsewhere
would report a breakout of a level nobody in that session was trading
against.

The midnight problem
--------------------
Three sessions fit inside one calendar day and one does not.
OVERNIGHT_DAYTIME runs 20:00 -> 04:00, so its bars span two dates and the
usual `start <= t < end` test is false for every one of them. A session
window that silently matched nothing would produce "no range" forever,
which reads exactly like a quiet session -- so the wrap is handled
explicitly and tested at both ends of midnight.

The session DATE of a wrapping session is the date it STARTED. A bar at
01:00 on the 22nd belongs to the session that opened at 20:00 on the
21st, and filing it under the 22nd would split one session in half and
give each half a range built from part of the data.

The range window is a comparison, not a decision
------------------------------------------------
REGULAR keeps its measured 15 minutes. The other sessions accept
`minutes` from the caller precisely so 5, 15 and 30 can be measured
against each other before one of them becomes the answer -- see
`config/s6_sessions.SHADOW_RANGE_MINUTES`. Nothing here picks a winner.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional

import pandas as pd

from market_hours import (
    MARKET_AFTERMARKET_END,
    MARKET_PREMARKET_START,
    MARKET_REGULAR_END,
    MARKET_REGULAR_START,
)
from scanners.base.session import has_datetime_index, to_eastern

#: Session -> (start, end) in Eastern time. Boundaries are the ones
#: `market_hours` already defines; no new clock constant is introduced.
SESSION_WINDOWS = {
    "PREMARKET": (MARKET_PREMARKET_START, MARKET_REGULAR_START),
    "REGULAR": (MARKET_REGULAR_START, MARKET_REGULAR_END),
    "AFTER_HOURS": (MARKET_REGULAR_END, MARKET_AFTERMARKET_END),
    # The only window that wraps midnight.
    "OVERNIGHT_DAYTIME": (MARKET_AFTERMARKET_END, MARKET_PREMARKET_START),
}

WRAPPING_SESSIONS = frozenset({"OVERNIGHT_DAYTIME"})


@dataclass(frozen=True)
class SessionRange:
    """One session's opening range, and what it was built from."""

    session: str
    range_start: Optional[datetime] = None
    range_end: Optional[datetime] = None
    range_high: Optional[float] = None
    range_low: Optional[float] = None
    bars: int = 0
    minutes: int = 0

    @property
    def complete(self) -> bool:
        """Whether a usable range exists.

        Both bounds AND at least one bar. A high without a low is not a
        range, and a range from zero bars is a pair of Nones that would
        compare False against every price -- silently never breaking out.
        """
        return (self.range_high is not None and self.range_low is not None
                and self.bars > 0)

    def breaks_out(self, price) -> Optional[bool]:
        """Is `price` above the range high? None when unanswerable.

        None rather than False for an incomplete range: "no breakout" and
        "no range to break" are different facts, and the second one means
        the scan has nothing to say yet.
        """
        if not self.complete or price is None:
            return None
        try:
            return float(price) > float(self.range_high)
        except (TypeError, ValueError):
            return None

    def reenters(self, price) -> Optional[bool]:
        """Has price fallen back INSIDE the range? The S6 exit signal."""
        if not self.complete or price is None:
            return None
        try:
            return float(price) <= float(self.range_high)
        except (TypeError, ValueError):
            return None

    def as_dict(self):
        return {
            "session": self.session,
            "range_start": self.range_start.isoformat() if self.range_start else None,
            "range_end": self.range_end.isoformat() if self.range_end else None,
            "range_high": self.range_high,
            "range_low": self.range_low,
            "range_bars": self.bars,
            "range_minutes": self.minutes,
        }


def window_for(session) -> Optional[tuple]:
    return SESSION_WINDOWS.get(str(session or "").strip().upper())


def wraps_midnight(session) -> bool:
    return str(session or "").strip().upper() in WRAPPING_SESSIONS


def session_start_date(moment, session) -> Optional[date]:
    """The date the session containing `moment` STARTED.

    For a wrapping session this is yesterday when the clock is past
    midnight: a bar at 01:00 belongs to the session that opened at 20:00
    the previous evening. Filing it under its own calendar date would
    split one session in two and build each half a range from part of the
    data.
    """
    window = window_for(session)
    if window is None or moment is None:
        return None
    start, _end = window
    if not wraps_midnight(session):
        return moment.date()
    return moment.date() if moment.time() >= start else moment.date() - timedelta(days=1)


def current_session_date(session, now=None) -> Optional[date]:
    """The date of the session happening NOW, in Eastern terms.

    The distinction this exists for
    -------------------------------
    `slice_session_bars` with no `session_date` takes the most recent
    date that HAS bars for the session. That is not the same as the
    current session, and on 2026-08-27 the difference was the whole
    problem: the provider had published nothing for the day, so a
    PREMARKET slice returned eight bars from 2026-08-26 09:25 ET and the
    scanner described them as today's premarket.

    Passing this instead makes an absent session an EMPTY slice, which
    the caller can report as NO_CURRENT_SESSION_DATA rather than
    silently analysing yesterday.
    """
    from datetime import datetime, timezone

    from market_hours import EASTERN

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return session_start_date(moment.astimezone(EASTERN), session)


def slice_session_bars(df, session, *, session_date: Optional[date] = None):
    """Every bar belonging to one session, oldest first.

    Empty frame rather than None when nothing matches -- the caller turns
    that into an explicit "no range" rather than an exception, because a
    session with no bars yet is normal at its start.
    """
    window = window_for(session)
    if df is None or len(df) == 0 or window is None:
        return pd.DataFrame()
    if not has_datetime_index(df):
        # Fixture-shaped frame: the whole thing is the session.
        return df

    frame = to_eastern(df)
    start, end = window

    if not wraps_midnight(session):
        target = session_date
        if target is None:
            candidates = [s.date() for s in frame.index
                          if start <= s.time() < end]
            if not candidates:
                return pd.DataFrame()
            target = max(candidates)
        keep = [s.date() == target and start <= s.time() < end
                for s in frame.index]
        return frame[keep]

    # The wrapping case: 20:00 -> 04:00 across two dates.
    target = session_date
    if target is None:
        starts = [session_start_date(s, session) for s in frame.index
                  if s.time() >= start or s.time() < end]
        starts = [d for d in starts if d is not None]
        if not starts:
            return pd.DataFrame()
        target = max(starts)
    keep = []
    for stamp in frame.index:
        in_window = stamp.time() >= start or stamp.time() < end
        keep.append(in_window and session_start_date(stamp, session) == target)
    return frame[keep]


def opening_range(df, session, *, minutes: int,
                  session_date: Optional[date] = None) -> SessionRange:
    """The first `minutes` of one session, as a range.

    Measured from the FIRST BAR'S timestamp rather than from the
    session's nominal open: a session whose data begins late still gets a
    range of the requested width, instead of one truncated by however
    long the feed took to start.
    """
    session_name = str(session or "").strip().upper()
    bars = slice_session_bars(df, session_name, session_date=session_date)
    if bars is None or len(bars) == 0:
        return SessionRange(session=session_name, minutes=int(minutes))

    if not has_datetime_index(bars):
        window = bars
        first = last = None
    else:
        first = bars.index[0]
        cutoff = first + timedelta(minutes=int(minutes))
        window = bars[[stamp < cutoff for stamp in bars.index]]
        last = window.index[-1] if len(window) else None

    if len(window) == 0:
        return SessionRange(session=session_name, minutes=int(minutes))

    highs = window["High"] if "High" in window else window.get("high")
    lows = window["Low"] if "Low" in window else window.get("low")
    high = float(highs.max()) if highs is not None and len(highs) else None
    low = float(lows.min()) if lows is not None and len(lows) else None

    return SessionRange(
        session=session_name,
        range_start=first.to_pydatetime() if first is not None else None,
        range_end=last.to_pydatetime() if last is not None else None,
        range_high=high, range_low=low, bars=len(window),
        minutes=int(minutes))


def shadow_ranges(df, session, *, windows, session_date: Optional[date] = None):
    """One range per candidate window, for the 5/15/30 comparison.

    Returned side by side so a study reads them together. Nothing here
    ranks them: choosing a window is what the comparison is FOR, and
    picking one now would answer the question before collecting the data.
    """
    return {int(m): opening_range(df, session, minutes=int(m),
                                  session_date=session_date)
            for m in windows}
