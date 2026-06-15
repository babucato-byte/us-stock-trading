from datetime import datetime
from zoneinfo import ZoneInfo

from market_hours import AFTERMARKET, CLOSED, PREMARKET, REGULAR, get_market_state, get_market_state_info, get_us_market_session


def test_market_hours_regular_session():
    now = datetime(2026, 6, 1, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    assert get_us_market_session(now) == "regular"
    assert get_market_state(now) == REGULAR


def test_market_hours_closed_weekend():
    now = datetime(2026, 6, 6, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    assert get_us_market_session(now) == "closed"
    assert get_market_state(now) == CLOSED


def test_kst_2207_in_june_is_premarket():
    now = datetime(2026, 6, 15, 22, 7, tzinfo=ZoneInfo("Asia/Seoul"))
    info = get_market_state_info(now)

    assert info.state == PREMARKET
    assert info.label == "프리마켓"
    assert info.detail == "정규장 시작까지: 23분"


def test_market_hours_aftermarket_session():
    now = datetime(2026, 6, 15, 17, 30, tzinfo=ZoneInfo("America/New_York"))
    info = get_market_state_info(now)

    assert info.state == AFTERMARKET
    assert info.label == "애프터마켓"
    assert info.detail.startswith("다음 정규장까지:")


def test_market_hours_holiday_closed():
    now = datetime(2026, 6, 19, 10, 0, tzinfo=ZoneInfo("America/New_York"))

    assert get_market_state(now) == CLOSED
