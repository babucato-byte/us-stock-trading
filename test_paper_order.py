import os
import requests
from dotenv import load_dotenv
from order_safety import run_order_safety_check

load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL = os.getenv("ALPACA_BASE_URL")

headers = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
    "Content-Type": "application/json"
}

symbol = "AAPL"

# 안전검사
run_order_safety_check(
    position_rate=0.01,
    today_trade_count=0,
    open_position_count=0
)

order = {
    "symbol": symbol,
    "qty": "1",
    "side": "buy",
    "type": "market",
    "time_in_force": "day"
}

url = f"{BASE_URL}/v2/orders"

response = requests.post(
    url,
    headers=headers,
    json=order,
    timeout=10
)

print("Status Code:", response.status_code)
print(response.text)
