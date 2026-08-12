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

from typing import Optional

import market_hours


def us_trading_day(now: Optional[object] = None) -> str:
    """The US-Eastern calendar date as `YYYY-MM-DD`.

    DST is handled by the zoneinfo conversion inside `eastern_now`, not
    by an offset constant.
    """
    upstream = getattr(market_hours, "us_trading_day", None)
    if callable(upstream):
        return upstream(now)
    return market_hours.eastern_now(now).date().isoformat()
