"""Explicit finite-number validation (CODEX-010).

Python's comparison operators (<, >, <=, >=) all evaluate to False when
either side is NaN, so a plain `value < MIN_PRICE` style range check
silently lets NaN through — it never satisfies the "too low" branch
either. compute_features()/eligibility.py must never rely on comparisons
alone; every number that reaches a downstream calculation or a threshold
check goes through require_finite_number() first.
"""

import math


class InvalidNumber(Exception):
    """A value failed finite-number validation. `reason_code` is the exact
    rejection-reason string to record (e.g. "INVALID_LATEST_PRICE")."""

    def __init__(self, reason_code, message):
        super().__init__(message)
        self.reason_code = reason_code


def require_finite_number(value, *, field_name, min_value=None, max_value=None,
                           allow_zero=True, min_exclusive=False):
    """Returns a validated float, or raises InvalidNumber.

    A valid number is: int or float, not bool (bool is an int subclass in
    Python but must never sneak through a numeric field), not None, not
    NaN, not +-Infinity, and within [min_value, max_value] (or
    (min_value, max_value] if min_exclusive). allow_zero=False additionally
    rejects exactly 0.
    """
    reason = f"INVALID_{field_name.upper()}"

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidNumber(reason, f"{field_name} must be a real number, got {type(value).__name__}: {value!r}")

    value = float(value)
    if math.isnan(value):
        raise InvalidNumber(reason, f"{field_name} is NaN")
    if math.isinf(value):
        raise InvalidNumber(reason, f"{field_name} is infinite")

    if not allow_zero and value == 0:
        raise InvalidNumber(reason, f"{field_name} must not be zero")
    if min_exclusive and min_value is not None and value <= min_value:
        raise InvalidNumber(reason, f"{field_name} {value} must be > {min_value}")
    if not min_exclusive and min_value is not None and value < min_value:
        raise InvalidNumber(reason, f"{field_name} {value} must be >= {min_value}")
    if max_value is not None and value > max_value:
        raise InvalidNumber(reason, f"{field_name} {value} must be <= {max_value}")

    return value


def is_finite_number(value):
    """Non-raising check, for call sites that just need a bool (e.g. final
    scalping_score sanity check)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))
