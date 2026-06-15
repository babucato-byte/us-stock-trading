from datetime import datetime


def safe_float(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def analyze_row(row, analyzed_at=None):
    analyzed_at = analyzed_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    volume_ratio = safe_float(row.get("volume_ratio"))
    rsi = safe_float(row.get("rsi"))
    smart = safe_float(row.get("smart_money_score"))
    score = safe_float(row.get("score"))
    final_score = safe_float(row.get("final_score"), score)
    trend = row.get("trend", "Unknown")
    momentum_score = safe_float(row.get("momentum_score"))
    breakout_score = safe_float(row.get("breakout_score"))

    risk_level = "medium"
    if rsi >= 70 or volume_ratio >= 4:
        risk_level = "high"
    elif 45 <= rsi <= 62 and volume_ratio < 3:
        risk_level = "low"

    gpt_score = min(100, int(final_score * 0.55 + smart * 0.20 + momentum_score * 0.15 + breakout_score * 0.10))
    symbol = row.get("symbol", "")
    return {
        "symbol": symbol,
        "summary": (
            f"{symbol}는 Paper Trading 검증 관점의 규칙 기반 분석 대상입니다. "
            "정규장 재확인 필요 조건으로만 참고하며, 자동 주문 판단에는 사용하지 않습니다."
        ),
        "risk_level": risk_level,
        "reason": (
            f"Final Score {final_score:.0f}, trend {trend}, momentum {momentum_score:.0f}, "
            f"breakout {breakout_score:.0f}, smart-money {smart:.0f}, RSI {rsi:.1f}, "
            f"volume {volume_ratio:.2f}x 기준입니다."
        ),
        "action_note": "매수 추천이 아니며 정규장 재확인 필요. 실거래 권유 없이 Paper Trading 검증 보조로만 사용하세요.",
        "gpt_score": gpt_score,
        "analyzed_at": analyzed_at,
        "provider": "fallback",
        "model": "fallback",
    }


def analyze(rows):
    analyzed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return [analyze_row(row, analyzed_at=analyzed_at) for row in rows]
