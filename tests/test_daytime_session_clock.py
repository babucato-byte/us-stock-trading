"""The daytime session clock, at every boundary, in both halves of the year.

주간거래 is published in KST and the KST->ET offset moves with US
daylight saving, so a window asserted in Eastern time is wrong for half
the year. `session_at` used to answer OVERNIGHT_DAYTIME for anything
that was not one of the other three -- a catch-all, not a window -- and
that made 00:00-01:00 UTC under DST a session in which the scanner,
the collector and the active watch all believed a shut venue was open.
"""

import datetime as dt

import pytest

from config import s6_sessions, session_capability
from config import kis_market_schedule as schedule
from s6_live import active_watch
from scanners.base import scan_session as ss

UTC = dt.timezone.utc
KST = schedule.KST

#: One date in each half of the year. US DST moves the ET offset; KST
#: does not move at all, which is the whole point.
DST_DAY = (2026, 9, 16)
STD_DAY = (2026, 1, 15)


def _utc(day, hh, mm=0, ss_=0):
    return dt.datetime(*day, hh, mm, ss_, tzinfo=UTC)


class TestTheDaytimeOpenIsFixedInKst:
    """The OPEN is 10:00 KST = 01:00 UTC, in both halves of the year."""

    @pytest.mark.parametrize("day", [DST_DAY, STD_DAY],
                             ids=["DST", "STANDARD"])
    def test_daytime_opens_at_0100_utc(self, day):
        assert ss.session_at(_utc(day, 0, 59, 59)) != ss.OVERNIGHT_DAYTIME
        assert ss.session_at(_utc(day, 1, 0, 0)) == ss.OVERNIGHT_DAYTIME

    @pytest.mark.parametrize("day", [DST_DAY, STD_DAY],
                             ids=["DST", "STANDARD"])
    def test_the_open_is_ten_kst_on_both_dates(self, day):
        opened = _utc(day, 1, 0).astimezone(KST)
        assert (opened.hour, opened.minute) == (10, 0)

    @pytest.mark.parametrize("day", [DST_DAY, STD_DAY],
                             ids=["DST", "STANDARD"])
    def test_0759_is_still_daytime_on_both_dates(self, day):
        assert ss.session_at(_utc(day, 7, 59, 59)) == ss.OVERNIGHT_DAYTIME


class TestTheClosedHour:
    """00:00-01:00 UTC under DST belongs to no venue at all."""

    @pytest.mark.parametrize("hh,mm", [(0, 0), (0, 30), (0, 59)])
    def test_the_hour_before_the_open_is_closed(self, hh, mm):
        assert ss.session_at(_utc(DST_DAY, hh, mm)) == ss.CLOSED

    def test_the_boundary_second_is_exact(self):
        assert ss.session_at(_utc(DST_DAY, 0, 59, 59)) == ss.CLOSED
        assert ss.session_at(_utc(DST_DAY, 1, 0, 0)) == ss.OVERNIGHT_DAYTIME

    def test_the_standard_time_evening_has_no_such_gap(self):
        """In standard time the aftermarket runs to 01:00 UTC and daytime
        opens there, so the two meet with nothing in between."""
        assert ss.session_at(_utc(STD_DAY, 0, 30)) == ss.AFTER_HOURS
        assert ss.session_at(_utc(STD_DAY, 1, 0)) == ss.OVERNIGHT_DAYTIME


class TestTheClockAndTheScheduleAgree:
    """The defect was two authorities answering 'which session is this'."""

    @pytest.mark.parametrize("day", [DST_DAY, STD_DAY],
                             ids=["DST", "STANDARD"])
    def test_every_minute_agrees_with_the_kis_schedule(self, day):
        for minute in range(0, 24 * 60, 1):
            moment = _utc(day, 0) + dt.timedelta(minutes=minute)
            clock = ss.session_at(moment)
            window = schedule.window_at(moment)
            if window == schedule.WINDOW_DAYTIME:
                assert clock == ss.OVERNIGHT_DAYTIME, moment
            else:
                assert clock != ss.OVERNIGHT_DAYTIME, moment

    @pytest.mark.parametrize("day", [DST_DAY, STD_DAY],
                             ids=["DST", "STANDARD"])
    def test_no_minute_claims_a_session_the_order_path_refuses(self, day):
        """A daytime session the capability layer calls MARKET_CLOSED is
        precisely the disagreement this removes."""
        for minute in range(0, 24 * 60, 5):
            moment = _utc(day, 0) + dt.timedelta(minutes=minute)
            if ss.session_at(moment) != ss.OVERNIGHT_DAYTIME:
                continue
            assert session_capability.capability_at(moment).window == "DAYTIME"


