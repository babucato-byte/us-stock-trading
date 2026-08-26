"""`OrderIntent` -- the broker-agnostic order the central Order Gate
(`execution/order_gate.py`) validates and the Execution Engine hands to
`brokers/kis_broker.py`. Never constructed by Strategy code directly with
a caller-declared quantity/price that bypasses Risk/Sizing -- see
`execution/execution_engine.py` for the only place this is built from a
`Signal` + a Sizing decision.

Quantity is always an int (spec §19/§30: 소수점 주문 금지, integer-only
live orders) and `order_type` is restricted to what this pilot allows
(`limit` only, per spec §21's `order_types: [limit]`) -- both are
enforced here at construction time, not left to a downstream check.
"""

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

ALLOWED_ORDER_TYPES = frozenset({"limit"})
ALLOWED_SIDES = frozenset({"buy", "sell"})


class OrderIntentError(Exception):
    """Raised when an OrderIntent cannot be safely constructed. Callers
    must treat this as a hard block -- there is no partial/best-effort
    order."""


@dataclass(frozen=True)
class OrderIntent:
    internal_order_id: str
    signal_id: str
    strategy_id: str
    symbol: str
    exchange: str
    side: str
    quantity: int
    order_type: str
    limit_price: float
    stop_price: Optional[float]
    target_price: Optional[float]
    created_at: datetime
    #: The US market session this order is being placed INTO.
    #:
    #: KIS routes the regular session and daytime trading to different
    #: endpoints with different TR families, and `brokers/kis_broker.py`
    #: already reads this field first when resolving the route -- it
    #: simply never had it, so every order fell back to the broker's
    #: `_session_hint` class default of "REGULAR". An S6 order placed in
    #: OVERNIGHT_DAYTIME was therefore addressed to the regular endpoint,
    #: which does not run at that hour.
    #:
    #: Optional, and None keeps the previous behaviour exactly (the
    #: broker falls back to its hint), so no existing caller changes
    #: meaning by gaining the field. A caller that knows its session says
    #: so, and the route follows the order rather than the process.
    session: Optional[str] = None

    def __post_init__(self):
        for field_name in ("internal_order_id", "signal_id", "strategy_id", "symbol", "exchange"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise OrderIntentError(f"{field_name} must be a non-empty string, got {value!r}")
        if self.side not in ALLOWED_SIDES:
            raise OrderIntentError(f"side must be one of {sorted(ALLOWED_SIDES)}, got {self.side!r}")
        if self.order_type not in ALLOWED_ORDER_TYPES:
            raise OrderIntentError(
                f"order_type must be one of {sorted(ALLOWED_ORDER_TYPES)}, got {self.order_type!r} "
                "(market orders are never allowed in this pilot -- spec §30)"
            )
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity < 1:
            raise OrderIntentError(
                f"quantity must be a positive int (fractional orders are never allowed -- "
                f"spec §19/§30), got {self.quantity!r}"
            )
        if not isinstance(self.limit_price, (int, float)) or isinstance(self.limit_price, bool) \
                or not math.isfinite(self.limit_price) or self.limit_price <= 0:
            raise OrderIntentError(f"limit_price must be a positive finite number, got {self.limit_price!r}")
        for name in ("stop_price", "target_price"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value) or value <= 0
            ):
                raise OrderIntentError(f"{name} must be a positive finite number or None, got {value!r}")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise OrderIntentError("created_at must be a timezone-aware datetime")
        # Blank is not None. None means "this caller does not know the
        # session, use the broker default"; an empty string means a
        # caller COMPUTED one and got nothing, which on an order path is
        # a fault that must not quietly resolve to the regular route.
        if self.session is not None and (
                not isinstance(self.session, str) or not self.session.strip()):
            raise OrderIntentError(
                f"session must be a non-empty string or None, got {self.session!r}")
