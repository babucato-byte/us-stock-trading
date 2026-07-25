"""Configuration for the `VWAP_MICRO_PULLBACK_MOMENTUM_V1` strategy plugin.

Same env-var-with-default pattern as `config/scalping_watchlist_config.py`
(kept as a separate module for the same reason that one is separate from
`risk_config.py`: this is strategy-signal tuning, not order-execution risk
policy, and it is not reachable from the dashboard's risk-editing surface).

Every threshold below is an initial, conservative guess grounded in
PROJECT_CONSTITUTION.md's description of the setup ("초기 rally, 얕은
pullback with declining volume, 재돌파 거래량 증가"), not a backtested value.
Where the constitution/roadmap text does not specify an exact number, the
constant is marked `# ASSUMPTION` and the reasoning is recorded in
docs/autonomous/DECISION_LOG.md ("Phase 4 VWAP 마이크로 풀백 전략: 초기 설정값
근거") rather than silently guessed. None of these are validated until
Phase 6 backtesting (roadmap Phase 4 "잔여 위험").
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


# How many trailing bars are searched for a rally+pullback+breakout shape.
RALLY_LOOKBACK_BARS = _env_int("SCALPING_STRATEGY_RALLY_LOOKBACK_BARS", 20)

# Minimum total bars evaluate_setup() needs before it will even attempt
# rally/pullback detection (fewer bars -> NO_SETUP, never a guess).
MIN_BARS_FOR_SETUP = _env_int("SCALPING_STRATEGY_MIN_BARS_FOR_SETUP", 8)

# A pullback must span at least this many bars to count as a real pullback
# (one bar of weakness is noise, not a pullback structure).
MIN_PULLBACK_BARS = _env_int("SCALPING_STRATEGY_MIN_PULLBACK_BARS", 2)

# ASSUMPTION: "초기 rally"의 최소 크기는 문서에 수치로 명시되어 있지 않다.
# 너무 작은 값은 잡음을 신호로 오인하므로, 최소한 눈에 띄는 상승(0.5%)을 rally로
# 요구한다. 근거: DECISION_LOG.md.
RALLY_MIN_PERCENT = _env_float("SCALPING_STRATEGY_RALLY_MIN_PERCENT", 0.5)

# ASSUMPTION: "얕은 눌림(shallow pullback)"의 정확한 % 범위는 문서에 없다. 너무
# 얕으면(<0.1%) 노이즈와 구분 불가, 너무 깊으면(>3%) 더 이상 "micro" pullback이
# 아니라고 보아 상한을 둔다. 근거: DECISION_LOG.md.
MIN_PULLBACK_DEPTH_PERCENT = _env_float("SCALPING_STRATEGY_MIN_PULLBACK_DEPTH_PERCENT", 0.1)
MAX_PULLBACK_DEPTH_PERCENT = _env_float("SCALPING_STRATEGY_MAX_PULLBACK_DEPTH_PERCENT", 3.0)

# Breakout bar's volume must be at least this multiple of the pullback's
# average volume to count as "거래량 재확대" (re-expansion), not just a
# random uptick.
VOLUME_EXPANSION_MULTIPLIER = _env_float("SCALPING_STRATEGY_VOLUME_EXPANSION_MULTIPLIER", 1.2)

# ATR period used for the stop's minimum-buffer calculation.
ATR_PERIOD = _env_int("SCALPING_STRATEGY_ATR_PERIOD", 14)

# calculate_stop(): the stop is the micro-pullback low, widened if needed so
# the entry-to-stop distance is never less than ATR_STOP_MULTIPLIER * ATR
# (constitution: "손절 위치" must use "micro-pullback low (with an ATR-based
# minimum buffer)").
ATR_STOP_MULTIPLIER = _env_float("SCALPING_STRATEGY_ATR_STOP_MULTIPLIER", 1.0)

# calculate_targets(): documented initial policy is "1R 도달 시 50% 분할
# 익절". target_1 at 1R is explicit in PROJECT_CONSTITUTION.md's flow
# description ("자동 손절 -> 50% 분할 익절"). ASSUMPTION: the R-multiple for
# the remaining runner (target_2) after the 1R partial is not specified
# anywhere in the source docs; 2R is used as a conservative, commonly-used
# runner target pending Phase 6 backtest validation. Recorded in
# DECISION_LOG.md.
TARGET_1_R_MULTIPLE = _env_float("SCALPING_STRATEGY_TARGET_1_R_MULTIPLE", 1.0)
TARGET_2_R_MULTIPLE = _env_float("SCALPING_STRATEGY_TARGET_2_R_MULTIPLE", 2.0)
PARTIAL_EXIT_FRACTION_AT_TARGET_1 = _env_float(
    "SCALPING_STRATEGY_PARTIAL_EXIT_FRACTION_AT_TARGET_1", 0.5
)

# Stage 4 (roadmap Phase 5): a position open longer than this is force-exited
# (exit_reason="TIME_STOP") regardless of where price is. ASSUMPTION: the
# constitution requires a time stop but does not specify a duration; a
# scalping strategy with "보유 시간: 수분에서 당일" (minutes to same-day) makes
# 60 minutes a conservative default pending Phase 6 backtest validation.
# Recorded in DECISION_LOG.md.
MAX_POSITION_HOLD_MINUTES = _env_int("SCALPING_STRATEGY_MAX_POSITION_HOLD_MINUTES", 60)

# Stage 4: how many minutes before the regular session close a still-open
# position is force-liquidated (exit_reason="EOD_FORCED_CLOSE"). Must be
# large enough that the forced-exit order itself can be submitted and
# reasonably expected to fill before 16:00 ET.
EOD_FORCE_CLOSE_MINUTES_BEFORE_CLOSE = _env_int(
    "SCALPING_STRATEGY_EOD_FORCE_CLOSE_MINUTES_BEFORE_CLOSE", 5
)
