"""One answer to "what trading day is it", for every component.

The disagreement this settles
-----------------------------
On 2026-08-30 at 20:22 ET the system reported session=OVERNIGHT_DAYTIME
with trading_day=2026-08-30 -- a Sunday, which is not a trading day at
all. Manifests were dated to it, the scanner logged skipped=WEEKEND, and
discovery produced nothing.

Nothing was actually broken in the order path. `session_capability` knew
perfectly well that KIS was closed, and refused. What produced the wrong
date was that TWO authorities answer "which session is this":

  scan_session.session_at()   pure ET clock; anything after 20:00 ET is
                              OVERNIGHT_DAYTIME. Deliberately
                              calendar-independent -- a holiday still has
                              a premarket window on the clock.

  session_capability          derived from KIS's KST windows, where
                              daytime is 10:00-17:00 KST = 21:00 ET
                              under DST.

Between 20:00 and 21:00 ET they disagree, and in that hour the first one
names a session while the second correctly says CLOSED. The trading day
then fell back to the Eastern calendar date, which on a Sunday evening
is Sunday.

Two rules that are easy to get wrong
------------------------------------
An evening daytime window belongs to the session it PRECEDES, not the
calendar date it starts on. And the following day is not "+1": Friday
evening does not precede Saturday, and the evening before a holiday does
not precede the holiday. Both must resolve to the next VALID trading
day, from the trading calendar, or they resolve to nothing at all.

Dating work is not permitting orders
------------------------------------
This module decides which day a manifest, scan or log belongs to. It
does NOT decide whether an order may be placed: `session_capability`
remains the sole authority for that, and its answer is passed through
unchanged. So a Friday evening now dates correctly to the following
Monday while still reporting exit_supported=False, because whether KIS
accepts a daytime order on a Saturday KST morning is a fact about KIS
that this module has no business overriding. Fixing a date must never
become a way of opening a route.

Session and trading day are different facts
-------------------------------------------
`session_date` is the calendar date the clock reads. `operational_trading_day`
is the session the work belongs to. Conflating them is the whole defect,
so both are returned, separately named, and neither is derived from the
other by a caller.
"""

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from config import kis_market_schedule as schedule
from config import session_capability
from market_hours import EASTERN, is_market_day

logger = logging.getLogger(__name__)

#: The systemic reason every component reports when the calendar cannot
#: be resolved. One name, so a calendar fault surfaces once instead of
#: as five unrelated-looking failures.
TRADING_DAY_RESOLUTION_ERROR = "TRADING_DAY_RESOLUTION_ERROR"

NOT_A_TRADING_DAY = "NOT_A_TRADING_DAY"
MARKET_CLOSED = "MARKET_CLOSED"
CAPABLE = "CAPABLE"

#: How far ahead to look for the next valid trading day. Four days
#: covers a Friday evening across a long weekend; beyond that something
#: is wrong with the calendar itself and guessing further is worse than
#: refusing.
MAX_FORWARD_DAYS = 4


def next_valid_trading_day(day, *, include_self=True) -> Optional[date]:
    """The first trading day on or after `day`, or None.

    Never a bare +1. Friday evening does not precede Saturday, and the
    evening before a holiday does not precede the holiday.
    """
    if day is None:
        return None
    candidate = day if include_self else day + timedelta(days=1)
    for _ in range(MAX_FORWARD_DAYS + 1):
        if is_market_day(candidate):
            return candidate
        candidate += timedelta(days=1)
    return None


def resolve_operational_trading_day(now_et=None, session=None) -> dict:
    """Everything a component needs to date its work correctly.

    `session` is accepted for callers that already know which session
    they are acting for, but it is NOT trusted over the market schedule:
    the clock-derived session is exactly what was wrong. It is recorded
    as `declared_session` so a disagreement is visible rather than
    silently resolved.
    """
    moment = now_et or datetime.now(EASTERN)
    try:
        eastern = moment.astimezone(EASTERN)
    except Exception:  # noqa: BLE001
        logger.warning("could not read the moment as Eastern time",
                       exc_info=True)
        return _unresolved(moment, session)

    try:
        window = schedule.window_at(eastern)
        capability = session_capability.capability_at(eastern)
    except Exception:  # noqa: BLE001
        logger.warning("could not resolve the session capability",
                       exc_info=True)
        return _unresolved(moment, session)

    session_date = eastern.date()

    # The daytime window belongs to the session it PRECEDES, and it
    # straddles midnight: the evening half sits on the day before, the
    # small-hours half on the day itself. Both resolve forward to the
    # next valid trading day, which is why Saturday 02:00 -- the tail of
    # Friday evening's window -- lands on Monday rather than nowhere.
    #
    # Every other window belongs to the calendar day it happens on, and
    # on a non-trading day it belongs to no operational day at all: a
    # holiday premarket is a clock window, not a session.
    if window == schedule.WINDOW_DAYTIME:
        base = (session_date + timedelta(days=1) if eastern.hour >= 12
                else session_date)
        operational = next_valid_trading_day(base)
    elif window == schedule.WINDOW_CLOSED:
        operational = None
    else:
        operational = session_date if is_market_day(session_date) else None

    resolved_session = capability.session or None
    declared = str(session).upper() if session else None

    if operational is None:
        reason = (MARKET_CLOSED if window == schedule.WINDOW_CLOSED
                  else NOT_A_TRADING_DAY)
        entry_supported = exit_supported = False
    else:
        reason = capability.entry_reason or capability.exit_reason or CAPABLE
        entry_supported = bool(capability.entry_supported)
        exit_supported = bool(capability.exit_supported)

    return {
        "session": resolved_session,
        #: What the caller thought the session was. Present so the
        #: 20:00-21:00 ET disagreement is visible in the record instead
        #: of being quietly overwritten.
        "declared_session": declared,
        "session_disagreement": bool(declared and resolved_session
                                     and declared != resolved_session),
        "window": window,
        "session_date": session_date.isoformat(),
        "operational_trading_day": (operational.isoformat()
                                    if operational else None),
        "calendar_trading_day": is_market_day(session_date),
        "orders_allowed": bool(entry_supported or exit_supported),
        "entry_supported": entry_supported,
        "exit_supported": exit_supported,
        "reason": reason,
        "resolved": operational is not None,
    }


def _unresolved(moment, session) -> dict:
    """Fail closed, under the one systemic name.

    Entries stop. Held positions are NOT abandoned -- that decision
    belongs to the exit runtime, which is told the calendar is unusable
    rather than told the market is closed.
    """
    return {
        "session": None,
        "declared_session": str(session).upper() if session else None,
        "session_disagreement": False,
        "window": None,
        "session_date": None,
        "operational_trading_day": None,
        "calendar_trading_day": False,
        "orders_allowed": False,
        "entry_supported": False,
        "exit_supported": False,
        "reason": TRADING_DAY_RESOLUTION_ERROR,
        "resolved": False,
    }


def operational_trading_day(now_et=None) -> Optional[str]:
    """Just the day, for callers that need nothing else."""
    return resolve_operational_trading_day(now_et)["operational_trading_day"]


def prior_trading_day(day_iso) -> Optional[str]:
    """The valid trading day before `day_iso`.

    Used for seeding from the previous session. A plain -1 lands on
    Sunday every Monday, which is how a Monday premarket ends up seeded
    from nothing.
    """
    try:
        day = date.fromisoformat(str(day_iso))
    except (TypeError, ValueError):
        return None
    candidate = day - timedelta(days=1)
    for _ in range(MAX_FORWARD_DAYS + 1):
        if is_market_day(candidate):
            return candidate.isoformat()
        candidate -= timedelta(days=1)
    return None
