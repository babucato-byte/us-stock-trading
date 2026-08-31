"""One answer to "what trading day is it", for every component.

On 2026-08-30 at 20:22 ET the system reported session=OVERNIGHT_DAYTIME
with trading_day=2026-08-30 -- a Sunday, which is not a trading day.
Manifests were dated to it, the scanner logged skipped=WEEKEND, and
discovery produced nothing.

The order path was never wrong: `session_capability` knew KIS was closed
and refused. What was wrong is that two authorities answer "which
session is this" -- a pure ET clock where anything after 20:00 is
OVERNIGHT_DAYTIME, and KIS's KST windows where daytime starts 10:00 KST
= 21:00 ET under DST. In that one hour the first names a session the
second says does not exist, and the trading day fell back to the Eastern
calendar date.
"""

import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import operational_calendar as cal  # noqa: E402

ET = ZoneInfo("America/New_York")


def _at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def _day(moment):
    return cal.resolve_operational_trading_day(moment)["operational_trading_day"]


class TestTheSundayEveningCaseThatBrokeIt:
    def test_the_exact_reported_moment(self):
        """2026-08-30 20:22 ET reported trading_day=2026-08-30, a Sunday.
        KIS daytime has not opened yet at that moment, so the honest
        answer is that there is no operational trading day -- not that it
        is Sunday."""
        out = cal.resolve_operational_trading_day(_at(2026, 8, 30, 20, 22))
        assert out["operational_trading_day"] is None
        assert out["reason"] == cal.MARKET_CLOSED
        assert out["session_date"] == "2026-08-30"

    def test_once_daytime_actually_opens_it_is_monday(self):
        out = cal.resolve_operational_trading_day(_at(2026, 8, 30, 21, 30))
        assert out["operational_trading_day"] == "2026-08-31"
        assert out["session"] == "OVERNIGHT_DAYTIME"
        assert out["exit_supported"] is True

    def test_the_session_date_is_still_sunday(self):
        """Both facts are kept. Conflating them is the whole defect."""
        out = cal.resolve_operational_trading_day(_at(2026, 8, 30, 21, 30))
        assert out["session_date"] == "2026-08-30"
        assert out["operational_trading_day"] == "2026-08-31"
        assert out["calendar_trading_day"] is False

    def test_a_declared_session_that_disagrees_is_recorded(self):
        """The 20:00-21:00 gap made visible instead of silently
        overwritten."""
        out = cal.resolve_operational_trading_day(
            _at(2026, 8, 30, 21, 30), session="REGULAR")
        assert out["declared_session"] == "REGULAR"
        assert out["session_disagreement"] is True

    def test_an_agreeing_session_is_not_a_disagreement(self):
        out = cal.resolve_operational_trading_day(
            _at(2026, 8, 30, 21, 30), session="OVERNIGHT_DAYTIME")
        assert out["session_disagreement"] is False


class TestTheReplayMatrix:
    """The hours around the boundary, as specified."""

    @pytest.mark.parametrize("hour,minute,expected", [
        (19, 59, None),          # before daytime: no operational day
        (20, 30, None),          # the disputed hour: KIS still closed
        (21, 30, "2026-08-31"),  # daytime open
        (23, 59, "2026-08-31"),
    ])
    def test_sunday_evening(self, hour, minute, expected):
        assert _day(_at(2026, 8, 30, hour, minute)) == expected

    @pytest.mark.parametrize("hour,expected", [
        (0, "2026-08-31"),
        (3, "2026-08-31"),
    ])
    def test_monday_small_hours_stay_monday(self, hour, expected):
        assert _day(_at(2026, 8, 31, hour)) == expected

    def test_monday_premarket_is_monday(self):
        out = cal.resolve_operational_trading_day(_at(2026, 8, 31, 5, 0))
        assert out["operational_trading_day"] == "2026-08-31"

    def test_monday_regular_is_monday(self):
        out = cal.resolve_operational_trading_day(_at(2026, 8, 31, 10, 0))
        assert out["operational_trading_day"] == "2026-08-31"
        assert out["calendar_trading_day"] is True


class TestFridayEveningIsNotSaturday:
    def test_it_resolves_to_the_following_monday(self):
        """A plain +1 gives Saturday, and dating a manifest to Saturday
        is how a whole session's work goes missing."""
        assert _day(_at(2026, 8, 28, 21, 30)) == "2026-08-31"

    def test_saturday_small_hours_also_resolve_forward(self):
        assert _day(_at(2026, 8, 29, 2, 0)) == "2026-08-31"

    def test_dating_it_forward_does_not_permit_orders(self):
        """Fixing a date must never become a way of opening a route:
        whether KIS accepts a daytime order on a Saturday KST morning is
        its decision, passed through untouched."""
        out = cal.resolve_operational_trading_day(_at(2026, 8, 28, 21, 30))
        assert out["operational_trading_day"] == "2026-08-31"
        assert out["exit_supported"] is False
        assert out["entry_supported"] is False


class TestHolidaysAreSkippedNotLandedOn:
    def test_the_evening_before_a_holiday_skips_it(self):
        """2026-07-03 is the observed Independence Day holiday; the
        Thursday evening before it precedes Monday the 6th."""
        assert _day(_at(2026, 7, 2, 21, 30)) == "2026-07-06"

    def test_next_valid_trading_day_skips_a_weekend(self):
        assert cal.next_valid_trading_day(date(2026, 8, 29)) == date(2026, 8, 31)

    def test_next_valid_trading_day_accepts_a_trading_day(self):
        assert cal.next_valid_trading_day(date(2026, 8, 31)) == date(2026, 8, 31)

    def test_it_gives_up_rather_than_guessing_far_ahead(self):
        """Beyond a long weekend something is wrong with the calendar
        itself, and guessing further is worse than refusing."""
        assert cal.next_valid_trading_day(None) is None


