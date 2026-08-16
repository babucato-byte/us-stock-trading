"""Trade / cost / result data model for the backtest engine."""

from dataclasses import dataclass, field
from typing import List, Optional

STATUS_OK = "OK"
STATUS_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass
class CostBreakdown:
    """Every field is a separately-reported dollar cost, per the
    requirement that spread/slippage/fees/entry-delay be shown as
    distinct line items rather than folded into one opaque number.
    spread_cost and slippage_cost are informational: they are already
    reflected in the fill prices used to compute realized_pnl (a fill
    price is quote price adjusted by spread+slippage), not an additional
    deduction. fee_cost genuinely is subtracted from realized_pnl
    (a flat per-share cost, not price-based). entry_delay_cost is also
    informational: the dollar impact of price drift between the signal
    bar's close and the actual fill bar's open, already embedded in the
    fill price used, reported separately so its size can be judged.
    """
    spread_cost: float = 0.0
    slippage_cost: float = 0.0
    fee_cost: float = 0.0
    entry_delay_cost: float = 0.0

    @property
    def total_informational(self):
        return self.spread_cost + self.slippage_cost + self.entry_delay_cost


@dataclass
class ExitEvent:
    time: str
    price: float
    qty: int
    reason: str
    session: str


@dataclass
class Trade:
    symbol: str
    strategy_id: str
    strategy_version: str
    signal_time: str
    signal_price: float  # signal bar's close -- reference price, not a fill
    entry_time: str
    entry_price: float  # actual fill price (spread+slippage applied)
    entry_session: str
    entry_bar_volume: float
    stop_price: float
    target_1_price: float
    target_2_price: float
    requested_qty: int
    filled_qty: int  # <= requested_qty if entry-bar volume constrained the fill
    exit_events: List[ExitEvent] = field(default_factory=list)
    exit_reason: str = ""
    realized_pnl: float = 0.0  # net of fee_cost
    r_multiple: Optional[float] = None
    costs: CostBreakdown = field(default_factory=CostBreakdown)


@dataclass
class BacktestResult:
    strategy_id: str
    strategy_version: str
    symbol: str
    status: str  # STATUS_OK | STATUS_INSUFFICIENT_DATA
    trades: List[Trade] = field(default_factory=list)
    reason: str = ""
    bars_evaluated: int = 0
