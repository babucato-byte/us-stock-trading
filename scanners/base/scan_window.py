"""May a scan run right now? One answer, from the calendar and the clock.

The defect this replaces
------------------------
`s6_scan.sh` asked `get_market_state() == "CLOSED"` and exited if so.
That question is about the US REGULAR market, and it is CLOSED for the
whole of OVERNIGHT_DAYTIME, PREMARKET and AFTER_HOURS -- so S6's
all-session family could only ever scan in REGULAR. In practice it never
scanned at all: `s6_scan.log` was never created.

"The regular market is closed" and "there is nothing to scan" are
different claims. The first is about one venue's hours; the second is
about whether this moment belongs to a session on a day the market
trades. Only the second may stop a scan.

What actually gates a scan
--------------------------
    calendar_trading_day    a real US trading day -- not a weekend, not
                            an NYSE holiday
    session                 which of the four this moment is in

The four sessions partition the day with no gap, so every moment on a
trading day belongs to exactly one of them. There is therefore no
"outside a session" case to refuse; the calendar is the whole gate.

The trading day is TODAY's, not the session's start date
--------------------------------------------------------
At 01:00 ET Monday the overnight session started at 20:00 ET Sunday, and
its `session_date` is Sunday -- that is the range's identity and is
computed by the existing engine. But whether a scan may RUN is asked of
the current moment: Monday is a trading day, so it runs. Saturday 01:00
is refused because Saturday is not, even though its session opened on a
Friday that was.

Scanning is not ordering
------------------------
Nothing here widens what may be traded. `scan_allowed` answers one
question; whether an order may be placed is `s6_sessions.orders_allowed`
plus the rollout policy plus the broker route, and none of them consult
this module.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Why a scan may or may not run. Separated because §9 needs
#: PRODUCER_MISSING to mean "a valid window produced nothing", which is
#: only meaningful once NOT_APPLICABLE windows are excluded.
VALID_TRADING_DAY = "VALID_TRADING_DAY"
WEEKEND = "WEEKEND"
US_MARKET_HOLIDAY = "US_MARKET_HOLIDAY"
CALENDAR_UNAVAILABLE = "CALENDAR_UNAVAILABLE"

#: Reasons that mean "this window does not exist", as opposed to "it
#: exists and nothing happened".
NOT_APPLICABLE_REASONS = frozenset({WEEKEND, US_MARKET_HOLIDAY})


@dataclass(frozen=True)
class ScanWindow:
    """Everything a report needs to say WHY, without conflating fields."""

    moment: datetime
    session: Optional[str]
    session_date: Optional[date]
    calendar_trading_day: bool
    scan_allowed: bool
    reason: str
    #: The US REGULAR market's own state. Reported alongside, never used
    #: as the scan gate -- it is CLOSED for three of the four sessions.
    regular_market_state: Optional[str] = None

    @property
    def not_applicable(self) -> bool:
        return self.reason in NOT_APPLICABLE_REASONS

    def as_dict(self) -> Dict[str, Any]:
        return {
            "session": self.session,
            "session_date": (self.session_date.isoformat()
                             if self.session_date else None),
            "calendar_trading_day": self.calendar_trading_day,
            "scan_allowed": self.scan_allowed,
            "reason": self.reason,
            "regular_market_state": self.regular_market_state,
        }


def _is_weekend(moment: datetime) -> bool:
    return moment.weekday() >= 5


def evaluate(moment: Optional[datetime] = None, *, scans=None) -> ScanWindow:
    """Whether a scan may run at `moment`. Never raises.

    `scans(session)` decides whether the CALLER scans that session at
    all; it defaults to S6's family, which scans all four. A caller that
    only runs in some sessions passes its own.

    A calendar that cannot be read refuses the scan. Guessing "probably a
    trading day" would publish candidates dated to a day the market never
    opened, and that row would sit in the hand-off store looking exactly
    like a real one.
    """
    from market_hours import EASTERN
    from scanners.base import scan_session, session_range

    current = moment or datetime.now(EASTERN)
    if current.tzinfo is None:
        current = current.replace(tzinfo=EASTERN)
    eastern = current.astimezone(EASTERN)

    session = scan_session.session_at(eastern)
    session_date = session_range.session_start_date(eastern, session)

    try:
        from market_hours import get_market_state

        market_state = get_market_state(eastern)
    except Exception:  # noqa: BLE001 - a display value must not decide
        market_state = None

    if _is_weekend(eastern):
        return ScanWindow(eastern, session, session_date, False, False,
                          WEEKEND, market_state)

    try:
        from market_guard import is_us_trading_day

        trading_day = bool(is_us_trading_day(eastern))
    except Exception as exc:  # noqa: BLE001
        logger.warning("US trading calendar unavailable; refusing to scan",
                       exc_info=True)
        return ScanWindow(eastern, session, session_date, False, False,
                          CALENDAR_UNAVAILABLE, market_state)

    if not trading_day:
        return ScanWindow(eastern, session, session_date, False, False,
                          US_MARKET_HOLIDAY, market_state)

    scannable = scans if scans is not None else _s6_scans
    allowed = bool(scannable(session))
    return ScanWindow(eastern, session, session_date, True, allowed,
                      VALID_TRADING_DAY, market_state)


def _s6_scans(session) -> bool:
    from config import s6_sessions

    return s6_sessions.scans(session)


def probe(moment: Optional[datetime] = None) -> str:
    """One line for a shell caller: `SESSION` or a refusal reason.

    `s6_scan.sh` reads this. It prints the session name when a scan may
    run and the REASON otherwise, so the cron log records why a window
    was skipped rather than leaving a silent gap.
    """
    window = evaluate(moment)
    return window.session if window.scan_allowed else window.reason
