"""Which trading day is it -- the scanner package's adapter.

Why this module exists
----------------------
Scanner code needs one thing from the calendar that `market_hours` does
not offer on every branch: the US-EASTERN calendar date, as an ISO
string, to label a day's signals with.

That is deliberately not the same question as `market_hours.is_market_day`,
which answers "is this date a session?" and returns a bool. Substituting
one for the other would put `True` in `trading_day` where a date belongs.
It is also not the UTC date: a scan that runs at 09:20 ET on Aug 12 is
already Aug 12 in UTC, but one running at 16:30 ET on Aug 12 is Aug 13
in UTC, so a UTC-dated label would file an afternoon scan under the next
session and split one trading day's signals across two files.

Delegation, not reimplementation
--------------------------------
`market_hours.us_trading_day` exists on the branch this framework was
first written against and is the definition the per-day trading limits
there are scoped to. Where it exists, it is used. Only where it does not
does this module compute the same value from `market_hours.eastern_now`,
which every branch has.

That ordering matters more than the three lines it costs. If the
scanners carried their own copy unconditionally, a signal's
`trading_day` and an entry ledger's day boundary could drift apart after
any edit to either -- and the two disagreeing about which day it is, is
exactly the class of bug that only shows up around a session boundary
and only in production.
"""

from datetime import date, timedelta
from typing import Optional

import market_hours


class TradingCalendarError(Exception):
    """The calendar could not answer. Callers must not guess a day."""


def next_trading_day(day) -> str:
    """The next US session strictly after `day`, as `YYYY-MM-DD`.

    Delegates to `market_hours.is_market_day`, which takes a date and
    therefore works for a FUTURE day -- unlike `market_guard.
    is_us_trading_day()`, which takes no argument and can only answer
    for right now. That distinction matters here: the evening watchlist
    pass has to file its output under tomorrow's session, which is by
    definition not today.

    Raises rather than falling back to "day + 1" if no session is found
    within a fortnight. A watchlist filed under the wrong date is read
    on the wrong morning, and nothing in the file would say so.
    """
    if isinstance(day, str):
        try:
            day = date.fromisoformat(day)
        except ValueError as exc:
            raise TradingCalendarError(f"not a date: {day!r}") from exc
    cursor = day
    for _ in range(14):
        cursor = cursor + timedelta(days=1)
        if market_hours.is_market_day(cursor):
            return cursor.isoformat()
    raise TradingCalendarError(
        f"no US trading day within two weeks after {day.isoformat()}")


def us_trading_day(now: Optional[object] = None) -> str:
    """The US-Eastern calendar date as `YYYY-MM-DD`.

    DST is handled by the zoneinfo conversion inside `eastern_now`, not
    by an offset constant.
    """
    upstream = getattr(market_hours, "us_trading_day", None)
    if callable(upstream):
        return upstream(now)
    return market_hours.eastern_now(now).date().isoformat()
