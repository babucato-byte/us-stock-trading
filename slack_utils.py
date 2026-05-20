import os
import requests
from dotenv import load_dotenv

load_dotenv()

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")


def send_slack_message(message):
    if not SLACK_WEBHOOK_URL:
        print("Slack Webhook URL이 없습니다.")
        return False

    response = requests.post(
        SLACK_WEBHOOK_URL,
        json={"text": message},
        timeout=10
    )

    if response.status_code == 200:
        print("Slack 알림 전송 성공")
        return True

    print("Slack 알림 전송 실패")
    print(response.status_code)
    print(response.text)
    return False
