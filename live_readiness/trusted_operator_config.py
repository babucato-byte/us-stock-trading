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

CODEX-039: `get_cash_usage_percent()` -- the function the new Account/
Risk/Sizing/Execution Engine pipeline (`live_readiness/
live_entry_pipeline.py`) actually calls -- takes NO arguments and returns
this trusted value VERBATIM. There is no caller-supplied percent to
combine it with in that pipeline at all (Strategy/caller code never
constructs a percent in the first place -- see PROJECT_CONSTITUTION.md's
계층 분리 원칙). `get_cash_usage_percent_ceiling()` is kept, unchanged,
for `order_gateway.py`'s pre-existing `LiveEntryContext.cash_usage_percent`
contract (the grandfathered legacy compat path, where a caller-declared
percent field still exists for backward compatibility and is only ever
tightened via `min()`, never loosened) -- the two functions currently
return the identical number, but they are named and documented
separately so a future change to one is never mistaken for a change to
the other's contract.
"""

import math

# 2026-07-28 자동 운영 구조 변경: 운영자가 매일 별도로 값을 입력하지 않아도
# 시스템이 그대로 사용하는 자동 기본값. 90은 "운영자 입력이 없으면 90 사용"
# 이라는 명시적 요구사항에 따른 값이며, 1~100 사이에서만 유효(margin/leverage는
# 이 비율과 무관하게 항상 금지 -- account_engine.py의 effective_cash_krw가
# non-margin cash로만 산정됨). 이 값을 바꾸는 것은 여전히 코드 리뷰를 거치는
# 운영자 결정이며, per-call caller 선택이 아니다.
CASH_USAGE_PERCENT_CEILING = 90

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


def get_cash_usage_percent():
    """CODEX-039: the ACTUAL percent the new engine pipeline applies --
    no caller value is combined with it, ever. Renaming this from
    "ceiling" to the plain trusted value itself: a caller/Strategy has no
    percent field to submit in the new pipeline's contract at all, so
    there is nothing left to cap -- this IS the number used."""
    _validate_percent(CASH_USAGE_PERCENT_CEILING, "CASH_USAGE_PERCENT_CEILING")
    return CASH_USAGE_PERCENT_CEILING


def get_cash_usage_percent_ceiling():
    """The trusted upper bound on cash_usage_percent for the LEGACY
    `order_gateway.py` contract only (`LiveEntryContext.cash_usage_percent`
    still exists there for backward compatibility) -- a caller's own
    cash_usage_percent can only ever be tightened against this via
    min(), never loosened. New code should call `get_cash_usage_percent()`
    instead; this function is kept solely so `order_gateway.py`'s
    existing behavior/tests are unaffected."""
    _validate_percent(CASH_USAGE_PERCENT_CEILING, "CASH_USAGE_PERCENT_CEILING")
    return CASH_USAGE_PERCENT_CEILING


def get_max_concurrent_live_positions():
    _validate_positive_int(MAX_CONCURRENT_LIVE_POSITIONS, "MAX_CONCURRENT_LIVE_POSITIONS")
    return MAX_CONCURRENT_LIVE_POSITIONS


def get_max_daily_live_entries():
    _validate_positive_int(MAX_DAILY_LIVE_ENTRIES, "MAX_DAILY_LIVE_ENTRIES")
    return MAX_DAILY_LIVE_ENTRIES
