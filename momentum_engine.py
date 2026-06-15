import math


def safe_float(value, default=0.0):
    try:
        number = float(value)
    except Exception:
        return default
    return default if math.isnan(number) else number


def clamp_score(value):
    return int(max(0, min(100, round(value))))


def calculate_momentum_score(rsi, volume_ratio, dollar_volume=0, relative_volume=None):
    rsi = safe_float(rsi)
    volume_ratio = safe_float(volume_ratio)
    relative_volume = safe_float(relative_volume, volume_ratio if relative_volume is None else 0.0)
    dollar_volume = safe_float(dollar_volume)

    if rsi <= 0:
        rsi_score = 0
    elif rsi <= 50:
        rsi_score = rsi * 0.7
    elif rsi <= 70:
        rsi_score = 35 + ((rsi - 50) / 20) * 10
    else:
        rsi_score = max(20, 45 - ((rsi - 70) * 1.5))

    volume_score = min(30, volume_ratio * 12)
    relative_volume_score = min(15, relative_volume * 6)
    dollar_volume_score = min(10, dollar_volume / 10_000_000)
    return clamp_score(rsi_score + volume_score + relative_volume_score + dollar_volume_score)
