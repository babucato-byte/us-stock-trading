"""Resolving report date ranges, and the one definition of "today".

Why "today" needs a definition at all
-------------------------------------
Every signal is labelled with `trading_calendar.us_trading_day()` -- the
US-EASTERN calendar date. A report that anchors its default range on
`date.today()` instead is asking the LOCAL system clock, and those two
are not the same day for several hours out of every twenty-four.

On the Oracle server, which runs UTC, `date.today()` rolls over at
19:00 or 20:00 Eastern depending on DST. A weekly report run by cron at
18:00 UTC Sunday would resolve to the week starting the following
Monday -- i.e. an empty window -- while every signal in the store is
dated by the Eastern calendar. The report would render perfectly and
show nothing, and nothing about the output would say why.

So all three reports take their anchor from here, and `today()` is the
Eastern trading day, matching what the signals are labelled with.

Partial ranges
--------------
`resolve_range` accepts either endpoint, both, or neither:

    both given      used as-is
    neither         [today - window, today]
    --start only    [start, today]          "from then until now"
    --end only      [end - window, end]     "the window ending then"

Each missing endpoint is derived from the one that was given, using the
same window as the no-argument default. The alternative -- refusing
anything but a complete pair -- was rejected because both partial forms
have exactly one sensible reading, and requiring the other half is
friction with no safety benefit.

Invalid input is refused rather than absorbed. A malformed date used to
pass straight through into a string comparison against stored day keys,
which matched nothing and produced an empty report indistinguishable
from a quiet month. Reports that silently return nothing when misused
are how a broken cron entry survives for weeks.
"""

from datetime import date, datetime, timedelta
from typing import Optional, Tuple

from scanners.base.trading_calendar import us_trading_day

#: Default lookback for a report that was given no range at all.
#: Thirty calendar days is roughly a month of sessions -- long enough
#: for the 5-day horizons to have matured over most of the window,
#: which is what makes an intersection analysis worth reading.
DEFAULT_WINDOW_DAYS = 30

DAY_FORMAT = "%Y-%m-%d"


class DateRangeError(ValueError):
    """A date argument was malformed, or the range runs backwards."""


def today(now=None) -> date:
    """The current US-Eastern trading day, as a `date`.

    Deliberately not `date.today()`. See the module docstring.
    """
    return datetime.strptime(us_trading_day(now), DAY_FORMAT).date()


def parse_day(value, *, label: str) -> date:
    """Parse `YYYY-MM-DD`, or raise with a message naming the argument."""
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value).strip(), DAY_FORMAT).date()
    except (TypeError, ValueError):
        raise DateRangeError(
            f"{label} must be a date in YYYY-MM-DD form, got {value!r}") from None


def recent_bounds(days: int = DEFAULT_WINDOW_DAYS, *, end=None) -> Tuple[str, str]:
    """`days` calendar days ending at `end` (default: today), inclusive.

    Calendar days, not sessions: the store is keyed by trading day and a
    30-calendar-day window simply contains whichever sessions occurred.
    Counting sessions instead would require a market calendar lookup to
    answer "what range am I reporting on", and would make the window's
    length depend on where the holidays fell.
    """
    window = int(days)
    if window < 1:
        raise DateRangeError(f"window must be at least 1 day, got {days!r}")
    last = parse_day(end, label="--end") if end is not None else today()
    first = last - timedelta(days=window - 1)
    return first.isoformat(), last.isoformat()


def resolve_range(
    start: Optional[str],
    end: Optional[str],
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> Tuple[str, str]:
    """Fill in whichever endpoints were not supplied.

    Returns ISO strings, because that is what the store's day keys are
    and what every `*_report.build()` signature takes.
    """
    if start is None and end is None:
        return recent_bounds(window_days)

    if start is None:
        return recent_bounds(window_days, end=end)

    first = parse_day(start, label="--start")
    last = parse_day(end, label="--end") if end is not None else today()

    if first > last:
        raise DateRangeError(
            f"--start {first.isoformat()} is after --end {last.isoformat()}; "
            "the range would be empty")
    return first.isoformat(), last.isoformat()
