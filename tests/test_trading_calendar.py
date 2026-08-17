"""The calendar S1's same-day signal is computed against.

`previous_trading_day` decides which bars count as finished. Getting it
wrong does not raise -- it silently computes the strategy on a different
window than the one it was measured on -- so it is pinned here.
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import market_hours  # noqa: E402
from scanners.base import trading_calendar as cal  # noqa: E402


class TestPreviousTradingDay:
    def test_monday_looks_back_to_friday(self):
        """§8: Monday -> Friday completed bars."""
        monday = date(2026, 8, 17)
        assert monday.weekday() == 0
        assert cal.previous_trading_day(monday) == "2026-08-14"

    def test_tuesday_looks_back_to_monday(self):
        assert cal.previous_trading_day(date(2026, 8, 18)) == "2026-08-17"

    def test_sunday_and_saturday_both_reach_friday(self):
        assert cal.previous_trading_day(date(2026, 8, 16)) == "2026-08-14"
        assert cal.previous_trading_day(date(2026, 8, 15)) == "2026-08-14"

    def test_the_day_itself_is_never_returned(self):
        """Today's bar is still forming -- it is not completed history."""
        for day in ("2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20"):
            assert cal.previous_trading_day(day) < day

    def test_it_accepts_an_iso_string_like_next_trading_day_does(self):
        assert cal.previous_trading_day("2026-08-17") == "2026-08-14"

    def test_the_result_is_always_an_actual_session(self):
        cursor = date(2026, 1, 5)
        for _ in range(200):
            previous = cal.previous_trading_day(cursor)
            assert market_hours.is_market_day(date.fromisoformat(previous))
            cursor = cursor + timedelta(days=1)

    def test_nothing_is_skipped_between_the_day_and_its_answer(self):
        """The NEAREST prior session, not merely A prior session."""
        cursor = date(2026, 1, 5)
        for _ in range(200):
            previous = date.fromisoformat(cal.previous_trading_day(cursor))
            gap = cursor - timedelta(days=1)
            while gap > previous:
                assert not market_hours.is_market_day(gap), (
                    f"{gap} traded but {cursor} was sent back past it")
                gap -= timedelta(days=1)
            cursor += timedelta(days=1)

    def test_a_holiday_is_stepped_over(self):
        """§8: the day after a holiday reaches the last day that traded."""
        stepped_over = []
        cursor = date(2026, 1, 2)
        for _ in range(365):
            previous = date.fromisoformat(cal.previous_trading_day(cursor))
            skipped = (cursor - previous).days - 1
            if skipped and cursor.weekday() not in (0, 5, 6):
                stepped_over.append(cursor.isoformat())
            cursor += timedelta(days=1)
        assert stepped_over, "no mid-week holiday found in a whole year"

    def test_it_is_the_exact_mirror_of_next_trading_day(self):
        cursor = date(2026, 3, 2)
        for _ in range(120):
            if market_hours.is_market_day(cursor):
                nxt = cal.next_trading_day(cursor)
                assert cal.previous_trading_day(nxt) == cursor.isoformat()
            cursor += timedelta(days=1)

    def test_a_non_date_is_refused_rather_than_guessed(self):
        for bad in ("not-a-date", "2026-13-01", ""):
            with pytest.raises(cal.TradingCalendarError):
                cal.previous_trading_day(bad)

    def test_it_refuses_rather_than_falling_back_to_minus_one_day(self):
        """A signal computed against the wrong session is undetectable
        downstream, so the failure has to be loud."""
        import scanners.base.trading_calendar as module

        original = market_hours.is_market_day
        try:
            module.market_hours.is_market_day = lambda d: False
            with pytest.raises(cal.TradingCalendarError):
                cal.previous_trading_day(date(2026, 8, 17))
        finally:
            module.market_hours.is_market_day = original
