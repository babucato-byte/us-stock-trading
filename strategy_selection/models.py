"""Data model for the strategy selection engine."""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

SELECTED = "SELECTED"
NOT_SELECTED = "NOT_SELECTED"
DISABLED = "DISABLED"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
MARKET_MISMATCH = "MARKET_MISMATCH"

VALID_SELECTION_STATES = {SELECTED, NOT_SELECTED, DISABLED, INSUFFICIENT_DATA, MARKET_MISMATCH}


@dataclass
class SelectionInput:
    """Everything the engine needs to consider one candidate strategy.

    `strategy` is a strategy.interface.TradingStrategy instance (its
    .status is read directly -- callers must keep it in sync with
    whatever registry it came from). `backtest_result` is a Stage 7
    backtest.models.BacktestResult, or None if never backtested.
    `paper_metrics`/`slippage_sensitivity_result` use the same shapes
    Stage 7's backtest.metrics.compute_metrics()/slippage_sensitivity()
    already produce, so a real paper-trading summary can be fed in
    without a second parallel schema.
    """
    strategy: Any  # strategy.interface.TradingStrategy
    symbol: str
    backtest_result: Optional[Any] = None  # backtest.models.BacktestResult
    paper_metrics: Optional[Dict] = None  # same shape as backtest.metrics.compute_metrics()
    slippage_sensitivity_result: Optional[Dict] = None  # same shape as backtest.metrics.slippage_sensitivity()


@dataclass
class SelectionFactors:
    """Per-factor scores, each in [0.0, 1.0] or None if not evaluated
    (never fabricated as a default like 0.5 -- a None factor is simply
    excluded from the composite average, and is visible to a reviewer as
    "not evaluated," not silently averaged in as neutral)."""
    market_state_fit: Optional[float] = None
    symbol_condition_fit: Optional[float] = None
    backtest_performance: Optional[float] = None
    paper_performance: Optional[float] = None
    sample_size: Optional[float] = None
    mdd: Optional[float] = None
    slippage_sensitivity: Optional[float] = None

    def as_dict(self):
        return {
            "market_state_fit": self.market_state_fit,
            "symbol_condition_fit": self.symbol_condition_fit,
            "backtest_performance": self.backtest_performance,
            "paper_performance": self.paper_performance,
            "sample_size": self.sample_size,
            "mdd": self.mdd,
            "slippage_sensitivity": self.slippage_sensitivity,
        }


@dataclass
class SelectionResult:
    strategy_id: str
    strategy_version: str
    symbol: str
    state: str
    composite_score: Optional[float] = None
    factors: Optional[SelectionFactors] = None
    rationale: str = ""

    def __post_init__(self):
        if self.state not in VALID_SELECTION_STATES:
            raise ValueError(f"Invalid selection state {self.state!r}; must be one of {sorted(VALID_SELECTION_STATES)}")
