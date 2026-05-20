from datetime import datetime, time
from zoneinfo import ZoneInfo


def get_us_market_session():
    ny_time = datetime.now(ZoneInfo("America/New_York"))

    if ny_time.weekday() >= 5:
        return "closed"

    premarket_start = time(4, 0)
    regular_start = time(9, 30)
    regular_end = time(16, 0)
    after_end = time(20, 0)

    current_time = ny_time.time()

    if premarket_start <= current_time < regular_start:
        return "premarket"

    if regular_start <= current_time <= regular_end:
        return "regular"

    if regular_end < current_time <= after_end:
        return "aftermarket"

    return "closed"


def is_us_regular_market_open():
    return get_us_market_session() == "regular"


def is_us_premarket():
    return get_us_market_session() == "premarket"


if __name__ == "__main__":
    print(get_us_market_session())