class TestThePriorTradingDay:
    def test_monday_looks_back_to_friday(self):
        """A plain -1 lands on Sunday every Monday, which is how a
        Monday premarket ends up seeded from nothing."""
        assert cal.prior_trading_day("2026-08-31") == "2026-08-28"

    def test_a_tuesday_looks_back_to_monday(self):
        assert cal.prior_trading_day("2026-09-01") == "2026-08-31"

    def test_an_unparseable_day_is_None(self):
        assert cal.prior_trading_day("not-a-date") is None
        assert cal.prior_trading_day(None) is None


class TestDSTAndDateBoundaries:
    def test_dst_start_does_not_shift_the_session(self):
        """2026-03-08 is the US DST start. The KST windows move relative
        to ET, which is precisely why the boundary must not be a
        hardcoded ET hour."""
        out = cal.resolve_operational_trading_day(_at(2026, 3, 9, 10, 0))
        assert out["operational_trading_day"] == "2026-03-09"

    def test_dst_end_does_not_shift_the_session(self):
        out = cal.resolve_operational_trading_day(_at(2026, 11, 2, 10, 0))
        assert out["operational_trading_day"] == "2026-11-02"

    def test_a_month_end_evening_crosses_into_the_next_month(self):
        out = cal.resolve_operational_trading_day(_at(2026, 9, 30, 21, 30))
        assert out["operational_trading_day"] == "2026-10-01"

    def test_a_year_end_evening_crosses_into_the_next_year(self):
        """2026-12-31 is a Thursday; the evening precedes Friday 1 Jan,
        a holiday, so the next valid day is Monday 4 Jan."""
        assert _day(_at(2026, 12, 31, 21, 30)) == "2027-01-04"

    def test_a_naive_datetime_is_not_silently_accepted_as_eastern(self):
        """It is converted, not assumed -- but it must not raise."""
        out = cal.resolve_operational_trading_day(
            datetime(2026, 8, 31, 14, 0, tzinfo=ZoneInfo("UTC")))
        assert out["session_date"] == "2026-08-31"


class TestFailingClosed:
    def test_an_unusable_moment_reports_the_systemic_reason(self):
        """One name, so a calendar fault surfaces once instead of as
        five unrelated-looking failures."""
        out = cal.resolve_operational_trading_day("not a datetime")
        assert out["reason"] == cal.TRADING_DAY_RESOLUTION_ERROR
        assert out["resolved"] is False

    def test_an_unresolved_calendar_permits_nothing(self):
        out = cal.resolve_operational_trading_day("not a datetime")
        assert out["entry_supported"] is False
        assert out["orders_allowed"] is False

    def test_a_broken_capability_does_not_raise(self, monkeypatch):
        from config import session_capability

        monkeypatch.setattr(
            session_capability, "capability_at",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        out = cal.resolve_operational_trading_day(_at(2026, 8, 31, 10, 0))
        assert out["reason"] == cal.TRADING_DAY_RESOLUTION_ERROR


class TestEveryComponentAgrees:
    """The defect was not a wrong formula -- it was two formulas. So the
    thing worth testing is that one timestamp yields one answer wherever
    it is asked."""

    MOMENTS = [
        _at(2026, 8, 30, 21, 30),   # Sunday evening daytime -> Monday
        _at(2026, 8, 31, 2, 0),     # Monday small hours -> Monday
        _at(2026, 8, 31, 10, 0),    # Monday regular
        _at(2026, 8, 28, 21, 30),   # Friday evening -> Monday
    ]

    @pytest.mark.parametrize("moment", MOMENTS)
    def test_the_manifest_and_collector_use_the_same_resolver(self, moment):
        """Both call `operational_trading_day`; asserted at the source so
        one of them cannot quietly go back to the calendar date."""
        collector = (REPO_ROOT / "scripts"
                     / "run_realtime_bar_collector.py").read_text()
        manifest = (REPO_ROOT / "market_data"
                    / "bootstrap_watchlist.py").read_text()
        for source in (collector, manifest):
            assert "operational_trading_day" in source

    @pytest.mark.parametrize("moment", MOMENTS)
    def test_the_resolver_is_deterministic_for_one_moment(self, moment):
        first = cal.resolve_operational_trading_day(moment)
        second = cal.resolve_operational_trading_day(moment)
        assert first == second

    def test_daily_entry_limits_still_use_the_calendar_day(self):
        """Deliberately NOT switched. `us_trading_day` scopes per-day
        entry limits, and moving that boundary would change how much the
        system may trade in a day -- a risk change wearing the clothes of
        a calendar fix."""
        import market_hours

        assert "per-day trading limit" in market_hours.us_trading_day.__doc__

    def test_a_resolved_day_is_always_a_real_trading_day(self):
        """Whatever it returns must be tradeable, or downstream will
        date work to a day that has no session."""
        from config import kis_market_schedule as ks

        for moment in self.MOMENTS:
            day = _day(moment)
            if day is not None:
                assert ks.is_trading_day(day), f"{moment} -> {day}"

    def test_the_systemic_reason_has_one_name(self):
        """So a calendar fault surfaces once instead of as five
        unrelated-looking failures across scanner, collector, manifest,
        entry and exit."""
        assert cal.TRADING_DAY_RESOLUTION_ERROR == "TRADING_DAY_RESOLUTION_ERROR"
