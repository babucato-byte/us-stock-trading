import pandas as pd
import yfinance as yf
from datetime import datetime
from slack_utils import send_slack_alert

MIN_PRICE = 5
MIN_AVG_DOLLAR_VOLUME = 20_000_000
SCAN_LIMIT = 800

RSI_MIN = 40
RSI_MAX = 65
VOLUME_RATIO_MIN = 1.2
MIN_SCORE = 70


def calculate_rsi(df, period=14):
    delta = df["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def analyze(symbol):
    try:
        df = yf.Ticker(symbol).history(period="1y")

        if df.empty or len(df) < 220:
            return None

        df["MA200"] = df["Close"].rolling(window=200).mean()
        df["RSI"] = calculate_rsi(df)
        df["AVG_VOLUME_20"] = df["Volume"].rolling(window=20).mean()

        price = df["Close"].iloc[-1]
        ma200 = df["MA200"].iloc[-1]
        rsi = df["RSI"].iloc[-1]
        volume = df["Volume"].iloc[-1]
        avg_volume_20 = df["AVG_VOLUME_20"].iloc[-1]
        volume_ratio = volume / avg_volume_20
        avg_dollar_volume = price * avg_volume_20

        if price < MIN_PRICE:
            return None

        if avg_dollar_volume < MIN_AVG_DOLLAR_VOLUME:
            return None

        score = 0

        if price > ma200:
            score += 40

        if RSI_MIN <= rsi <= RSI_MAX:
            score += 30

        if volume_ratio >= VOLUME_RATIO_MIN:
            score += 30

        smart_money_score = 0

        if volume_ratio >= 2:
            smart_money_score += 30

        if volume_ratio >= 3:
            smart_money_score += 20

        if price > ma200:
            smart_money_score += 20

        if RSI_MIN <= rsi <= RSI_MAX:
            smart_money_score += 20

        if score < MIN_SCORE:
            return None

        if smart_money_score >= 50:
            stock_type = "수급/세력 가능 후보"
        elif score >= 70:
            stock_type = "기술 조건 후보"
        else:
            stock_type = "관찰"

        return {
            "symbol": symbol,
            "price": round(float(price), 2),
            "ma200": round(float(ma200), 2),
            "rsi": round(float(rsi), 2),
            "volume_ratio": round(float(volume_ratio), 2),
            "avg_dollar_volume": round(float(avg_dollar_volume), 0),
            "score": score,
            "smart_money_score": smart_money_score,
            "type": stock_type,
            "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

    except Exception:
        return None


universe = pd.read_csv("universe.csv")
symbols = universe["symbol"].dropna().unique().tolist()

results = []

print(f"전체 대상 종목: {len(symbols)}개")
print(f"이번 스캔 제한: {SCAN_LIMIT}개")

for idx, symbol in enumerate(symbols[:SCAN_LIMIT], start=1):
    print(f"[{idx}/{min(len(symbols), SCAN_LIMIT)}] {symbol} 분석 중")

    result = analyze(symbol)

    if result:
        results.append(result)

df = pd.DataFrame(results)

if df.empty:

    print("조건 만족 후보 없음")

    df.to_csv("candidates.csv", index=False)

    send_slack_alert(
        "실시간 후보 탐지 결과: 조건 만족 후보 없음"
    )

else:

    df = df.sort_values(
        by=["score", "smart_money_score", "volume_ratio"],
        ascending=False
    )

    df.to_csv("candidates.csv", index=False)

    print("\n=== 조건 만족 후보 ===")

    print(
        df[
            [
                "symbol",
                "type",
                "price",
                "rsi",
                "volume_ratio",
                "score",
                "smart_money_score"
            ]
        ]
    )

    print(f"\n후보 저장 완료: {len(df)}개 → candidates.csv")

    top_candidates = df.head(5)

    message_lines = []

    message_lines.append(
        "*실시간 후보 탐지 결과*"
    )

    message_lines.append("")

    for _, row in top_candidates.iterrows():

        line = (
            f"{row['symbol']} | "
            f"{row['type']} | "
            f"RSI={row['rsi']} | "
            f"VOL={row['volume_ratio']}x | "
            f"SCORE={row['score']} | "
            f"SMART={row['smart_money_score']}"
        )

        message_lines.append(line)

    message_lines.append("")

    message_lines.append(
        f"전체 후보 수: {len(df)}개"
    )

    message = "\n".join(message_lines)

    print("\nSlack 전송 시도")

    try:

        send_slack_alert(message)

        print("Slack 전송 완료")

    except Exception as e:

        print("Slack 오류 발생")
        print(e)
