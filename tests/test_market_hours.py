from datetime import datetime
from zoneinfo import ZoneInfo

from market_hours import get_us_market_session


def test_market_hours_regular_session():
    now = datetime(2026, 6, 1, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    assert get_us_market_session(now) == "regular"


def test_market_hours_closed_weekend():
    now = datetime(2026, 6, 6, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    assert get_us_market_session(now) == "closed"
