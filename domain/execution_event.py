"""`ExecutionRecord` -- the broker's report of what actually happened to
an `OrderIntent`, keyed by `internal_order_id` (this codebase's own
idempotency key, never the broker's own order id -- see
`execution/idempotency.py`). `broker` is always the literal string
`"kis"` for any record that reached a real network call in this
migration; `error_code`/`error_message` are populated on any non-success
`status` so `execution/order_state_machine.py` never has to guess why an
order didn't confirm.
"""

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

VALID_STATUSES = frozenset({
    "CREATED", "VALIDATING", "APPROVED", "SUBMITTING", "ACCEPTED",
    "PARTIALLY_FILLED", "FILLED", "CANCEL_PENDING", "CANCELLED",
    "REJECTED", "UNKNOWN",
})


class ExecutionRecordError(Exception):
    """Raised when an ExecutionRecord cannot be safely constructed."""


@dataclass(frozen=True)
class ExecutionRecord:
    internal_order_id: str
    broker: str
    broker_order_id: Optional[str]
    requested_quantity: int
    requested_price: float
    filled_quantity: float
    average_fill_price: Optional[float]
    status: str
    submitted_at: datetime
    updated_at: datetime
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    def __post_init__(self):
        if not isinstance(self.internal_order_id, str) or not self.internal_order_id:
            raise ExecutionRecordError("internal_order_id must be a non-empty string")
        if not isinstance(self.broker, str) or not self.broker:
            raise ExecutionRecordError("broker must be a non-empty string")
        if self.status not in VALID_STATUSES:
            raise ExecutionRecordError(f"status must be one of {sorted(VALID_STATUSES)}, got {self.status!r}")
        if isinstance(self.requested_quantity, bool) or not isinstance(self.requested_quantity, int) \
                or self.requested_quantity < 1:
            raise ExecutionRecordError(f"requested_quantity must be a positive int, got {self.requested_quantity!r}")
        if not isinstance(self.requested_price, (int, float)) or isinstance(self.requested_price, bool) \
                or not math.isfinite(self.requested_price) or self.requested_price <= 0:
            raise ExecutionRecordError(f"requested_price must be a positive finite number, got {self.requested_price!r}")
        if not isinstance(self.filled_quantity, (int, float)) or isinstance(self.filled_quantity, bool) \
                or not math.isfinite(self.filled_quantity) or self.filled_quantity < 0:
            raise ExecutionRecordError(f"filled_quantity must be a non-negative finite number, got {self.filled_quantity!r}")
        if self.filled_quantity > self.requested_quantity:
            raise ExecutionRecordError(
                f"filled_quantity {self.filled_quantity!r} must not exceed "
                f"requested_quantity {self.requested_quantity!r}"
            )
        if self.average_fill_price is not None and (
            not isinstance(self.average_fill_price, (int, float)) or isinstance(self.average_fill_price, bool)
            or not math.isfinite(self.average_fill_price) or self.average_fill_price <= 0
        ):
            raise ExecutionRecordError(f"average_fill_price must be a positive finite number or None, got {self.average_fill_price!r}")
        for name in ("submitted_at", "updated_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ExecutionRecordError(f"{name} must be a timezone-aware datetime")

    @property
    def is_terminal(self):
        """FILLED/CANCELLED/REJECTED are terminal -- no further state
        transition is expected. UNKNOWN is deliberately NOT terminal:
        it must be reconciled to a real terminal state, never treated as
        "done" (spec §9)."""
        return self.status in ("FILLED", "CANCELLED", "REJECTED")
