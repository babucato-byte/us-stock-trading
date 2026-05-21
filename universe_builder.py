import os
import requests
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL = os.getenv("ALPACA_BASE_URL")

headers = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY
}

url = f"{BASE_URL}/v2/assets"
response = requests.get(url, headers=headers, timeout=20)
response.raise_for_status()

assets = response.json()

rows = []

for asset in assets:
    if (
        asset.get("status") == "active"
        and asset.get("tradable") is True
        and asset.get("class") == "us_equity"
    ):
        rows.append({
            "symbol": asset.get("symbol"),
            "name": asset.get("name"),
            "exchange": asset.get("exchange"),
            "tradable": asset.get("tradable"),
            "shortable": asset.get("shortable")
        })

df = pd.DataFrame(rows)
df = df.dropna(subset=["symbol"])
df = df.drop_duplicates(subset=["symbol"])
df.to_csv("universe.csv", index=False)

print(f"거래 가능 종목 저장 완료: {len(df)}개")
print("파일: universe.csv")
