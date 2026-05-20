import os
import requests
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

FMP_API_KEY = os.getenv("FMP_API_KEY")

tickers = ["AAPL", "MSFT", "STLA", "C", "PLTR", "GM", "F", "CVX", "PRU"]


def get_fundamental(symbol):
    url = f"https://financialmodelingprep.com/stable/key-metrics-ttm?symbol={symbol}&apikey={FMP_API_KEY}"
    
    response = requests.get(url, timeout=10)
    data = response.json()

    if not data:
        return None

    item = data[0]

    return {
        "pe": item.get("peRatioTTM"),
        "ps": item.get("priceToSalesRatioTTM"),
        "pb": item.get("pbRatioTTM"),
        "roe": item.get("roeTTM"),
    }


def get_technical(symbol):
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

    current_price = df["Close"].iloc[-1]
    ma200 = df["MA200"].iloc[-1]
    rsi = df["RSI"].iloc[-1]
    today_volume = df["Volume"].iloc[-1]
    avg_volume_20 = df["AVG_VOLUME_20"].iloc[-1]
    volume_ratio = today_volume / avg_volume_20

    return {
        "price": current_price,
        "ma200": ma200,
        "rsi": rsi,
        "volume_ratio": volume_ratio,
    }


def safe_number(value):
    if value is None:
        return None
    try:
        return float(value)
    except:
        return None


def analyze(symbol):
    fundamental = get_fundamental(symbol)
    technical = get_technical(symbol)

    if fundamental is None or technical is None:
        return None

    pe = safe_number(fundamental["pe"])
    ps = safe_number(fundamental["ps"])
    pb = safe_number(fundamental["pb"])
    roe = safe_number(fundamental["roe"])

    price = technical["price"]
    ma200 = technical["ma200"]
    rsi = technical["rsi"]
    volume_ratio = technical["volume_ratio"]

    value_score = 0
    tech_score = 0

    if pe is not None and pe <= 15 and pe > 0:
        value_score += 25

    if ps is not None and ps <= 1.5 and ps > 0:
        value_score += 25

    if pb is not None and pb <= 1.5 and pb > 0:
        value_score += 25

    if roe is not None and roe >= 0.12:
        value_score += 25

    if price > ma200:
        tech_score += 40

    if 40 <= rsi <= 65:
        tech_score += 30

    if volume_ratio >= 1.5:
        tech_score += 30

    total_score = value_score + tech_score

    stock_type = "관찰"

    if value_score >= 75 and tech_score >= 70:
        stock_type = "하이브리드 후보"
    elif value_score >= 75:
        stock_type = "안정형 가치주"
    elif tech_score >= 70:
        stock_type = "급등 가능 후보"

    return {
        "symbol": symbol,
        "pe": pe,
        "ps": ps,
        "pb": pb,
        "roe": roe,
        "price": price,
        "ma200": ma200,
        "rsi": rsi,
        "volume_ratio": volume_ratio,
        "value_score": value_score,
        "tech_score": tech_score,
        "total_score": total_score,
        "type": stock_type,
    }


results = []

for symbol in tickers:
    try:
        result = analyze(symbol)
        if result:
            results.append(result)
    except Exception as e:
        print(f"{symbol} 오류: {e}")


results = sorted(results, key=lambda x: x["total_score"], reverse=True)

print("=== 가치주 + 기술지표 스캐너 ===")

for item in results:
    print(f"""
[{item['symbol']}] {item['type']}
PER: {item['pe']}
PSR: {item['ps']}
PBR: {item['pb']}
ROE: {item['roe']}
현재가: {item['price']:.2f}
200일선: {item['ma200']:.2f}
RSI: {item['rsi']:.2f}
거래량 배수: {item['volume_ratio']:.2f}배
Value Score: {item['value_score']}/100
Tech Score: {item['tech_score']}/100
Total Score: {item['total_score']}/200
""")
