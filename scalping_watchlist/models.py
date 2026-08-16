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
    # CODEX-014: three distinct lifecycle timestamps, not one — all must be
    # timezone-aware ISO 8601 strings. A single ambiguous "detected_at"
    # made it impossible to tell "first seen" from "still being seen" from
    # "record last touched", which is what let a corrupted value silently
    # keep a row ACTIVE forever (see validate_lifecycle_timestamps below).
    first_detected_at: str
    last_detected_at: str
    updated_at: str
    trading_session: str  # SESSION_PREMARKET / SESSION_REGULAR
    latest_price: Numeric = UNKNOWN
    previous_close: Numeric = UNKNOWN
    gap_percent: Numeric = UNKNOWN
    premarket_volume: Numeric = NOT_EVALUATED
    # CODEX-015: whether premarket_volume covers the full 04:00-09:30 ET
    # window or only part of it (provider-dependent) — never presented as
    # "the" premarket volume without this caveat.
    premarket_coverage_complete: Numeric = NOT_EVALUATED
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

LIFECYCLE_TIMESTAMP_FIELDS = ("first_detected_at", "last_detected_at", "expires_at", "updated_at")


def is_sentinel(value) -> bool:
    return isinstance(value, str) and value in SENTINELS


def parse_lifecycle_timestamp(value):
    """Returns a timezone-aware datetime, or raises ValueError.

    CODEX-014 policy (documented, not auto-corrected): a lifecycle
    timestamp must be a non-empty, non-sentinel, timezone-aware ISO 8601
    string. Empty string, None, NaN, a naive datetime string, or a string
    that fails to parse are all rejected outright — never coerced to a
    "best guess" value, since that is exactly how a corrupted timestamp
    could previously bypass TTL expiry and keep a row ACTIVE forever.
    """
    from datetime import datetime

    if not isinstance(value, str) or not value or is_sentinel(value):
        raise ValueError(f"lifecycle timestamp is empty or a sentinel: {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"lifecycle timestamp is not valid ISO 8601: {value!r}")
    if parsed.tzinfo is None:
        raise ValueError(f"lifecycle timestamp is not timezone-aware: {value!r}")
    return parsed


def validate_lifecycle_timestamps(row):
    """Validates a watchlist row's timestamp fields as a set, including
    ordering invariants. Returns a list of problems (empty = valid).

    Policy: a row failing ANY of these checks is rejected (status set to
    REJECTED with reason INVALID_LIFECYCLE_TIMESTAMP by the caller) rather
    than the whole watchlist store being treated as fail-closed — one
    corrupted row must not block every other symbol's lifecycle tracking.
    This policy choice is recorded in DECISION_LOG.md.
    """
    problems = []
    parsed = {}
    for field_name in LIFECYCLE_TIMESTAMP_FIELDS:
        try:
            parsed[field_name] = parse_lifecycle_timestamp(row.get(field_name))
        except ValueError as exc:
            problems.append(f"{field_name}: {exc}")

    if problems:
        return problems  # ordering checks need all four parsed; skip if any already failed

    if parsed["last_detected_at"] < parsed["first_detected_at"]:
        problems.append("last_detected_at is before first_detected_at")
    if parsed["expires_at"] < parsed["first_detected_at"]:
        problems.append("expires_at is before first_detected_at")

    return problems
