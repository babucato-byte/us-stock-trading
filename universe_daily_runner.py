from datetime import datetime
from zoneinfo import ZoneInfo
import subprocess
import os

from config.paths import get_project_root

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")

BASE_DIR = str(get_project_root())
PYTHON = f"{BASE_DIR}/venv/bin/python"
LOG_DIR = f"{BASE_DIR}/logs"

os.makedirs(LOG_DIR, exist_ok=True)

now_kst = datetime.now(KST)
now_ny = datetime.now(NY)

with open(f"{LOG_DIR}/universe_daily_runner.log", "a") as f:
    f.write(f"{now_kst} | NY={now_ny} | universe refresh\n")

subprocess.run(
    [PYTHON, f"{BASE_DIR}/universe_builder.py"],
    cwd=BASE_DIR
)
