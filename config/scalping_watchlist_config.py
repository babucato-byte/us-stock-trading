"""Configuration for the Phase 2 scalping watchlist selection engine.

Kept separate from risk_config.py / scanner_rules.json on purpose (Phase 2
instructions, section 9): this is candidate-selection tuning, not risk or
order-execution policy, and must not be reachable from the dashboard's
existing risk-editing surface.

Every threshold below is an initial, conservative guess, not a backtested
value. Rationale for each is recorded in docs/autonomous/DECISION_LOG.md
("Phase 2 관심종목 선별 엔진: 재사용 범위와 초기 설정값 근거"). Do not treat
these as validated until Phase 6 backtesting.
"""

import os


def _env_float(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_int(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


# Stage B: price and liquidity
MIN_PRICE = _env_float("SCALPING_MIN_PRICE", 5.0)
MAX_PRICE = _env_float("SCALPING_MAX_PRICE", 500.0)
MIN_AVERAGE_VOLUME = _env_float("SCALPING_MIN_AVERAGE_VOLUME", 500_000)
MIN_AVERAGE_DOLLAR_VOLUME = _env_float("SCALPING_MIN_AVERAGE_DOLLAR_VOLUME", 20_000_000)
MIN_CURRENT_VOLUME = _env_float("SCALPING_MIN_CURRENT_VOLUME", 100_000)

# Stage C: intraday movement
MIN_RELATIVE_VOLUME = _env_float("SCALPING_MIN_RELATIVE_VOLUME", 3.0)
MIN_GAP_PERCENT = _env_float("SCALPING_MIN_GAP_PERCENT", 2.0)
MAX_GAP_PERCENT = _env_float("SCALPING_MAX_GAP_PERCENT", 50.0)
MIN_ATR_PERCENT = _env_float("SCALPING_MIN_ATR_PERCENT", 1.5)

# Liquidity proxy (no real bid/ask spread source is wired in yet — see
# DECISION_LOG.md). liquidity_score is derived from average_dollar_volume;
# MAX_SPREAD_PERCENT is retained for a future real spread source and is not
# enforced while spread_estimate is NOT_AVAILABLE.
MIN_LIQUIDITY_SCORE = _env_float("SCALPING_MIN_LIQUIDITY_SCORE", 20.0)
MAX_SPREAD_PERCENT = _env_float("SCALPING_MAX_SPREAD_PERCENT", 0.5)

# Data freshness (CODEX-011). A scan cycle runs roughly every 15 minutes
# (REPEAT_WINDOW_MINUTES below), so data older than one cycle is treated
# as stale rather than a fresh read of the current session.
MAX_PREMARKET_DATA_AGE_MINUTES = _env_float("SCALPING_MAX_PREMARKET_DATA_AGE_MINUTES", 15.0)
MAX_REGULAR_DATA_AGE_MINUTES = _env_float("SCALPING_MAX_REGULAR_DATA_AGE_MINUTES", 15.0)
MAX_AFTER_HOURS_DATA_AGE_MINUTES = _env_float("SCALPING_MAX_AFTER_HOURS_DATA_AGE_MINUTES", 15.0)

# Allowed trading sessions and the regular-session window (CODEX-012).
# Phase 2's charter (section 1) is explicitly premarket + "정규장 초반"
# (early regular session), not the full regular session — scanning the
# entire day at low frequency is Phase 2's job already handled elsewhere;
# this engine's job is the narrow early window feeding Phase 3. Decision
# and rationale recorded in DECISION_LOG.md.
ALLOWED_SESSIONS = ("premarket", "regular")
REGULAR_OPEN_WINDOW_MINUTES = _env_int("SCALPING_REGULAR_OPEN_WINDOW_MINUTES", 60)

# Stage D: persistence / repeat detection
MIN_REPEAT_COUNT = _env_int("SCALPING_MIN_REPEAT_COUNT", 1)
REPEAT_WINDOW_MINUTES = _env_int("SCALPING_REPEAT_WINDOW_MINUTES", 15)

# Average volume window (CODEX-015): the current/most recent trading day
# is always excluded before averaging (its volume is necessarily partial
# while the market is open), then the trailing LOOKBACK_DAYS of complete
# days are used, requiring at least MIN_VALID_VOLUME_DAYS to avoid
# computing an average from too little history.
AVERAGE_VOLUME_LOOKBACK_DAYS = _env_int("SCALPING_AVERAGE_VOLUME_LOOKBACK_DAYS", 20)
MIN_VALID_VOLUME_DAYS = _env_int("SCALPING_MIN_VALID_VOLUME_DAYS", 10)

# Watchlist sizing and lifecycle
MAX_WATCHLIST_SIZE = _env_int("SCALPING_MAX_WATCHLIST_SIZE", 30)
WATCHLIST_TTL_MINUTES = _env_int("SCALPING_WATCHLIST_TTL_MINUTES", 30)
WATCHLIST_EXPIRE_MINUTES = _env_int("SCALPING_WATCHLIST_EXPIRE_MINUTES", 60)

# Stage E: scoring weights (sum to 1.0; each sub-score is normalized 0-100
# before weighting). Unvalidated initial guess — see DECISION_LOG.md.
SCORING_WEIGHTS = {
    "liquidity_score": 0.15,
    "volume_score": 0.25,
    "gap_score": 0.15,
    "volatility_score": 0.15,
    "repeat_score": 0.15,
    "smart_money_component": 0.15,
}
