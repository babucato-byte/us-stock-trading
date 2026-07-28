"""`AccountSnapshot` -- the KIS-authoritative account facts Risk/Sizing
read (spec §14/§18). Distinct from `live_readiness/account_engine.
AccountSnapshot` (the existing Alpaca-KRW-percent pilot model, kept
unchanged for that still-supported path) -- this one separates KRW cash
from USD cash and tracks USD reserved-in-open-orders explicitly, since
KIS's overseas order flow settles and reserves in USD, not KRW.
"""

import math
from dataclasses import dataclass
from datetime import datetime


class AccountSnapshotError(Exception):
    """Raised when an AccountSnapshot cannot be safely constructed."""


@dataclass(frozen=True)
class AccountSnapshot:
    krw_cash: float
    usd_cash: float
    usd_orderable_cash: float
    usd_reserved_in_open_orders: float
    as_of: datetime
    source: str
    account_id: str

    def __post_init__(self):
        for name in ("krw_cash", "usd_cash", "usd_orderable_cash", "usd_reserved_in_open_orders"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) \
                    or not math.isfinite(value) or value < 0:
                raise AccountSnapshotError(f"{name} must be a non-negative finite number, got {value!r}")
        if not isinstance(self.as_of, datetime) or self.as_of.tzinfo is None:
            raise AccountSnapshotError("as_of must be a timezone-aware datetime")
        for name in ("source", "account_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise AccountSnapshotError(f"{name} must be a non-empty string, got {value!r}")

    @property
    def usd_available_for_new_order(self):
        """KIS's own `usd_orderable_cash` is already net of what KIS
        itself considers reserved -- `usd_reserved_in_open_orders` here
        is this codebase's OWN durable ledger figure (spec §17's
        idempotency/duplicate tracking), subtracted again as a second,
        independent floor so a KIS-side accounting lag never lets two
        systems' views of "available cash" silently diverge upward."""
        return max(0.0, self.usd_orderable_cash - self.usd_reserved_in_open_orders)

    def is_stale(self, *, max_age_seconds, now=None):
        from datetime import timezone
        current = now or datetime.now(timezone.utc)
        age_seconds = (current - self.as_of).total_seconds()
        return age_seconds < 0 or age_seconds > max_age_seconds
