from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")
MARKET_PREMARKET_START = time(4, 0)
MARKET_REGULAR_START = time(9, 30)
MARKET_REGULAR_END = time(16, 0)
MARKET_AFTERMARKET_END = time(20, 0)

PREMARKET = "PREMARKET"
REGULAR = "REGULAR"
AFTERMARKET = "AFTERMARKET"
CLOSED = "CLOSED"


@dataclass(frozen=True)
class MarketStateInfo:
    state: str
    label: str
    detail: str
    eastern_time: datetime


def eastern_now(now=None):
    if now is None:
        return datetime.now(EASTERN)
    if now.tzinfo is None:
        return now.replace(tzinfo=EASTERN)
    return now.astimezone(EASTERN)


def observed_date(year, month, day):
    actual = date(year, month, day)
    if actual.weekday() == 5:
        return actual - timedelta(days=1)
    if actual.weekday() == 6:
        return actual + timedelta(days=1)
    return actual


def nth_weekday(year, month, weekday, n):
    current = date(year, month, 1)
    while current.weekday() != weekday:
        current += timedelta(days=1)
    return current + timedelta(days=7 * (n - 1))


def last_weekday(year, month, weekday):
    if month == 12:
        current = date(year, 12, 31)
    else:
        current = date(year, month + 1, 1) - timedelta(days=1)
    while current.weekday() != weekday:
        current -= timedelta(days=1)
    return current


def easter_date(year):
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def us_market_holidays(year):
    holidays = {
        observed_date(year, 1, 1),
        nth_weekday(year, 1, 0, 3),
        nth_weekday(year, 2, 0, 3),
        easter_date(year) - timedelta(days=2),
        last_weekday(year, 5, 0),
        observed_date(year, 6, 19),
        observed_date(year, 7, 4),
        nth_weekday(year, 9, 0, 1),
        nth_weekday(year, 11, 3, 4),
        observed_date(year, 12, 25),
    }
    return holidays


def is_market_holiday(day):
    return day in us_market_holidays(day.year)


def is_market_day(day):
    return day.weekday() < 5 and not is_market_holiday(day)


def combine_eastern(day, clock):
    return datetime.combine(day, clock, tzinfo=EASTERN)


def next_regular_open(ny_time):
    day = ny_time.date()
    today_open = combine_eastern(day, MARKET_REGULAR_START)
    if is_market_day(day) and ny_time < today_open:
        return today_open

    day += timedelta(days=1)
    while not is_market_day(day):
        day += timedelta(days=1)
    return combine_eastern(day, MARKET_REGULAR_START)


def format_duration(delta):
    total_minutes = max(0, int(delta.total_seconds() // 60))
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        return f"{hours}시간 {minutes:02d}분"
    return f"{minutes}분"


def get_market_state(now=None):
    ny_time = eastern_now(now)
    if not is_market_day(ny_time.date()):
        return CLOSED

    current_time = ny_time.time()
    if MARKET_PREMARKET_START <= current_time < MARKET_REGULAR_START:
        return PREMARKET
    if MARKET_REGULAR_START <= current_time < MARKET_REGULAR_END:
        return REGULAR
    if MARKET_REGULAR_END <= current_time < MARKET_AFTERMARKET_END:
        return AFTERMARKET
    return CLOSED


def get_market_state_info(now=None):
    ny_time = eastern_now(now)
    state = get_market_state(ny_time)

    if state == PREMARKET:
        regular_start = combine_eastern(ny_time.date(), MARKET_REGULAR_START)
        return MarketStateInfo(state, "프리마켓", f"정규장 시작까지: {format_duration(regular_start - ny_time)}", ny_time)

    if state == REGULAR:
        regular_end = combine_eastern(ny_time.date(), MARKET_REGULAR_END)
        return MarketStateInfo(state, "정규장", f"장 종료까지: {format_duration(regular_end - ny_time)}", ny_time)

    if state == AFTERMARKET:
        next_open = next_regular_open(ny_time)
        return MarketStateInfo(state, "애프터마켓", f"다음 정규장까지: {format_duration(next_open - ny_time)}", ny_time)

    return MarketStateInfo(state, "장 마감", "", ny_time)


def get_us_market_session(now=None):
    state = get_market_state(now)
    return {
        PREMARKET: "premarket",
        REGULAR: "regular",
        AFTERMARKET: "aftermarket",
        CLOSED: "closed",
    }[state]



def is_us_regular_market_open(now=None):
    return get_us_market_session(now) == "regular"


def is_us_premarket(now=None):
    return get_us_market_session(now) == "premarket"


if __name__ == "__main__":
    print(get_market_state_info())
