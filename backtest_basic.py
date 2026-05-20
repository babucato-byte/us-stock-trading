import yfinance as yf

symbol = "AAPL"

df = yf.Ticker(symbol).history(period="5y")

df["MA200"] = df["Close"].rolling(window=200).mean()

delta = df["Close"].diff()
gain = delta.clip(lower=0)
loss = -delta.clip(upper=0)

avg_gain = gain.rolling(window=14).mean()
avg_loss = loss.rolling(window=14).mean()

rs = avg_gain / avg_loss
df["RSI"] = 100 - (100 / (1 + rs))

cash = 10000
position = 0
buy_price = 0
trades = []

for i in range(200, len(df)):
    price = df["Close"].iloc[i]
    ma200 = df["MA200"].iloc[i]
    rsi = df["RSI"].iloc[i]

    # 매수 조건
    if position == 0:
        if price > ma200 and 40 <= rsi <= 65:
            position = cash / price
            buy_price = price
            cash = 0
            trades.append(("BUY", df.index[i], price))

    # 매도 조건
    else:
        profit_rate = (price - buy_price) / buy_price

        if profit_rate >= 0.15 or profit_rate <= -0.08 or price < ma200:
            cash = position * price
            position = 0
            trades.append(("SELL", df.index[i], price, profit_rate))

if position > 0:
    cash = position * df["Close"].iloc[-1]

final_value = cash
return_rate = (final_value - 10000) / 10000 * 100

print(f"종목: {symbol}")
print(f"초기 자금: $10,000")
print(f"최종 자금: ${final_value:.2f}")
print(f"수익률: {return_rate:.2f}%")
print(f"거래 횟수: {len(trades)}")

print("\n거래 내역:")
for trade in trades:
    print(trade)
