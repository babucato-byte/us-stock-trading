import os
import subprocess

import pandas as pd


def print_csv_status(label, filename):
    print(f"\n{label}")
    if not os.path.exists(filename):
        print(f"- {filename} 없음")
        return

    try:
        df = pd.read_csv(filename)
        print(f"- 데이터 행 수: {len(df):,}건")
    except Exception as exc:
        print(f"- 파일 읽기 실패: {exc}")


def print_command_output(label, command):
    print(f"\n{label}")
    try:
        result = subprocess.run(command, capture_output=True, text=True)
        output = result.stdout.strip() or result.stderr.strip() or "출력 없음"
        print(output)
    except Exception as exc:
        print(f"- 명령 실행 실패: {exc}")


def main():
    print("\n=== 시스템 상태 점검 ===")

    print_csv_status("1. watchlist 확인", "watchlist.csv")
    print_csv_status("2. order_history 확인", "order_history.csv")
    print_command_output("3. order-monitor systemd 상태", ["systemctl", "is-active", "order-monitor"])
    print_command_output("4. 최근 운영 로그", ["tail", "-5", "logs/premarket_cron.log"])

    print("\n=== 점검 완료 ===")


if __name__ == "__main__":
    main()
