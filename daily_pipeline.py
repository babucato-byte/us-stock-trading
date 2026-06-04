import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from slack_utils import send_slack_message


BASE_DIR = Path(os.getenv("TRADING_BASE_DIR", Path(__file__).resolve().parent))
PYTHON = os.getenv("TRADING_PYTHON", sys.executable)
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "daily_pipeline.log"

STEPS = [
    {"name": "후보 종목 스캐너", "command": [PYTHON, str(BASE_DIR / "daily_candidate_scanner.py")]},
    {"name": "AI 후보 분석", "command": [PYTHON, str(BASE_DIR / "gpt_analysis.py")]},
    {"name": "일일 Slack 운영 리포트", "command": [PYTHON, str(BASE_DIR / "slack_report.py")]},
]


def write_log(message):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{now}] {message}"
    print(line)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def send_slack_error(message):
    send_slack_message(f"*일일 파이프라인 오류*\n```{message[:2500]}```")


def main():
    write_log("=== 일일 파이프라인 시작 ===")
    for step in STEPS:
        write_log(f"[실행] {step['name']}")
        result = subprocess.run(
            step["command"],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        if result.stdout:
            write_log(result.stdout[-3000:])
        if result.stderr:
            write_log("[오류 출력]")
            write_log(result.stderr[-3000:])
        if result.returncode != 0:
            error_message = f"{step['name']} 실패\n\n{result.stderr[-3000:]}"
            write_log(error_message)
            send_slack_error(error_message)
            raise SystemExit(1)

    write_log("=== 일일 파이프라인 완료 ===")


if __name__ == "__main__":
    main()
