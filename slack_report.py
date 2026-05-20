import os
import requests
import yfinance as yf
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

try:
    watchlist_df = pd.read_csv("watchlist.csv")
    tickers = watchlist_df["symbol"].dropna().unique().tolist()
except Exception as e:
    print(f"watchlist.csv 읽기 실패: {e}")
    tickers = []

def send_slack(message):

    response = requests.post(
        SLACK_WEBHOOK_URL,
        json={"text": message},
        timeout=10
    )

    print("Slack 전송 완료")


def analyze_stock(symbol):

    ticker = yf.Ticker(symbol)

    df = ticker.history(period="1y")

    if df.empty:
        return None

    # 200일선
    df["MA200"] = df["Close"].rolling(window=200).mean()

    # RSI
    delta = df["Close"].diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(window=14).mean()
    avg_loss = loss.rolling(window=14).mean()

    rs = avg_gain / avg_loss

    df["RSI"] = 100 - (100 / (1 + rs))

    # 거래량 평균
    df["AVG_VOLUME_20"] = df["Volume"].rolling(window=20).mean()

    current_price = df["Close"].iloc[-1]
    ma200 = df["MA200"].iloc[-1]

    rsi = df["RSI"].iloc[-1]

    today_volume = df["Volume"].iloc[-1]
    avg_volume = df["AVG_VOLUME_20"].iloc[-1]

    volume_ratio = today_volume / avg_volume

    score = 0

    # 점수 계산

    if current_price > ma200:
        score += 40

    if 40 <= rsi <= 65:
        score += 30

    if volume_ratio >= 1.5:
        score += 30

    # 타입 분류

    stock_type = "관찰"

    if score >= 70:
        stock_type = "급등 가능 후보"

    return {
        "symbol": symbol,
        "price": current_price,
        "ma200": ma200,
        "rsi": rsi,
        "volume_ratio": volume_ratio,
        "score": score,
        "type": stock_type
    }


results = []

for symbol in tickers:

    try:

        result = analyze_stock(symbol)

        if result:
            results.append(result)

    except Exception as e:

        print(symbol, e)


# 점수순 정렬
results = sorted(results, key=lambda x: x["score"], reverse=True)

# Slack 메시지 생성

message = "*미국주식 Watchlist 기반 프리마켓 기술 스캐너*\n\n"

if not tickers:
    message += "watchlist.csv에 분석할 종목이 없습니다."
    print(message)
    send_slack(message)
    exit()

for item in results:

    # 점수 70 이상만 출력
    if item["score"] < 70:
        continue

    high_score_count += 1

    message += f"""
*[{item['symbol']}]*
유형: {item['type']}

현재가: {item['price']:.2f}
200일선: {item['ma200']:.2f}

RSI: {item['rsi']:.2f}

거래량 배수: {item['volume_ratio']:.2f}배

기술점수: {item['score']}/100

------------------------
"""

if high_score_count == 0:

    message += "오늘은 조건 만족 종목이 없습니다."

print(message)

send_slack(message)
