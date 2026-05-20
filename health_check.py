import os
import subprocess
import pandas as pd


print("\n=== 시스템 상태 점검 ===\n")


print("1. watchlist 확인")

if os.path.exists("watchlist.csv"):
    try:
        df = pd.read_csv("watchlist.csv")
        print(f"watchlist 종목 수: {len(df)}")
    except:
        print("watchlist 읽기 실패")
else:
    print("watchlist.csv 없음")


print("\n2. order_history 확인")

if os.path.exists("order_history.csv"):
    try:
        df = pd.read_csv("order_history.csv")
        print(f"주문 기록 수: {len(df)}")
    except:
        print("order_history 읽기 실패")
else:
    print("order_history.csv 없음")


print("\n3. systemd 상태")

result = subprocess.run(
    ["systemctl", "is-active", "order-monitor"],
    capture_output=True,
    text=True
)

print("order-monitor:", result.stdout.strip())


print("\n4. 최근 로그")

try:
    result = subprocess.run(
        ["tail", "-5", "logs/premarket_cron.log"],
        capture_output=True,
        text=True
    )

    print(result.stdout)

except Exception as e:
    print(e)


print("\n=== 점검 완료 ===")
