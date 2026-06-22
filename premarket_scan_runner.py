from datetime import datetime
from zoneinfo import ZoneInfo
import subprocess
import os

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")

BASE_DIR = "/home/ubuntu/trading"
PYTHON = f"{BASE_DIR}/venv/bin/python"
LOG_DIR = f"{BASE_DIR}/logs"

os.makedirs(LOG_DIR, exist_ok=True)

now_kst = datetime.now(KST)
now_ny = datetime.now(NY)

ny_hour = now_ny.hour
ny_minute = now_ny.minute
weekday = now_ny.weekday()

is_weekday = weekday < 5
from market_guard import is_us_trading_day

# 미국 프리마켓 04:00~09:30
is_premarket = (
    is_weekday
    and (
        (ny_hour > 4 or (ny_hour == 4 and ny_minute >= 0))
        and (ny_hour < 9 or (ny_hour == 9 and ny_minute < 30))
    )
)

with open(f"{LOG_DIR}/premarket_scan_runner.log", "a") as f:
    f.write(f"{now_kst} | NY={now_ny} | premarket={is_premarket}\n")

if is_premarket and is_us_trading_day():
    subprocess.run(
        [PYTHON, f"{BASE_DIR}/daily_candidate_scanner.py"],
        cwd=BASE_DIR
    )

