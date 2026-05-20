import os
import requests
from dotenv import load_dotenv

load_dotenv()

WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

payload = {
    "text": "✅ Oracle 서버 Slack 테스트 성공"
}

requests.post(WEBHOOK_URL, json=payload)
