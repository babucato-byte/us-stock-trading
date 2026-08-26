"""KIS's US-market windows, defined where KIS defines them: in KST.

Why not in Eastern time
-----------------------
Because KIS does not define them in Eastern time, and the difference is
not cosmetic. Its published schedule is a fixed KOREAN clock:

    주간거래   10:00-17:00 KST (DST)    10:00-18:00 KST (standard)
    프리마켓   17:00-22:30              18:00-23:30
    정규장     22:30-05:00              23:30-06:00
    애프터마켓 05:00-07:00              06:00-07:00
    애프터마켓 연장 07:00-09:00          07:00-09:00

The KST->ET offset moves by an hour with US daylight saving, so a window
that is fixed in KST is NOT fixed in ET. KIS shortens the daytime window
in summer (17:00 rather than 18:00 close) precisely so its ET *close*
stays 04:00 -- which means the ET *open* moves, from 20:00 in standard
time to 21:00 under DST.

`scan_session` had OVERNIGHT_DAYTIME hardcoded as 20:00->04:00 ET. That
is right in December and an hour early in August, so every DST day
carried a one-hour window -- 20:00-21:00 ET, 09:00-10:00 KST -- in which
the system believed it could place a daytime order while KIS had no
session open at all. It is not even the tail of the previous session:
the aftermarket extension has ended by then and 주간거래 has not begun.

So the schedule is expressed in KST, once, and every ET answer is derived
from it rather than asserted alongside it.

Two families, not five sessions
-------------------------------
What matters for an order is which API family it must be addressed to,
and there are only two:

    GENERAL  프리마켓 / 정규장 / 애프터마켓
             /trading/order + /trading/order-rvsecncl
             TTTT1002U buy, TTTT1006U sell, TTTT1004U cancel

    DAYTIME  미국주간거래
             /trading/daytime-order + /trading/daytime-order-rvsecncl
             TTTS6036U buy, TTTS6037U sell, TTTS6038U cancel

The general family covers premarket and aftermarket as well as the
regular session -- the overseas order API documents US orders in all
three. An earlier reading of "there is no premarket-specific TR" as "the
API cannot order in premarket" was wrong, and it cost those sessions.

The aftermarket EXTENSION is deliberately separate and unsupported. It
requires a per-customer application through HTS or the mobile app, so
whether an API order is accepted in it does not follow from the general
schedule. It is left UNVERIFIED rather than assumed either way: this
module never guesses a capability it has not established.
"""

from datetime import time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from market_hours import EASTERN, is_market_day

KST = ZoneInfo("Asia/Seoul")

WINDOW_DAYTIME = "DAYTIME"
WINDOW_PREMARKET = "PREMARKET"
WINDOW_REGULAR = "REGULAR"
WINDOW_AFTERMARKET = "AFTERMARKET"
WINDOW_AFTERMARKET_EXTENSION = "AFTERMARKET_EXTENSION"
WINDOW_CLOSED = "CLOSED"

FAMILY_GENERAL = "GENERAL"
FAMILY_DAYTIME = "DAYTIME"

#: Window -> the API family an order in it must be addressed to.
#:
#: AFTERMARKET_EXTENSION is absent, not mapped to GENERAL: absent means
#: "no capability established", which the resolver refuses. Mapping it to
#: GENERAL would be assuming that a session gated behind a separate
#: application accepts API orders, which is exactly the kind of guess
#: that put daytime orders on the regular endpoint.
FAMILY_BY_WINDOW = {
    WINDOW_PREMARKET: FAMILY_GENERAL,
    WINDOW_REGULAR: FAMILY_GENERAL,
    WINDOW_AFTERMARKET: FAMILY_GENERAL,
    WINDOW_DAYTIME: FAMILY_DAYTIME,
}

