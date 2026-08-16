"""Backtest cost/execution assumptions.

IMPORTANT: none of these numbers are fit to make any strategy's backtest
results look better. They are fixed, documented engineering assumptions
about transaction costs and execution realism, recorded in
docs/autonomous/DECISION_LOG.md's Stage 7 section *before* any strategy
was backtested with them, and they are never adjusted after seeing a
result. If a real number becomes available (e.g. Alpaca's actual
commission schedule, or measured historical spread/slippage for a
symbol), update it here with a DECISION_LOG.md entry explaining the
source -- never silently tune it because a strategy's metrics looked bad.
"""

from dataclasses import dataclass

SAME_BAR_COLLISION_STOP_FIRST = "STOP_FIRST"
# The only supported collision policy. When a single bar's [Low, High]
# range touches both the stop price and a target price, we cannot know
# from 1-minute OHLCV alone which was actually touched first intrabar.
# Assuming the worse outcome (stop first) is the conservative choice --
# it can only ever understate a strategy's edge, never flatter it.
SUPPORTED_COLLISION_POLICIES = {SAME_BAR_COLLISION_STOP_FIRST}


@dataclass(frozen=True)
class BacktestConfig:
    spread_bps: float = 5.0
    slippage_bps: float = 5.0
    fee_per_share: float = 0.0  # Alpaca is commission-free for US equities as of this writing
    entry_delay_bars: int = 1  # signal at bar i fills using bar (i + 1 + entry_delay_bars)'s open
    same_bar_collision_policy: str = SAME_BAR_COLLISION_STOP_FIRST
    max_fill_fraction_of_bar_volume: float = 0.10
    min_bars_required: int = 500
    nominal_qty: int = 100
    # ASSUMPTION: a fixed nominal lot size used for every simulated trade
    # (subject to volume-fraction capping like any other fill). Real
    # position sizing (risk budget / stop distance / the 30,000 KRW cap)
    # is Stage 4/Stage 10's live-trading concern, not this engine's --
    # nominal_qty only needs to be large enough that the 1R 50% partial
    # exit and 2R runner exit are each a meaningful, distinct fill rather
    # than both collapsing onto a single indivisible share.
    entry_allowed_sessions: tuple = ("regular",)
    # mirrors paper_strategy_order.py's real gate (get_us_market_session() == "regular"
    # is the only session real orders are ever submitted in) -- premarket bars are still
    # visible to the strategy for indicator warmup (VWAP/EMA/ATR), just never an entry trigger.

    def __post_init__(self):
        if self.same_bar_collision_policy not in SUPPORTED_COLLISION_POLICIES:
            raise ValueError(
                f"Unsupported same_bar_collision_policy: {self.same_bar_collision_policy!r}; "
                f"must be one of {sorted(SUPPORTED_COLLISION_POLICIES)}"
            )
        if not (0 <= self.max_fill_fraction_of_bar_volume <= 1):
            raise ValueError("max_fill_fraction_of_bar_volume must be in [0, 1]")
        if self.entry_delay_bars < 0:
            raise ValueError("entry_delay_bars must be >= 0")
        if self.min_bars_required < 1:
            raise ValueError("min_bars_required must be >= 1")
