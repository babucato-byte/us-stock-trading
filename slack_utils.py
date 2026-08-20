import os

import requests
from dotenv import load_dotenv

load_dotenv()

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
SLACK_ALERT_WEBHOOK_URL = os.getenv("SLACK_ALERT_WEBHOOK_URL")


def _send(webhook_url, message):
    if not webhook_url:
        print("Slack Webhook URL이 설정되지 않았습니다.")
        return False

    response = requests.post(
        webhook_url,
        json={"text": message},
        timeout=10,
    )

    if response.status_code == 200:
        print("Slack 알림 전송 성공")
        return True

    print("Slack 알림 전송 실패")
    print(response.status_code)
    print(response.text)
    return False


def send_to_webhook(webhook_url, message):
    """Public name for the one outbound call.

    Callers that resolve their own webhook -- the scanner monitor reads a
    caller-supplied env mapping so it stays testable -- route through this
    rather than opening a second `requests.post`. One transport means one
    place where the timeout and the status handling live; a private copy
    elsewhere would drift from this one silently.

    It resolves no webhook of its own, deliberately. A fallback here would
    let a caller with an unset URL reroute into whichever channel this
    module happened to default to.
    """
    return _send(webhook_url, message)


def send_slack_message(message):
    return _send(SLACK_WEBHOOK_URL, message)


def send_slack_alert(message):
    return _send(SLACK_ALERT_WEBHOOK_URL, message)
