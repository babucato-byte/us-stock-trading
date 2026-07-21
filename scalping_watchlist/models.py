"""Data model for a single scalping watchlist entry.

Phase 2 instructions (section 4): define the model before the CSV storage
format. A field that cannot be computed from real data is never given a
fabricated numeric value — one of the sentinel strings below is used
instead, and the reason is recorded in `rejection_reasons`.
"""

from dataclasses import dataclass, field, fields
from typing import Optional, Union

# Sentinels for a field that could not be produced, with distinct meanings:
UNKNOWN = "UNKNOWN"              # value exists in principle but could not be determined this run
NOT_AVAILABLE = "NOT_AVAILABLE"  # no data source exists for this field at all (e.g. spread_estimate)
NOT_EVALUATED = "NOT_EVALUATED"  # calculation was skipped (e.g. symbol rejected before this stage ran)

SENTINELS = (UNKNOWN, NOT_AVAILABLE, NOT_EVALUATED)

# Lifecycle statuses (Phase 2 instructions, section 8). Phase 3 watches ACTIVE only.
STATUS_NEW = "NEW"
STATUS_ACTIVE = "ACTIVE"
STATUS_COOLING = "COOLING"
STATUS_EXPIRED = "EXPIRED"
STATUS_REJECTED = "REJECTED"
VALID_STATUSES = {STATUS_NEW, STATUS_ACTIVE, STATUS_COOLING, STATUS_EXPIRED, STATUS_REJECTED}

# Trading sessions a candidate can be detected in.
SESSION_PREMARKET = "premarket"
SESSION_REGULAR = "regular"

Numeric = Union[float, int, str]  # str only ever holds one of SENTINELS


@dataclass
class WatchlistEntry:
    symbol: str
    detected_at: str  # ISO 8601 timestamp, America/New_York
    trading_session: str  # SESSION_PREMARKET / SESSION_REGULAR
    latest_price: Numeric = UNKNOWN
    previous_close: Numeric = UNKNOWN
    gap_percent: Numeric = UNKNOWN
    premarket_volume: Numeric = NOT_EVALUATED
    current_volume: Numeric = UNKNOWN
    average_volume: Numeric = UNKNOWN
    relative_volume: Numeric = UNKNOWN
    average_dollar_volume: Numeric = UNKNOWN
    atr: Numeric = UNKNOWN
    atr_percent: Numeric = UNKNOWN
    liquidity_score: Numeric = UNKNOWN
    spread_estimate: Numeric = NOT_AVAILABLE  # no bid/ask data source wired in yet (see DECISION_LOG.md)
    repeat_count: int = 1
    smart_money_score: Numeric = NOT_EVALUATED
    source_score: Numeric = NOT_EVALUATED
    scalping_score: Numeric = NOT_EVALUATED
    eligibility_reasons: str = ""   # semicolon-joined
    rejection_reasons: str = ""     # semicolon-joined; empty when status != REJECTED
    status: str = STATUS_NEW
    expires_at: Numeric = UNKNOWN  # ISO 8601 timestamp, America/New_York

    def __post_init__(self):
        if self.status not in VALID_STATUSES:
            raise ValueError(f"Invalid status {self.status!r}; must be one of {sorted(VALID_STATUSES)}")


CSV_COLUMNS = [f.name for f in fields(WatchlistEntry)]


def is_sentinel(value) -> bool:
    return isinstance(value, str) and value in SENTINELS
