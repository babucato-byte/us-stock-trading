"""When may a scan run? §10's matrix.

The confusion this file exists to stop
--------------------------------------
`get_market_state() == "CLOSED"` is true for three of the four sessions.
Using it as the scan gate meant S6's all-session family could only ever
scan in REGULAR -- and in practice never scanned at all.

    "the regular market is closed"   one venue's hours
    "there is nothing to scan"       not a trading day

Only the second may stop a scan, and the tests below hold those apart:
every OVERNIGHT / PREMARKET / AFTER_HOURS case asserts `scan_allowed` is
True while `regular_market_state` is CLOSED or an extended-hours state.

Scanning is not ordering. The last class asserts that nothing here moved
what may be traded.
"""

import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scanners.base import scan_window  # noqa: E402

ET = ZoneInfo("America/New_York")

#: 2026-08-24 is a Monday; 2026-08-22 the Saturday before it.
MON = (2026, 8, 24)
SAT = (2026, 8, 22)
SUN = (2026, 8, 23)


def at(day, hour, minute=0):
    return datetime(*day, hour, minute, tzinfo=ET)


class TestATradingDayOpensEverySession:
    @pytest.mark.parametrize("hour,session", [
        (1, "OVERNIGHT_DAYTIME"),
        (8, "PREMARKET"),
        (10, "REGULAR"),
        (18, "AFTER_HOURS"),
    ])
    def test_each_session_is_scannable_on_a_monday(self, hour, session):
        window = scan_window.evaluate(at(MON, hour))
        assert window.session == session
        assert window.calendar_trading_day is True
        assert window.scan_allowed is True
        assert window.reason == scan_window.VALID_TRADING_DAY

    def test_the_regular_market_being_closed_does_not_stop_a_scan(self):
        """The whole defect, in one assertion."""
        window = scan_window.evaluate(at(MON, 1))
        assert window.regular_market_state == "CLOSED"
        assert window.scan_allowed is True

    @pytest.mark.parametrize("hour", [1, 8, 18])
    def test_the_non_regular_sessions_are_the_ones_that_were_unreachable(
            self, hour):
        window = scan_window.evaluate(at(MON, hour))
        assert window.scan_allowed is True
        assert window.regular_market_state != "REGULAR"


class TestAWeekendClosesEverySession:
    @pytest.mark.parametrize("hour", [1, 8, 10, 18])
    def test_saturday_is_refused_at_every_hour(self, hour):
        window = scan_window.evaluate(at(SAT, hour))
        assert window.calendar_trading_day is False
        assert window.scan_allowed is False
        assert window.reason == scan_window.WEEKEND
        assert window.not_applicable is True

    @pytest.mark.parametrize("hour", [1, 10, 22])
    def test_sunday_is_refused_too(self, hour):
        window = scan_window.evaluate(at(SUN, hour))
        assert window.scan_allowed is False
        assert window.reason == scan_window.WEEKEND

    def test_a_saturday_session_that_opened_on_a_trading_day_is_still_refused(
            self):
        """Saturday 01:00's overnight session opened Friday 20:00, and
        Friday WAS a trading day. The scan is still refused: the question
        is asked of the current moment, not the session's birthday."""
        window = scan_window.evaluate(at(SAT, 1))
        assert window.session == "OVERNIGHT_DAYTIME"
        assert window.session_date == date(2026, 8, 21)   # Friday
        assert window.scan_allowed is False


class TestHolidays:
    def test_a_us_market_holiday_is_refused(self, monkeypatch):
        import market_guard

        monkeypatch.setattr(market_guard, "is_us_trading_day",
                            lambda now=None: False)
        window = scan_window.evaluate(at(MON, 10))
        assert window.scan_allowed is False
        assert window.reason == scan_window.US_MARKET_HOLIDAY
        assert window.not_applicable is True

    def test_a_holiday_is_distinguishable_from_a_weekend(self, monkeypatch):
        """§2: WEEKEND and US_MARKET_HOLIDAY are separate answers."""
        import market_guard

        monkeypatch.setattr(market_guard, "is_us_trading_day",
                            lambda now=None: False)
        holiday = scan_window.evaluate(at(MON, 10)).reason
        weekend = scan_window.evaluate(at(SAT, 10)).reason
        assert holiday == scan_window.US_MARKET_HOLIDAY
        assert weekend == scan_window.WEEKEND
        assert holiday != weekend

    def test_an_unreadable_calendar_refuses_rather_than_guesses(self,
                                                                monkeypatch):
        """Guessing "probably a trading day" would publish candidates
        dated to a day the market never opened."""
        import market_guard

        def boom(now=None):
            raise RuntimeError("calendar unavailable")

        monkeypatch.setattr(market_guard, "is_us_trading_day", boom)
        window = scan_window.evaluate(at(MON, 10))
        assert window.scan_allowed is False
        assert window.reason == scan_window.CALENDAR_UNAVAILABLE
        # NOT "not applicable" -- this one needs fixing, not ignoring.
        assert window.not_applicable is False


