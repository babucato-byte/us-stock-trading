"""Stage E: explainable composite scoring (Phase 2 instructions, section 5).

Every sub-score is normalized to [0, 100] before weighting so the final
scalping_score is always in [0, 100] too, and clamped defensively against
NaN/Infinity (a required test case). Weights live in
config/scalping_watchlist_config.py, not here — they are unvalidated
initial guesses (see DECISION_LOG.md), deliberately not backed by
elaborate curve-fitting, per section 5's "과거 성과 근거 없이 복잡한
가중치를 만들지 않습니다".
"""

import math

from config import scalping_watchlist_config as cfg
from .models import NOT_EVALUATED, is_sentinel


def _clamp(value, lo=0.0, hi=100.0):
    if value is None or isinstance(value, str):
        return 0.0
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(value) or math.isinf(value):
        return 0.0
    return max(lo, min(hi, value))


def _volume_score(features):
    relative_volume = features.get("relative_volume")
    if is_sentinel(relative_volume):
        return 0.0
    return _clamp(float(relative_volume) * 10)  # 10x relative volume -> 100


def _gap_score(features):
    gap_percent = features.get("gap_percent")
    if is_sentinel(gap_percent):
        return 0.0
    return _clamp(abs(float(gap_percent)) * 4)  # 25% gap -> 100


def _volatility_score(features):
    atr_percent = features.get("atr_percent")
    if is_sentinel(atr_percent):
        return 0.0
    return _clamp(float(atr_percent) * 20)  # 5% ATR -> 100


def _liquidity_score(features):
    liquidity_score = features.get("liquidity_score")
    if is_sentinel(liquidity_score):
        return 0.0
    return _clamp(float(liquidity_score))


def _repeat_score(repeat_info):
    if not repeat_info:
        return 0.0
    streak = repeat_info.get("consecutive_streak") or 0
    try:
        streak = float(streak)
    except (TypeError, ValueError):
        streak = 0.0
    return _clamp(streak * 25)  # 4 consecutive detections -> 100


def _smart_money_component(smart_money_score):
    if is_sentinel(smart_money_score):
        return 0.0
    return _clamp(float(smart_money_score))


def compute_scalping_score(features, repeat_info=None, smart_money_score=NOT_EVALUATED):
    """Returns (scalping_score: float, sub_scores: dict). Deterministic and
    input-order independent — a pure function of its arguments only."""
    sub_scores = {
        "liquidity_score": _liquidity_score(features),
        "volume_score": _volume_score(features),
        "gap_score": _gap_score(features),
        "volatility_score": _volatility_score(features),
        "repeat_score": _repeat_score(repeat_info),
        "smart_money_component": _smart_money_component(smart_money_score),
    }
    total = sum(sub_scores[name] * cfg.SCORING_WEIGHTS[name] for name in sub_scores)
    return _clamp(total), sub_scores
