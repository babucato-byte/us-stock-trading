import yfinance as yf

ticker = yf.Ticker("AAPL")

df = ticker.history(period="1y")

# 200일선
df["MA200"] = df["Close"].rolling(window=200).mean()

# RSI 계산
delta = df["Close"].diff()
gain = delta.clip(lower=0)
loss = -delta.clip(upper=0)

avg_gain = gain.rolling(window=14).mean()
avg_loss = loss.rolling(window=14).mean()

rs = avg_gain / avg_loss
df["RSI"] = 100 - (100 / (1 + rs))

# 거래량 증가율
df["AVG_VOLUME_20"] = df["Volume"].rolling(window=20).mean()

current_price = df["Close"].iloc[-1]
ma200 = df["MA200"].iloc[-1]
rsi = df["RSI"].iloc[-1]
today_volume = df["Volume"].iloc[-1]
avg_volume_20 = df["AVG_VOLUME_20"].iloc[-1]
volume_ratio = today_volume / avg_volume_20

print("=== AAPL 지표 분석 ===")
print(f"현재 가격: {current_price:.2f}")
print(f"200일선: {ma200:.2f}")
print(f"RSI: {rsi:.2f}")
print(f"오늘 거래량: {today_volume:,.0f}")
print(f"20일 평균 거래량: {avg_volume_20:,.0f}")
print(f"거래량 배수: {volume_ratio:.2f}배")

print("\n=== 판단 ===")

if current_price > ma200:
    print("✅ 현재 가격이 200일선 위에 있습니다.")
else:
    print("❌ 현재 가격이 200일선 아래에 있습니다.")

if 40 <= rsi <= 65:
    print("✅ RSI가 적정 구간입니다.")
elif rsi > 70:
    print("⚠️ RSI가 과열 구간입니다.")
else:
    print("⚠️ RSI가 약한 구간입니다.")

if volume_ratio >= 1.5:
    print("✅ 거래량이 평균보다 크게 증가했습니다.")
else:
    print("⚠️ 거래량 증가가 강하지 않습니다.")
