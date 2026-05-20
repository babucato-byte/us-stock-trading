from datetime import datetime
from zoneinfo import ZoneInfo
import subprocess
import os

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")

now_kst = datetime.now(KST)
now_ny = datetime.now(NY)

is_dst = bool(now_ny.dst())

# 미국 썸머타임이면 한국시간 22시
# 일반시간이면 한국시간 23시
target_hour = 22 if is_dst else 23

log_dir = "/home/ubuntu/trading/logs"
os.makedirs(log_dir, exist_ok=True)

with open(f"{log_dir}/premarket_runner.log", "a") as f:
    f.write(
        f"{now_kst} | NY={now_ny} | DST={is_dst} | target_hour={target_hour}\n"
    )

if now_kst.hour == target_hour:
    subprocess.run(
        [
            "/home/ubuntu/trading/venv/bin/python",
            "/home/ubuntu/trading/daily_pipeline.py"
        ],
        cwd="/home/ubuntu/trading"
    )
