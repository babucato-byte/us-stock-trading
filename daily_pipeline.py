import os
import requests
import subprocess
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

PYTHON = "/home/ubuntu/trading/venv/bin/python"
BASE_DIR = "/home/ubuntu/trading"
LOG_DIR = f"{BASE_DIR}/logs"

os.makedirs(LOG_DIR, exist_ok=True)

log_file = f"{LOG_DIR}/daily_pipeline.log"

steps = [
    {
        "name": "거래 가능 종목 universe 생성",
        "command": [PYTHON, f"{BASE_DIR}/universe_builder.py"]
    },
    {
        "name": "실제 조건 기반 후보 스캔",
        "command": [PYTHON, f"{BASE_DIR}/daily_candidate_scanner.py"]
    },
    {
        "name": "백테스트 실행 및 watchlist 생성",
        "command": [PYTHON, f"{BASE_DIR}/backtest_multi.py"]
    },
    {
        "name": "Slack 정기 리포트 발송",
        "command": [PYTHON, f"{BASE_DIR}/slack_report.py"]
    }
]

def write_log(message):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{now}] {message}"
    print(line)

    with open(log_file, "a") as f:
        f.write(line + "\n")


def send_slack_error(message):
    if not SLACK_WEBHOOK_URL:
        write_log("SLACK_WEBHOOK_URL 없음")
        return

    payload = {
        "text": f"""
*자동매매 파이프라인 오류 발생*

```{message}```
"""
    }

    requests.post(
        SLACK_WEBHOOK_URL,
        json=payload,
        timeout=10
    )


write_log("=== Daily Pipeline 시작 ===")

for step in steps:
    write_log(f"[실행] {step['name']}")

    result = subprocess.run(
        step["command"],
        cwd=BASE_DIR,
        capture_output=True,
        text=True
    )

    if result.stdout:
        write_log(result.stdout[-3000:])

    if result.stderr:
        write_log("[STDERR]")
        write_log(result.stderr[-3000:])

    if result.returncode != 0:
        error_message = f"{step['name']} 실패\n\n{result.stderr[-3000:]}"
        write_log(error_message)
        send_slack_error(error_message)
        exit(1)

write_log("=== Daily Pipeline 완료 ===")
