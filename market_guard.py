from datetime import datetime
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal


def is_us_trading_day(now=None):
    ny_now = now.astimezone(ZoneInfo("America/New_York")) if now is not None else datetime.now(ZoneInfo("America/New_York"))

    # 주말
    if ny_now.weekday() >= 5:
        return False

    nyse = mcal.get_calendar("NYSE")

    schedule = nyse.schedule(
        start_date=ny_now.date(),
        end_date=ny_now.date()
    )

    return not schedule.empty
