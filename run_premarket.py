from datetime import datetime
from zoneinfo import ZoneInfo
import subprocess
import os
from market_guard import is_us_trading_day
from config.paths import get_project_root

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")

now_kst = datetime.now(KST)
now_ny = datetime.now(NY)

is_dst = bool(now_ny.dst())

# 미국 썸머타임이면 한국시간 22시
# 일반시간이면 한국시간 23시
target_hour = 22 if is_dst else 23

BASE_DIR = str(get_project_root())
log_dir = f"{BASE_DIR}/logs"
os.makedirs(log_dir, exist_ok=True)

with open(f"{log_dir}/premarket_runner.log", "a") as f:
    f.write(
        f"{now_kst} | NY={now_ny} | DST={is_dst} | target_hour={target_hour}\n"
    )

if now_kst.hour == target_hour and is_us_trading_day():
    subprocess.run(
        [
            f"{BASE_DIR}/venv/bin/python",
            f"{BASE_DIR}/daily_pipeline.py"
        ],
        cwd=BASE_DIR
    )
