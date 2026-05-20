import yfinance as yf
import pandas as pd

ticker = yf.Ticker("AAPL")

df = ticker.history(period="1y")

# 200일 이동평균 계산
df["MA200"] = df["Close"].rolling(window=200).mean()

# 현재 가격
current_price = df["Close"].iloc[-1]

# 현재 200일선
ma200 = df["MA200"].iloc[-1]

print(f"현재 가격: {current_price:.2f}")
print(f"200일선: {ma200:.2f}")

if current_price > ma200:
    print("현재 가격이 200일선 위에 있습니다.")
else:
    print("현재 가격이 200일선 아래에 있습니다.")