class TestTheOtherBoundariesAreUnmoved:
    """PREMARKET / REGULAR / AFTER_HOURS keep their Eastern boundaries."""

    def test_dst_transitions(self):
        assert ss.session_at(_utc(DST_DAY, 8, 0)) == ss.PREMARKET
        assert ss.session_at(_utc(DST_DAY, 13, 29)) == ss.PREMARKET
        assert ss.session_at(_utc(DST_DAY, 13, 30)) == ss.REGULAR
        assert ss.session_at(_utc(DST_DAY, 19, 59)) == ss.REGULAR
        assert ss.session_at(_utc(DST_DAY, 20, 0)) == ss.AFTER_HOURS
        assert ss.session_at(_utc(DST_DAY, 23, 59)) == ss.AFTER_HOURS
        # AFTER_HOURS -> CLOSED at 00:00 UTC the next day.
        assert ss.session_at(_utc(DST_DAY, 23, 59)
                             + dt.timedelta(minutes=1)) == ss.CLOSED

    def test_standard_transitions(self):
        assert ss.session_at(_utc(STD_DAY, 9, 0)) == ss.PREMARKET
        assert ss.session_at(_utc(STD_DAY, 14, 30)) == ss.REGULAR
        assert ss.session_at(_utc(STD_DAY, 21, 0)) == ss.AFTER_HOURS


class TestNothingDaytimeRunsWhileClosed:
    """Section 6: no scan, no watch scope, no orders, no collection."""

    @pytest.fixture
    def closed(self):
        return ss.session_at(_utc(DST_DAY, 0, 30))

    def test_s6_does_not_scan_it(self, closed):
        assert s6_sessions.scans(closed) is False
        assert s6_sessions.variant_for(closed) == ""

    def test_there_is_no_active_watch_scope(self, closed):
        assert active_watch.session_scope(closed, _utc(DST_DAY, 0, 30)) is None

    def test_no_orders_are_allowed(self, closed):
        assert s6_sessions.orders_allowed(closed) is False
        assert ss.order_route_verified(closed) is False

    def test_the_capability_layer_refuses_every_route(self):
        cap = session_capability.capability_at(
            _utc(DST_DAY, 0, 30), strategy_id="S6_ORB_BREAKOUT_V1")
        assert cap.entry_supported is False
        assert cap.exit_supported is False
        assert cap.orders_allowed is False
        assert cap.order_route_buy is None
        assert cap.order_route_sell is None
        assert cap.cancel_route is None
        assert cap.entry_reason == "MARKET_CLOSED"

    def test_the_collector_refuses_to_start(self):
        """`SCAN_SESSIONS` is the gate the runner checks."""
        assert ss.session_at(_utc(DST_DAY, 0, 30)) not in \
            s6_sessions.SCAN_SESSIONS


class TestTheDaytimeOrbOrigin:
    """Section 3: the opening range is measured from 10:00 KST."""

    @pytest.mark.parametrize("day,expected_utc", [
        ((2026, 9, 15), "2026-09-16 01:00"),
        ((2026, 1, 15), "2026-01-16 01:00"),
    ], ids=["DST", "STANDARD"])
    def test_origin_is_0100_utc_on_both_dates(self, day, expected_utc):
        from scanners.base import session_range as sr

        origin = sr.official_origin("OVERNIGHT_DAYTIME", dt.date(*day))
        assert origin.astimezone(UTC).strftime("%Y-%m-%d %H:%M") == expected_utc
        assert origin.astimezone(KST).hour == 10

    def test_the_other_origins_are_unchanged(self):
        from scanners.base import session_range as sr

        day = dt.date(2026, 9, 15)
        assert sr.official_origin("PREMARKET", day).hour == 4
        assert sr.official_origin("REGULAR", day).hour == 9
        assert sr.official_origin("AFTER_HOURS", day).hour == 16
