import math


def safe_float(value):
    try:
        number = float(value)
    except Exception:
        return None
    return None if math.isnan(number) else number


def clamp_score(value):
    return int(max(0, min(100, round(value))))


def calculate_trend(df, price=None):
    frame = df.copy()
    frame["MA20"] = frame["Close"].rolling(window=20).mean()
    frame["MA50"] = frame["Close"].rolling(window=50).mean()
    frame["MA200"] = frame["Close"].rolling(window=200).mean()

    price = safe_float(price if price is not None else frame["Close"].iloc[-1])
    ma20 = safe_float(frame["MA20"].iloc[-1])
    ma50 = safe_float(frame["MA50"].iloc[-1])
    ma200 = safe_float(frame["MA200"].iloc[-1])

    trend = classify_trend(price, ma20, ma50, ma200)
    return {
        "ma20": ma20,
        "ma50": ma50,
        "ma200": ma200,
        "trend": trend,
        "trend_score": score_trend(trend, price, ma50),
    }


def classify_trend(price, ma20, ma50, ma200):
    if None in (price, ma20, ma50, ma200):
        return "Unknown"
    if price > ma20 > ma50 > ma200:
        return "Strong Uptrend"
    if price < ma20 < ma50 < ma200:
        return "Strong Downtrend"
    if price > ma50 > ma200:
        return "Uptrend"
    if price < ma50 < ma200:
        return "Downtrend"
    if ma50 and abs(price - ma50) / ma50 <= 0.03:
        return "Sideways"
    return "Sideways"


def score_trend(trend, price, ma50):
    base_scores = {
        "Strong Uptrend": 90,
        "Uptrend": 75,
        "Sideways": 50,
        "Downtrend": 25,
        "Strong Downtrend": 10,
        "Unknown": 0,
    }
    score = base_scores.get(trend, 0)
    if ma50 and price:
        distance = ((price - ma50) / ma50) * 100
        if trend in {"Strong Uptrend", "Uptrend"}:
            score += min(10, max(0, distance))
        elif trend in {"Downtrend", "Strong Downtrend"}:
            score -= min(10, max(0, -distance))
    return clamp_score(score)
