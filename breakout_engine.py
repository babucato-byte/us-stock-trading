import math


def safe_float(value):
    try:
        number = float(value)
    except Exception:
        return None
    return None if math.isnan(number) else number


def calculate_breakout(df, price=None):
    if df.empty:
        return {
            "breakout_flag": False,
            "breakout_score": 0,
            "breakout_20d": False,
            "breakout_50d": False,
            "near_52w_high": False,
        }

    price = safe_float(price if price is not None else df["Close"].iloc[-1])
    prior_highs = df["High"].iloc[:-1] if len(df) > 1 else df["High"]
    high_20 = safe_float(prior_highs.tail(20).max()) if len(prior_highs) >= 20 else None
    high_50 = safe_float(prior_highs.tail(50).max()) if len(prior_highs) >= 50 else None
    high_52w = safe_float(prior_highs.tail(252).max()) if len(prior_highs) >= 50 else None

    breakout_20d = bool(price is not None and high_20 is not None and price > high_20)
    breakout_50d = bool(price is not None and high_50 is not None and price > high_50)
    near_52w_high = bool(
        price is not None
        and high_52w is not None
        and high_52w > 0
        and price >= high_52w * 0.97
    )

    score = 0
    if breakout_20d:
        score += 35
    if breakout_50d:
        score += 35
    if near_52w_high:
        score += 30

    return {
        "breakout_flag": breakout_20d or breakout_50d,
        "breakout_score": int(max(0, min(100, score))),
        "breakout_20d": breakout_20d,
        "breakout_50d": breakout_50d,
        "near_52w_high": near_52w_high,
    }
