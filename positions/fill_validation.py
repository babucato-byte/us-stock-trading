"""CODEX-027: strict validation for fill quantities/prices.

A fill is a domain object with hard invariants -- negative, NaN, or
regressing quantities and non-positive/NaN prices must never reach
positions/store.py. Validation happens here, once, so every call site
(record_fill(), the exit-fill-confirmation path) gets the same guarantees
rather than re-deriving them ad hoc.
"""

import math


class InvalidFillError(Exception):
    pass


def _is_finite_number(value):
    if isinstance(value, bool):  # bool is an int subclass -- reject explicitly
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def validate_cumulative_fill(requested_qty, previous_filled_qty, new_filled_qty, average_fill_price):
    """Validate a new *cumulative* filled_qty observation against the
    previous cumulative value already on record. Raises InvalidFillError
    on any violation; callers are expected to treat
    new_filled_qty == previous_filled_qty (a duplicate observation of the
    same event) as a legitimate idempotent no-op *before* calling this --
    this function does not special-case that itself, it only forbids
    regression (new < previous).
    """
    if not _is_finite_number(requested_qty) or requested_qty <= 0:
        raise InvalidFillError(f"requested_qty must be a positive finite number, got {requested_qty!r}")
    if not _is_finite_number(new_filled_qty):
        raise InvalidFillError(f"filled_qty must be a finite number, got {new_filled_qty!r}")
    if new_filled_qty < 0:
        raise InvalidFillError(f"filled_qty must be >= 0, got {new_filled_qty!r}")
    if new_filled_qty > requested_qty:
        raise InvalidFillError(f"filled_qty {new_filled_qty!r} exceeds requested_qty {requested_qty!r}")
    if previous_filled_qty is not None and new_filled_qty < previous_filled_qty:
        raise InvalidFillError(
            f"filled_qty regressed from {previous_filled_qty!r} to {new_filled_qty!r} -- "
            "cumulative fill quantity may never decrease"
        )
    if new_filled_qty > 0:
        if not _is_finite_number(average_fill_price):
            raise InvalidFillError(f"average_fill_price must be a finite number, got {average_fill_price!r}")
        if average_fill_price <= 0:
            raise InvalidFillError(
                f"average_fill_price must be > 0 when filled_qty > 0, got {average_fill_price!r}"
            )


def validate_exit_qty(remaining_qty, exit_qty):
    """Validate a proposed exit fill quantity against a position's current
    remaining_qty (a simpler, non-cumulative check used by the exit path,
    which tracks per-exit fills rather than a single cumulative entry
    fill)."""
    if not _is_finite_number(exit_qty):
        raise InvalidFillError(f"exit fill qty must be a finite number, got {exit_qty!r}")
    if exit_qty < 0:
        raise InvalidFillError(f"exit fill qty must be >= 0, got {exit_qty!r}")
    if exit_qty > remaining_qty:
        raise InvalidFillError(f"exit fill qty {exit_qty!r} exceeds remaining_qty {remaining_qty!r}")


def validate_fill_price(price):
    if not _is_finite_number(price):
        raise InvalidFillError(f"fill price must be a finite number, got {price!r}")
    if price <= 0:
        raise InvalidFillError(f"fill price must be > 0, got {price!r}")
