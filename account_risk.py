import os
import requests
from dotenv import load_dotenv
from risk_config import MAX_DAILY_LOSS_RATE

load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL = os.getenv("ALPACA_BASE_URL")

headers = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY
}


def get_account():
    url = f"{BASE_URL}/v2/account"
    response = requests.get(url, headers=headers, timeout=10)
    response.raise_for_status()
    return response.json()


def check_daily_loss_limit():
    account = get_account()

    equity = float(account["equity"])
    last_equity = float(account["last_equity"])

    if last_equity <= 0:
        return True

    daily_return = (equity - last_equity) / last_equity

    print(f"현재 계좌 평가금액: {equity}")
    print(f"전일 계좌 평가금액: {last_equity}")
    print(f"일일 손익률: {daily_return:.2%}")

    if daily_return <= MAX_DAILY_LOSS_RATE:
        raise Exception(
            f"일일 손실 제한 초과: {daily_return:.2%} <= {MAX_DAILY_LOSS_RATE:.2%}"
        )

    return True


if __name__ == "__main__":
    check_daily_loss_limit()
    print("일일 손실 제한 통과")
