"""`Position` -- KIS is always the authoritative source (spec §14/§16);
this dataclass is what `reconciliation/position_reconciler.py` compares
internal records against, and what `execution/order_gate.py`'s sell-side
checks read `quantity` from -- never Alpaca's paper/virtual position.
"""

import math
from dataclasses import dataclass
from datetime import datetime


class PositionError(Exception):
    """Raised when a Position cannot be safely constructed."""


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: int
    average_fill_price: float
    unrealized_pnl: float
    realized_pnl: float
    as_of: datetime
    source: str

    def __post_init__(self):
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise PositionError(f"symbol must be a non-empty string, got {self.symbol!r}")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity < 0:
            raise PositionError(f"quantity must be a non-negative int, got {self.quantity!r}")
        if not isinstance(self.average_fill_price, (int, float)) or isinstance(self.average_fill_price, bool) \
                or not math.isfinite(self.average_fill_price) or self.average_fill_price < 0:
            raise PositionError(f"average_fill_price must be a non-negative finite number, got {self.average_fill_price!r}")
        for name in ("unrealized_pnl", "realized_pnl"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                raise PositionError(f"{name} must be a finite number, got {value!r}")
        if not isinstance(self.as_of, datetime) or self.as_of.tzinfo is None:
            raise PositionError("as_of must be a timezone-aware datetime")
        if not isinstance(self.source, str) or not self.source.strip():
            raise PositionError(f"source must be a non-empty string, got {self.source!r}")

    @property
    def is_flat(self):
        return self.quantity == 0
