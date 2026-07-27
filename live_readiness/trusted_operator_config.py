"""Trusted operator configuration -- the SOLE source for policy values a
caller/Strategy/request context must never be able to set or raise.

Account/Risk/Sizing/Execution Engines read `cash_usage_percent` and the
concurrent-position/daily-entry ceilings ONLY from this module, never
from a caller-supplied context. This is the single source of truth these
values were previously scattered across (order_gateway.py's
MAX_CONCURRENT_LIVE_POSITIONS/MAX_DAILY_LIVE_ENTRIES,
account_cash.py's TRUSTED_CASH_USAGE_PERCENT_CEILING) -- both modules now
import from here instead of defining their own copy, so there is exactly
one place an operator edits to change deployed policy.

All three values are validated on every read (`get_*()` functions, not
bare module attributes) so a corrupted/edited-into-invalid-state config
fails closed (blocks all new entries) rather than silently using a
garbage number.
"""

import math

# CODEX-036 required behavior: "기본값은 보수적으로 설정, 운영자 승인 전 변경
# 금지" (conservative default, no change without operator approval) -- 50%
# is a deliberately conservative starting ceiling for a brand-new
# limited-live pilot, not a market/technical constant. Raising it is an
# operator decision made by editing this constant under code review, never
# a per-call caller choice.
CASH_USAGE_PERCENT_CEILING = 50

# Matches docs/live_review/LIMITED_LIVE_30K_KRW_PLAYBOOK.md §3's recommended
# initial limits: 1 concurrent position, up to 2 daily entries. Trusted
# code constants -- a caller's own context value can only ever TIGHTEN
# these via min(), never loosen them.
MAX_CONCURRENT_LIVE_POSITIONS = 1
MAX_DAILY_LIVE_ENTRIES = 2


class TrustedConfigError(Exception):
    """Raised when a trusted operator config value cannot be validated.
    Callers must treat this as a hard block on new live entries -- there
    is no meaningful fallback."""


def _validate_percent(value, name):
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TrustedConfigError(f"{name} must be a number, got {value!r}")
    if not math.isfinite(value):
        raise TrustedConfigError(f"{name} must be finite, got {value!r}")
    if not (0 < value <= 100):
        raise TrustedConfigError(f"{name} must be in (0, 100], got {value!r}")


def _validate_positive_int(value, name):
    if value is None or isinstance(value, bool) or not isinstance(value, int):
        raise TrustedConfigError(f"{name} must be an int, got {value!r}")
    if value < 1:
        raise TrustedConfigError(f"{name} must be >= 1, got {value!r}")


def get_cash_usage_percent_ceiling():
    """The trusted upper bound on cash_usage_percent. A caller's own
    cash_usage_percent can only ever be tightened against this via
    min() -- never loosened."""
    _validate_percent(CASH_USAGE_PERCENT_CEILING, "CASH_USAGE_PERCENT_CEILING")
    return CASH_USAGE_PERCENT_CEILING


def get_max_concurrent_live_positions():
    _validate_positive_int(MAX_CONCURRENT_LIVE_POSITIONS, "MAX_CONCURRENT_LIVE_POSITIONS")
    return MAX_CONCURRENT_LIVE_POSITIONS


def get_max_daily_live_entries():
    _validate_positive_int(MAX_DAILY_LIVE_ENTRIES, "MAX_DAILY_LIVE_ENTRIES")
    return MAX_DAILY_LIVE_ENTRIES
