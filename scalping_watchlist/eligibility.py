"""Stage A/B/C eligibility gates (Phase 2 instructions, section 5).

Deliberately NOT built on daily_candidate_scanner.py's JSON rule engine:
that engine warns-and-passes on an unsupported field, which is the
opposite of Phase 2's explicit principle "불명확하면 포함하지 않는다"
(when in doubt, exclude). See DECISION_LOG.md for the reuse-scope
decision. These are small, explicit, directly testable functions instead.

Stage A (tradable / active / US equity / valid symbol) is already enforced
upstream by universe_builder.py's Alpaca asset filtering before a symbol
ever reaches universe.csv, so it is re-validated here only defensively
(non-empty, uppercase-alnum symbol string).
"""

import re

from config import scalping_watchlist_config as cfg
from .models import is_sentinel

_VALID_SYMBOL_PATTERN = re.compile(r"^[A-Z]{1,6}(\.[A-Z])?$")


def check_symbol_format(symbol):
    """Stage A (defensive re-check only; see module docstring)."""
    if not symbol or not isinstance(symbol, str):
        return ["INVALID_SYMBOL: empty or non-string symbol"]
    if not _VALID_SYMBOL_PATTERN.match(symbol):
        return [f"INVALID_SYMBOL: {symbol!r} does not look like a valid US equity ticker"]
    return []


def check_price_and_liquidity(features):
    """Stage B."""
    reasons = []
    price = features.get("latest_price")
    if is_sentinel(price):
        reasons.append("PRICE_UNAVAILABLE")
    else:
        if price < cfg.MIN_PRICE:
            reasons.append(f"PRICE_TOO_LOW: {price} < {cfg.MIN_PRICE}")
        if price > cfg.MAX_PRICE:
            reasons.append(f"PRICE_TOO_HIGH: {price} > {cfg.MAX_PRICE}")

    average_volume = features.get("average_volume")
    if is_sentinel(average_volume):
        reasons.append("AVERAGE_VOLUME_UNAVAILABLE")
    elif average_volume < cfg.MIN_AVERAGE_VOLUME:
        reasons.append(f"AVERAGE_VOLUME_TOO_LOW: {average_volume} < {cfg.MIN_AVERAGE_VOLUME}")

    dollar_volume = features.get("average_dollar_volume")
    if is_sentinel(dollar_volume):
        reasons.append("AVERAGE_DOLLAR_VOLUME_UNAVAILABLE")
    elif dollar_volume < cfg.MIN_AVERAGE_DOLLAR_VOLUME:
        reasons.append(f"AVERAGE_DOLLAR_VOLUME_TOO_LOW: {dollar_volume} < {cfg.MIN_AVERAGE_DOLLAR_VOLUME}")

    current_volume = features.get("current_volume")
    if is_sentinel(current_volume):
        reasons.append("CURRENT_VOLUME_UNAVAILABLE")
    elif current_volume < cfg.MIN_CURRENT_VOLUME:
        reasons.append(f"CURRENT_VOLUME_TOO_LOW: {current_volume} < {cfg.MIN_CURRENT_VOLUME}")

    liquidity_score = features.get("liquidity_score")
    if is_sentinel(liquidity_score):
        reasons.append("LIQUIDITY_SCORE_UNAVAILABLE")
    elif liquidity_score < cfg.MIN_LIQUIDITY_SCORE:
        reasons.append(f"LIQUIDITY_TOO_LOW: {liquidity_score} < {cfg.MIN_LIQUIDITY_SCORE}")

    return reasons


def check_intraday_movement(features):
    """Stage C."""
    reasons = []

    relative_volume = features.get("relative_volume")
    if is_sentinel(relative_volume):
        reasons.append("RELATIVE_VOLUME_UNAVAILABLE")
    elif relative_volume < cfg.MIN_RELATIVE_VOLUME:
        reasons.append(f"RELATIVE_VOLUME_TOO_LOW: {relative_volume} < {cfg.MIN_RELATIVE_VOLUME}")

    gap_percent = features.get("gap_percent")
    if is_sentinel(gap_percent):
        reasons.append("GAP_UNAVAILABLE")
    else:
        abs_gap = abs(gap_percent)
        if abs_gap < cfg.MIN_GAP_PERCENT:
            reasons.append(f"GAP_TOO_SMALL: {abs_gap} < {cfg.MIN_GAP_PERCENT}")
        if abs_gap > cfg.MAX_GAP_PERCENT:
            reasons.append(f"GAP_TOO_LARGE: {abs_gap} > {cfg.MAX_GAP_PERCENT}")

    atr_percent = features.get("atr_percent")
    if is_sentinel(atr_percent):
        reasons.append("ATR_PERCENT_UNAVAILABLE")
    elif atr_percent < cfg.MIN_ATR_PERCENT:
        reasons.append(f"VOLATILITY_TOO_LOW: {atr_percent} < {cfg.MIN_ATR_PERCENT}")

    return reasons


def evaluate_eligibility(symbol, features, data_quality_reasons):
    """Runs Stage A/B/C in order. Returns (eligibility_reasons, rejection_reasons).

    All applicable rejection reasons are collected (not short-circuited) so
    the CSV always explains every reason a symbol was excluded, per section
    6's "rejection reason은 저장하여 왜 탈락했는지 확인 가능해야 합니다".
    """
    rejection_reasons = list(data_quality_reasons)
    rejection_reasons += check_symbol_format(symbol)
    rejection_reasons += check_price_and_liquidity(features)
    rejection_reasons += check_intraday_movement(features)

    eligibility_reasons = []
    if not rejection_reasons:
        eligibility_reasons.append("PASSED_STAGE_A_THROUGH_C")
    return eligibility_reasons, rejection_reasons
