import os
import requests
import subprocess
from dotenv import load_dotenv

load_dotenv()

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

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
*미국주식 백테스트 리포트*

```{output[-3500:]}```
"""

response = requests.post(
    SLACK_WEBHOOK_URL,
    json={"text": message},
    timeout=10
)

if response.status_code == 200:
    print("Slack 백테스트 리포트 발송 성공")
else:
    print("Slack 발송 실패")
    print(response.status_code)
    print(response.text)
