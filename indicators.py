import math
import os

import pandas as pd


TECHNICAL_CHECK_COLUMNS = [
    "price_above_hma200",
    "hma200_rising",
    "hma_macd_bullish",
    "macd_histogram_rising",
    "sqzmom_green",
]

DEFAULT_TECHNICAL_FILTER_WEIGHTS = {
    # Rebalanced from technical_filter_log.csv performance review:
    # trend slope, histogram acceleration, and SQZMOM had better upside/downside profiles.
    "price_above_hma200": 1,
    "hma200_rising": 2,
    "hma_macd_bullish": 1,
    "macd_histogram_rising": 2,
    "sqzmom_green": 2,
}


def env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def technical_filter_weights():
    return {
        "price_above_hma200": env_int("TECH_FILTER_WEIGHT_PRICE_ABOVE_HMA200", 1),
        "hma200_rising": env_int("TECH_FILTER_WEIGHT_HMA200_RISING", 2),
        "hma_macd_bullish": env_int("TECH_FILTER_WEIGHT_HMA_MACD_BULLISH", 1),
        "macd_histogram_rising": env_int("TECH_FILTER_WEIGHT_MACD_HISTOGRAM_RISING", 2),
        "sqzmom_green": env_int("TECH_FILTER_WEIGHT_SQZMOM_GREEN", 2),
    }


def calculate_technical_filter_score(checks, weights=None):
    weights = weights or technical_filter_weights()
    return sum(int(weights.get(name, 1)) for name, passed in checks.items() if passed)


def get_close_series(df):
    if "Close" in df.columns:
        return pd.to_numeric(df["Close"], errors="coerce")
    if "close" in df.columns:
        return pd.to_numeric(df["close"], errors="coerce")
    return pd.Series(dtype=float)


def weighted_moving_average(series, length):
    if length <= 0:
        return pd.Series(index=series.index, dtype=float)
    weights = pd.Series(range(1, length + 1), dtype=float)
    return series.rolling(length).apply(lambda values: (values * weights).sum() / weights.sum(), raw=True)


def hma(series, length):
    series = pd.to_numeric(series, errors="coerce")
    if length <= 1:
        return series.copy()
    half_length = max(1, int(length / 2))
    sqrt_length = max(1, int(math.sqrt(length)))
    raw_hma = (2 * weighted_moving_average(series, half_length)) - weighted_moving_average(series, length)
    return weighted_moving_average(raw_hma, sqrt_length)


def calculate_hma200(df, length=None):
    length = int(length or env_int("HMA_LONG_LENGTH", 200))
    close = get_close_series(df)
    return hma(close, length)


def calculate_hma_macd(df, fast=None, slow=None, signal=None):
    fast = int(fast or env_int("HMA_MACD_FAST", 12))
    slow = int(slow or env_int("HMA_MACD_SLOW", 26))
    signal = int(signal or env_int("HMA_MACD_SIGNAL", 9))
    close = get_close_series(df)

    macd_line = hma(close, fast) - hma(close, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    histogram = macd_line - signal_line
    return pd.DataFrame(
        {
            "hma_macd_line": macd_line,
            "hma_macd_signal": signal_line,
            "hma_macd_histogram": histogram,
        },
        index=df.index,
    )


def calculate_sqzmom_basic(df, length=None, bb_mult=None, kc_mult=None):
    length = int(length or env_int("SQZMOM_LENGTH", 20))
    # Multipliers are accepted for configuration compatibility with full SQZMOM.
    _ = env_float("SQZMOM_BB_MULT", 2.0) if bb_mult is None else bb_mult
    _ = env_float("SQZMOM_KC_MULT", 1.5) if kc_mult is None else kc_mult
    close = get_close_series(df)
    rolling_mean = close.rolling(length).mean()
    return close - rolling_mean


def latest_two(series):
    valid = series.dropna()
    if len(valid) < 2:
        return None, None
    return valid.iloc[-1], valid.iloc[-2]


def check_hma200_trend(df, length=None):
    hma200 = calculate_hma200(df, length=length)
    current, previous = latest_two(hma200)
    if current is None:
        return False
    return bool(current > previous)


def check_hma_macd_signal(df, fast=None, slow=None, signal=None):
    macd = calculate_hma_macd(df, fast=fast, slow=slow, signal=signal)
    line = macd["hma_macd_line"]
    signal_line = macd["hma_macd_signal"]
    latest_line, previous_line = latest_two(line)
    latest_signal, previous_signal = latest_two(signal_line)
    if latest_line is None or latest_signal is None:
        return False
    golden_cross = previous_line <= previous_signal and latest_line > latest_signal
    line_above_signal = latest_line > latest_signal
    return bool(golden_cross or line_above_signal)


def check_sqzmom_green(df, length=None):
    momentum = calculate_sqzmom_basic(df, length=length)
    current, previous = latest_two(momentum)
    if current is None:
        return False
    positive_turn = previous <= 0 < current
    increasing = current > previous
    return bool(positive_turn or increasing)


def technical_entry_filter(df, min_score=None):
    min_score = int(min_score or env_int("TECHNICAL_FILTER_MIN_SCORE", 5))
    hma_length = env_int("HMA_LONG_LENGTH", 200)
    sqzmom_length = env_int("SQZMOM_LENGTH", 20)
    minimum_rows = max(hma_length + int(math.sqrt(hma_length)), sqzmom_length + 2)

    if df is None or df.empty or len(df) < minimum_rows:
        checks = {name: False for name in TECHNICAL_CHECK_COLUMNS}
        return {"pass": False, "score": 0, "checks": checks}

    close = get_close_series(df)
    hma200 = calculate_hma200(df, length=hma_length)
    macd = calculate_hma_macd(df)
    sqzmom = calculate_sqzmom_basic(df, length=sqzmom_length)

    latest_close = close.iloc[-1] if not close.empty else None
    latest_hma200 = hma200.dropna().iloc[-1] if not hma200.dropna().empty else None
    latest_histogram, previous_histogram = latest_two(macd["hma_macd_histogram"])
    latest_momentum, previous_momentum = latest_two(sqzmom)

    checks = {
        "price_above_hma200": bool(pd.notna(latest_close) and pd.notna(latest_hma200) and latest_close > latest_hma200),
        "hma200_rising": check_hma200_trend(df, length=hma_length),
        "hma_macd_bullish": check_hma_macd_signal(df),
        "macd_histogram_rising": bool(
            latest_histogram is not None and previous_histogram is not None and latest_histogram > previous_histogram
        ),
        "sqzmom_green": bool(
            latest_momentum is not None
            and previous_momentum is not None
            and (previous_momentum <= 0 < latest_momentum or latest_momentum > previous_momentum)
        ),
    }
    score = calculate_technical_filter_score(checks)
    return {"pass": score >= min_score, "score": score, "checks": checks}
