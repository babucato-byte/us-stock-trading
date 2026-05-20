import os
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
from order_safety import run_order_safety_check
from market_hours import is_us_market_open

load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL = os.getenv("ALPACA_BASE_URL")

headers = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
    "Content-Type": "application/json"
}


def get_account():
    url = f"{BASE_URL}/v2/account"
    response = requests.get(url, headers=headers, timeout=10)
    response.raise_for_status()
    return response.json()


def get_positions():
    url = f"{BASE_URL}/v2/positions"
    response = requests.get(url, headers=headers, timeout=10)
    response.raise_for_status()
    return response.json()


def submit_order(symbol, qty=1):
    order = {
        "symbol": symbol,
        "qty": str(qty),
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

    print(f"{symbol} 주문 결과:", response.status_code)
    print(response.text)

    return response


def analyze_stock(symbol):
    ticker = yf.Ticker(symbol)
    df = ticker.history(period="1y")

    if df.empty:
        return None

    df["MA200"] = df["Close"].rolling(window=200).mean()

    delta = df["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(window=14).mean()
    avg_loss = loss.rolling(window=14).mean()

    rs = avg_gain / avg_loss
    df["RSI"] = 100 - (100 / (1 + rs))

    df["AVG_VOLUME_20"] = df["Volume"].rolling(window=20).mean()

    price = df["Close"].iloc[-1]
    ma200 = df["MA200"].iloc[-1]
    rsi = df["RSI"].iloc[-1]
    volume_ratio = df["Volume"].iloc[-1] / df["AVG_VOLUME_20"].iloc[-1]

    score = 0

    if price > ma200:
        score += 40

    if 40 <= rsi <= 65:
        score += 30

    if volume_ratio >= 1.2:
        score += 30

    return {
        "symbol": symbol,
        "price": float(price),
        "ma200": float(ma200),
        "rsi": float(rsi),
        "volume_ratio": float(volume_ratio),
        "score": score
    }


def load_watchlist():
    try:
        watchlist_df = pd.read_csv("watchlist.csv")

        if watchlist_df.empty:
            print("watchlist.csv가 비어 있습니다.")
            return []

        return watchlist_df["symbol"].dropna().unique().tolist()

    except Exception as e:
        print(f"watchlist.csv 읽기 실패: {e}")
        return []


def load_order_history():
    try:
        return pd.read_csv("order_history.csv")
    except Exception:
        return pd.DataFrame(columns=["symbol", "order_date"])


def save_order_history(order_history):
    order_history.to_csv("order_history.csv", index=False)


def main():
    if not is_us_market_open():
        print("미국장이 닫혀 있어 주문을 중단합니다.")
        return

    tickers = load_watchlist()

    if not tickers:
        print("주문 대상 종목이 없습니다.")
        return

    account = get_account()
    positions = get_positions()

    open_position_count = len(positions)
    today_trade_count = 0
    today = datetime.now().strftime("%Y-%m-%d")

    held_symbols = [p["symbol"] for p in positions]

    order_history = load_order_history()

    print("Paper 계좌 연결 성공")
    print("현재 보유 종목 수:", open_position_count)
    print("현재 보유 종목:", held_symbols)

    for symbol in tickers:
        if symbol in held_symbols:
            print(f"{symbol} 이미 보유 중 → 주문 건너뜀")
            continue

        already_ordered = (
            (order_history["symbol"] == symbol)
            & (order_history["order_date"] == today)
        ).any()

        if already_ordered:
            print(f"{symbol} 오늘 이미 주문됨 → 건너뜀")
            continue

        result = analyze_stock(symbol)

        if not result:
            print(f"{symbol} 분석 실패")
            continue

        print(result)

        if result["score"] >= 70:
            print(f"{symbol} 전략 조건 충족")

            run_order_safety_check(
                position_rate=0.01,
                today_trade_count=today_trade_count,
                open_position_count=open_position_count
            )

            response = submit_order(symbol, qty=1)

            if response.status_code in [200, 201]:
                new_row = pd.DataFrame([
                    {
                        "symbol": symbol,
                        "order_date": today
                    }
                ])

                order_history = pd.concat(
                    [order_history, new_row],
                    ignore_index=True
                )

                save_order_history(order_history)

                today_trade_count += 1
                open_position_count += 1

        else:
            print(f"{symbol} 조건 미충족")


if __name__ == "__main__":
    main()
