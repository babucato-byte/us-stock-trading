import os
import requests
from dotenv import load_dotenv

load_dotenv()

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
SLACK_ALERT_WEBHOOK_URL = os.getenv("SLACK_ALERT_WEBHOOK_URL")


def _send(webhook_url, message):
    if not webhook_url:
        print("Slack Webhook URL이 없습니다.")
        return False

    response = requests.post(
        webhook_url,
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


def send_slack_message(message):
    return _send(SLACK_WEBHOOK_URL, message)


def send_slack_alert(message):
    return _send(SLACK_ALERT_WEBHOOK_URL, message)
