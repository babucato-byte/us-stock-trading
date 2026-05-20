import subprocess
from datetime import datetime

PYTHON = "/home/ubuntu/trading/venv/bin/python"
BASE_DIR = "/home/ubuntu/trading"

steps = [
    {
        "name": "백테스트 실행 및 watchlist 생성",
        "command": [PYTHON, f"{BASE_DIR}/backtest_multi.py"]
    },
    {
        "name": "Slack 정기 리포트 발송",
        "command": [PYTHON, f"{BASE_DIR}/slack_report.py"]
    }
]

print("=== Daily Pipeline 시작 ===")
print(datetime.now())

for step in steps:
    print(f"\n[실행] {step['name']}")

    result = subprocess.run(
        step["command"],
        cwd=BASE_DIR,
        capture_output=True,
        text=True
    )

    if result.stdout:
        print(result.stdout)

    if result.stderr:
        print("[ERROR]")
        print(result.stderr)

    if result.returncode != 0:
        print(f"[중단] {step['name']} 실패")
        exit(1)

print("\n=== Daily Pipeline 완료 ===")
print(datetime.now())