#: (start, end) in KST. `end` is exclusive. REGULAR wraps past midnight
#: and is handled explicitly rather than by splitting it into two rows,
#: so the table reads the way KIS publishes it.
_DST = {
    WINDOW_DAYTIME: (time(10, 0), time(17, 0)),
    WINDOW_PREMARKET: (time(17, 0), time(22, 30)),
    WINDOW_REGULAR: (time(22, 30), time(5, 0)),
    WINDOW_AFTERMARKET: (time(5, 0), time(7, 0)),
    WINDOW_AFTERMARKET_EXTENSION: (time(7, 0), time(9, 0)),
}
_STANDARD = {
    WINDOW_DAYTIME: (time(10, 0), time(18, 0)),
    WINDOW_PREMARKET: (time(18, 0), time(23, 30)),
    WINDOW_REGULAR: (time(23, 30), time(6, 0)),
    WINDOW_AFTERMARKET: (time(6, 0), time(7, 0)),
    WINDOW_AFTERMARKET_EXTENSION: (time(7, 0), time(9, 0)),
}


def _eastern_is_dst(moment) -> bool:
    """Whether US Eastern is on daylight time at `moment`.

    Asked of the zone rather than of a date range, so the changeover
    dates are the platform's and not a second copy that can go stale.
    KIS follows the US changeover, not Korea's -- Korea has no DST.
    """
    return moment.astimezone(EASTERN).dst() != timedelta(0)


def windows_for(moment):
    """The KST window table in force at `moment`."""
    return _DST if _eastern_is_dst(moment) else _STANDARD


def _in_window(clock, start, end) -> bool:
    if start <= end:
        return start <= clock < end
    # Wraps past midnight: 22:30->05:00 is "at or after 22:30, or before
    # 05:00", which a naive `start <= clock < end` would call empty.
    return clock >= start or clock < end


def window_at(moment) -> str:
    """Which KIS window `moment` falls in, or CLOSED.

    CLOSED is a real answer, not a fallback. Under DST the KST hour
    09:00-10:00 belongs to no window at all -- the extension has ended
    and daytime has not opened -- and that hour is exactly the one the
    fixed-ET boundary used to mislabel.
    """
    if moment is None:
        return WINDOW_CLOSED
    try:
        clock = moment.astimezone(KST).time()
    except Exception:  # noqa: BLE001 - fail closed
        return WINDOW_CLOSED
    for name, (start, end) in windows_for(moment).items():
        if _in_window(clock, start, end):
            return name
    return WINDOW_CLOSED


def family_for_window(window) -> Optional[str]:
    """The API family for a window, or None when none is established."""
    return FAMILY_BY_WINDOW.get(window)


def trading_day_for(moment, window=None) -> Optional[str]:
    """The US trading day a window belongs to, as an ISO date.

    Every window is dated to the Eastern calendar day of the REGULAR
    session it belongs with. That is the Eastern date for premarket,
    regular and the aftermarket that follows it -- but NOT for daytime,
    which runs the evening BEFORE the session it precedes. A daytime
    window opening 21:00 ET on the 26th is the overnight ahead of the
    27th, and dating it to the 26th would let a Friday evening inherit
    Friday's tradability and place an order into a Saturday.
    """
    if moment is None:
        return None
    try:
        eastern = moment.astimezone(EASTERN)
    except Exception:  # noqa: BLE001
        return None
    window = window or window_at(moment)
    day = eastern.date()
    # Daytime is the only window that can sit on the evening side of
    # midnight; when it does, it belongs to the following day.
    if window == WINDOW_DAYTIME and eastern.hour >= 12:
        day = day + timedelta(days=1)
    return day.isoformat()


def is_trading_day(day_iso) -> bool:
    """Whether `day_iso` is a US trading day (weekday, not a holiday)."""
    from datetime import date

    try:
        return bool(is_market_day(date.fromisoformat(day_iso)))
    except Exception:  # noqa: BLE001
        return False


def describe(moment) -> dict:
    """Everything this module knows about `moment`, for reports."""
    window = window_at(moment)
    day = trading_day_for(moment, window)
    return {
        "window": window,
        "family": family_for_window(window),
        "trading_day": day,
        "is_trading_day": is_trading_day(day) if day else False,
        "eastern_dst": _eastern_is_dst(moment) if moment else None,
        "kst": moment.astimezone(KST).isoformat() if moment else None,
        "eastern": moment.astimezone(EASTERN).isoformat() if moment else None,
    }
