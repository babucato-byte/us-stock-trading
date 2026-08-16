import os

import requests
from dotenv import load_dotenv

load_dotenv()

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
SLACK_ALERT_WEBHOOK_URL = os.getenv("SLACK_ALERT_WEBHOOK_URL")

# KIS live trading uses its OWN pair of webhooks, never these two.
#
# The two above carry Alpaca paper fills and scanner chatter -- high
# volume, low consequence. A real-money KIS order landing in that stream
# is an order nobody sees. Worse, if the KIS webhooks were ever left
# unset and the code fell back to these, a first live order would look
# like it notified correctly while going to the paper channel. So there
# is deliberately NO fallback: an unset KIS live webhook makes
# `kis_live_notifications_configured()` false, which blocks readiness at
# KIS_LIVE_NOTIFICATION_NOT_CONFIGURED rather than silently rerouting.
#
# Read from the environment per call rather than captured at import:
# the readiness checker and the bootstrap runner both set these in the
# process environment, and an import-time snapshot would answer for
# whatever the environment looked like when the first module imported.
KIS_LIVE_WEBHOOK_ENV = "KIS_LIVE_SLACK_WEBHOOK_URL"
KIS_LIVE_ALERT_WEBHOOK_ENV = "KIS_LIVE_SLACK_ALERT_WEBHOOK_URL"


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


def send_slack_message(message):
    return _send(SLACK_WEBHOOK_URL, message)


def send_slack_alert(message):
    return _send(SLACK_ALERT_WEBHOOK_URL, message)


def _kis_live_webhook(env_name):
    value = os.getenv(env_name)
    return value.strip() if value else ""


def kis_live_notifications_configured():
    """True only when BOTH KIS live webhooks are set to something non-blank.

    Both, not either: the routine and urgent streams are separate for a
    reason, and a configuration with only one of them silently drops
    half the lifecycle.
    """
    return bool(_kis_live_webhook(KIS_LIVE_WEBHOOK_ENV)
                and _kis_live_webhook(KIS_LIVE_ALERT_WEBHOOK_ENV))


def send_kis_live_message(message):
    """Routine KIS live lifecycle. Never falls back to SLACK_WEBHOOK_URL."""
    return _send(_kis_live_webhook(KIS_LIVE_WEBHOOK_ENV), message)


def send_kis_live_alert(message):
    """Urgent KIS live lifecycle. Never falls back to SLACK_ALERT_WEBHOOK_URL."""
    return _send(_kis_live_webhook(KIS_LIVE_ALERT_WEBHOOK_ENV), message)
