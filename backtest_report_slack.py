import os
import requests
import subprocess
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

result = subprocess.run(
    ["/home/ubuntu/trading/venv/bin/python", "/home/ubuntu/trading/backtest_multi.py"],
    capture_output=True,
    text=True,
    cwd="/home/ubuntu/trading"
)

output = result.stdout

if result.stderr:
    output += "\n\n[ERROR]\n" + result.stderr

message = f"""
*미국주식 백테스트 요약 리포트*
생성 시간: {now}

```{output[-3500:]}```

*확인 포인트*
- MDD가 너무 큰 종목은 제외 후보
- 연속 손실이 큰 전략은 실전 전 조정 필요
- CAGR은 높아도 MDD가 크면 위험
- 수익 월보다 손실 월이 많으면 전략 재검토
"""

response = requests.post(
    SLACK_WEBHOOK_URL,
    json={"text": message},
    timeout=10
)

if response.status_code == 200:
    print("Slack 백테스트 요약 리포트 발송 성공")
else:
    print("Slack 발송 실패")
    print(response.status_code)
    print(response.text)