class TestTheOvernightWrap:
    def test_after_midnight_belongs_to_the_previous_evening(self):
        """§3: 01:00 ET Monday is the session that opened 20:00 Sunday."""
        window = scan_window.evaluate(at(MON, 1))
        assert window.session == "OVERNIGHT_DAYTIME"
        assert window.session_date == date(2026, 8, 23)

    def test_before_midnight_is_its_own_date(self):
        window = scan_window.evaluate(at(MON, 22))
        assert window.session == "OVERNIGHT_DAYTIME"
        assert window.session_date == date(2026, 8, 24)

    def test_the_wrap_uses_the_existing_engine(self):
        """Not a second implementation of the same rule."""
        from scanners.base import session_range

        moment = at(MON, 1)
        assert scan_window.evaluate(moment).session_date == \
            session_range.session_start_date(moment, "OVERNIGHT_DAYTIME")

    @pytest.mark.parametrize("hour,expected", [(8, MON), (10, MON), (18, MON)])
    def test_non_wrapping_sessions_use_their_own_date(self, hour, expected):
        assert scan_window.evaluate(at(MON, hour)).session_date == \
            date(*expected)


class TestScanningIsNotOrdering:
    @pytest.mark.parametrize("hour", [1, 8, 10, 18])
    def test_scan_allowed_never_widens_what_may_be_ordered(self, hour):
        from config import s6_sessions

        window = scan_window.evaluate(at(MON, hour))
        assert window.scan_allowed is True
        # The order decision is asked elsewhere, and scanning never
        # widens it. The session SETS are equal now -- premarket and
        # aftermarket share the general route family with the regular
        # session -- so what narrows ordering is no longer the set but
        # the CLOCK: KIS runs no window in the DST hour between the
        # aftermarket extension and the daytime open, and none on a
        # weekend or holiday.
        assert s6_sessions.LIVE_SESSIONS <= s6_sessions.SCAN_SESSIONS
        assert window.session in s6_sessions.SCAN_SESSIONS

        from datetime import datetime

        from config import session_capability as sc
        from market_hours import EASTERN

        for closed in (datetime(2026, 8, 26, 20, 30, tzinfo=EASTERN),
                       datetime(2026, 8, 26, 18, 30, tzinfo=EASTERN),
                       datetime(2026, 8, 29, 22, 0, tzinfo=EASTERN)):
            assert sc.capability_at(closed).orders_allowed is False

    def test_the_module_consults_no_order_policy(self):
        import ast

        text = (REPO_ROOT / "scanners" / "base" / "scan_window.py").read_text()
        roots = set()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
        assert "brokers" not in roots
        assert "execution" not in roots
        assert "kis_live_trading" not in roots

    def test_the_live_modes_are_untouched(self):
        from config import scanner_live_mode as slm

        # Pinned to the INTENDED posture so this stays a tripwire on an
        # accidental change. `orb` moved to LIMITED_LIVE as a reviewed
        # promotion; S2 has not been promoted and S1 was not touched.
        assert slm.SCANNER_LIVE_MODE["orb"] == slm.MODE_LIMITED_LIVE
        assert slm.SCANNER_LIVE_MODE["accumulation"] == slm.MODE_DISCOVERY_ONLY
        assert slm.SCANNER_LIVE_MODE["hma_early_trend"] == slm.MODE_LIMITED_LIVE

    def test_the_rollout_policy_is_untouched(self):
        from config.live_rollout_config import LiveRolloutConfig

        rollout = LiveRolloutConfig.from_env()
        assert rollout.allow_extended_hours is False
        assert rollout.regular_session_only is True
        # The COUNT caps are unset by design since LIMITED_LIVE ended:
        # capacity is bounded by cash, the per-symbol lock, same-day
        # re-entry, ownership and reconciliation. The invariant this
        # test guards -- that the work in this file widened nothing --
        # is carried by the flags and the per-order quantity below.
        assert rollout.max_open_positions is None
        assert rollout.max_quantity_per_order == 1


class TestTheShellProbe:
    def test_it_prints_the_session_when_allowed(self):
        assert scan_window.probe(at(MON, 1)) == "OVERNIGHT_DAYTIME"
        assert scan_window.probe(at(MON, 10)) == "REGULAR"

    def test_it_prints_the_reason_when_refused(self):
        assert scan_window.probe(at(SAT, 10)) == scan_window.WEEKEND

    def test_the_cron_script_treats_a_reason_as_a_skip(self):
        """The script must not pass a refusal reason to --session."""
        text = (REPO_ROOT / "deploy" / "cron" / "s6_scan.sh").read_text()
        assert "scan_window.probe()" in text
        assert "PREMARKET|REGULAR|AFTER_HOURS|OVERNIGHT_DAYTIME" in text
        assert "skipped=$SESSION" in text
        # The old regular-hours gate is gone.
        assert "get_market_state() == 'CLOSED'" not in text

    def test_every_allowed_value_is_a_real_session(self):
        from scanners.base import scan_session

        for hour in (1, 8, 10, 18):
            value = scan_window.probe(at(MON, hour))
            assert value in scan_session.SESSIONS


class TestReportFieldsAreSeparate:
    """§8: `market_state=CLOSED` and `scan_allowed=False` are different
    facts and must never share a field."""

    def test_the_overnight_report_shape_is_the_documented_one(self):
        window = scan_window.evaluate(at(MON, 1))
        assert window.as_dict() == {
            "session": "OVERNIGHT_DAYTIME",
            "session_date": "2026-08-23",
            "calendar_trading_day": True,
            "scan_allowed": True,
            "reason": scan_window.VALID_TRADING_DAY,
            "regular_market_state": "CLOSED",
        }

    def test_a_refusal_carries_its_own_reason(self):
        assert scan_window.evaluate(at(SAT, 10)).as_dict()["reason"] == \
            scan_window.WEEKEND
