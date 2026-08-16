"""CODEX-027: fill validation tests (pure functions, no I/O)."""
import math

import pytest

from positions.fill_validation import (
    InvalidFillError,
    validate_cumulative_fill,
    validate_exit_qty,
    validate_fill_price,
)


def test_valid_partial_fill_accepted():
    validate_cumulative_fill(requested_qty=100, previous_filled_qty=0, new_filled_qty=40, average_fill_price=10.0)


def test_valid_full_fill_accepted():
    validate_cumulative_fill(requested_qty=100, previous_filled_qty=40, new_filled_qty=100, average_fill_price=10.0)


def test_negative_filled_qty_rejected():
    with pytest.raises(InvalidFillError):
        validate_cumulative_fill(100, 0, -3, 10.0)


def test_nan_filled_qty_rejected():
    with pytest.raises(InvalidFillError):
        validate_cumulative_fill(100, 0, float("nan"), 10.0)


def test_infinity_filled_qty_rejected():
    with pytest.raises(InvalidFillError):
        validate_cumulative_fill(100, 0, float("inf"), 10.0)


def test_filled_qty_exceeding_requested_rejected():
    with pytest.raises(InvalidFillError):
        validate_cumulative_fill(100, 0, 150, 10.0)


def test_filled_qty_regression_rejected():
    with pytest.raises(InvalidFillError):
        validate_cumulative_fill(100, 60, 40, 10.0)


def test_negative_price_rejected():
    with pytest.raises(InvalidFillError):
        validate_cumulative_fill(100, 0, 10, -5.0)


def test_zero_price_with_positive_qty_rejected():
    with pytest.raises(InvalidFillError):
        validate_cumulative_fill(100, 0, 10, 0.0)


def test_nan_price_rejected():
    with pytest.raises(InvalidFillError):
        validate_cumulative_fill(100, 0, 10, float("nan"))


def test_bool_quantity_rejected():
    with pytest.raises(InvalidFillError):
        validate_cumulative_fill(100, 0, True, 10.0)


def test_string_quantity_rejected():
    with pytest.raises(InvalidFillError):
        validate_cumulative_fill(100, 0, "40", 10.0)


def test_zero_fill_with_no_price_is_allowed():
    # A cumulative fill of 0 (nothing filled yet) doesn't require a valid price.
    validate_cumulative_fill(100, 0, 0, None)


def test_same_cumulative_fill_repeated_is_not_itself_rejected_by_validator():
    # The validator allows new == previous (idempotency is the *caller's*
    # responsibility to detect and short-circuit before calling this).
    validate_cumulative_fill(100, 40, 40, 10.0)


def test_validate_exit_qty_negative_rejected():
    with pytest.raises(InvalidFillError):
        validate_exit_qty(remaining_qty=50, exit_qty=-1)


def test_validate_exit_qty_exceeding_remaining_rejected():
    with pytest.raises(InvalidFillError):
        validate_exit_qty(remaining_qty=50, exit_qty=51)


def test_validate_exit_qty_nan_rejected():
    with pytest.raises(InvalidFillError):
        validate_exit_qty(remaining_qty=50, exit_qty=float("nan"))


def test_validate_exit_qty_valid_accepted():
    validate_exit_qty(remaining_qty=50, exit_qty=50)
    validate_exit_qty(remaining_qty=50, exit_qty=0)


def test_validate_fill_price_negative_rejected():
    with pytest.raises(InvalidFillError):
        validate_fill_price(-1.0)


def test_validate_fill_price_infinity_rejected():
    with pytest.raises(InvalidFillError):
        validate_fill_price(float("inf"))


def test_validate_fill_price_valid_accepted():
    validate_fill_price(10.5)
