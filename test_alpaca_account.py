import os
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL = os.getenv("ALPACA_BASE_URL")

headers = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY
}

url = f"{BASE_URL}/v2/account"

response = requests.get(
    url,
    headers=headers,
    timeout=10
)

print("Status Code:", response.status_code)
print(response.text)
