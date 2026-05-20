import yfinance as yf

tickers = ["AAPL", "MSFT", "STLA", "C", "PLTR"]

def analyze_stock(symbol):
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="1y")

        if df.empty:
            return f"{symbol}: 데이터 없음"

        df["MA200"] = df["Close"].rolling(window=200).mean()

        delta = df["Close"].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)

        avg_gain = gain.rolling(window=14).mean()
        avg_loss = loss.rolling(window=14).mean()

        rs = avg_gain / avg_loss
        df["RSI"] = 100 - (100 / (1 + rs))

        df["AVG_VOLUME_20"] = df["Volume"].rolling(window=20).mean()

        current_price = df["Close"].iloc[-1]
        ma200 = df["MA200"].iloc[-1]
        rsi = df["RSI"].iloc[-1]
        today_volume = df["Volume"].iloc[-1]
        avg_volume_20 = df["AVG_VOLUME_20"].iloc[-1]
        volume_ratio = today_volume / avg_volume_20

        score = 0

        if current_price > ma200:
            score += 40

        if 40 <= rsi <= 65:
            score += 30

        if volume_ratio >= 1.5:
            score += 30

        return f"""
[{symbol}]
현재가: {current_price:.2f}
200일선: {ma200:.2f}
RSI: {rsi:.2f}
거래량 배수: {volume_ratio:.2f}배
기술점수: {score}/100
"""

    except Exception as e:
        return f"{symbol}: 오류 발생 - {e}"


print("=== 미국주식 기술지표 스캐너 ===")

for symbol in tickers:
    result = analyze_stock(symbol)
    print(result)
