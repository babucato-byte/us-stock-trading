from datetime import datetime, time
from zoneinfo import ZoneInfo


def is_us_market_open():
    ny_time = datetime.now(ZoneInfo("America/New_York"))

    # 월~금만 허용
    if ny_time.weekday() >= 5:
        return False

    market_open = time(9, 30)
    market_close = time(16, 0)

    return market_open <= ny_time.time() <= market_close


if __name__ == "__main__":
    if is_us_market_open():
        print("미국장 열림")
    else:
        print("미국장 닫힘")
