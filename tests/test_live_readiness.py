"""Stage 10: 30,000 KRW limited-live-trading preparation tests.

Pure functions only -- no file I/O, no network, no operational state.
"""
import pytest

from live_readiness.allowlist import is_symbol_allowed
from live_readiness.sizing import (
    STATUS_BELOW_MINIMUM_ORDER_AMOUNT,
    STATUS_INSUFFICIENT_FUNDS,
    STATUS_OK,
    InvalidSizingInputError,
    calculate_micro_order_quantity,
)

KRW_30000 = 30_000
FX_RATE = 1_350.0  # illustrative only, not a real/current rate -- see playbook TBD_OPERATOR


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------

def test_typical_pilot_budget_affords_whole_shares_of_a_cheap_stock():
    result = calculate_micro_order_quantity(KRW_30000, FX_RATE, share_price_usd=5.0)
    assert result.status == STATUS_OK
    assert result.quantity == 4  # $22.22 budget // $5.00 = 4 shares
    assert result.estimated_cost_usd == pytest.approx(20.0)


def test_expensive_stock_yields_insufficient_funds_not_zero_silently():
    result = calculate_micro_order_quantity(KRW_30000, FX_RATE, share_price_usd=1000.0)
    assert result.status == STATUS_INSUFFICIENT_FUNDS
    assert result.quantity == 0
    assert "cannot afford" in result.reason


def test_affordable_quantity_below_minimum_order_amount_is_blocked():
    # Tiny KRW budget affords exactly 1 share of a very cheap stock, but that
    # 1-share order's total value still doesn't clear the broker's minimum.
    result = calculate_micro_order_quantity(100, FX_RATE, share_price_usd=0.05, min_order_amount_usd=1.0)
    assert result.status == STATUS_BELOW_MINIMUM_ORDER_AMOUNT
    assert result.quantity == 0


def test_fractional_shares_disabled_by_default_rounds_down():
    result = calculate_micro_order_quantity(KRW_30000, FX_RATE, share_price_usd=7.0)
    budget = KRW_30000 / FX_RATE
    expected_whole = int(budget // 7.0)
    assert result.quantity == expected_whole
    assert isinstance(result.quantity, int)


def test_fractional_shares_allowed_returns_non_integer_quantity():
    result = calculate_micro_order_quantity(KRW_30000, FX_RATE, share_price_usd=1000.0,
                                             fractional_shares_allowed=True, min_order_amount_usd=1.0)
    assert result.status == STATUS_OK
    assert 0 < result.quantity < 1  # affordable fraction of one $1000 share


def test_zero_or_negative_inputs_raise_not_silently_return_zero():
    with pytest.raises(InvalidSizingInputError):
        calculate_micro_order_quantity(0, FX_RATE, 5.0)
    with pytest.raises(InvalidSizingInputError):
        calculate_micro_order_quantity(KRW_30000, 0, 5.0)
    with pytest.raises(InvalidSizingInputError):
        calculate_micro_order_quantity(KRW_30000, FX_RATE, 0)
    with pytest.raises(InvalidSizingInputError):
        calculate_micro_order_quantity(-100, FX_RATE, 5.0)


def test_budget_usd_reported_regardless_of_outcome():
    result = calculate_micro_order_quantity(KRW_30000, FX_RATE, share_price_usd=1000.0)
    assert result.budget_usd == pytest.approx(KRW_30000 / FX_RATE)


# ---------------------------------------------------------------------------
# Allow-list -- fail-closed
# ---------------------------------------------------------------------------

def test_empty_allow_list_permits_nothing():
    assert is_symbol_allowed("AAPL", []) is False
    assert is_symbol_allowed("AAPL", None) is False


def test_symbol_in_allow_list_permitted():
    assert is_symbol_allowed("AAPL", ["AAPL", "MSFT"]) is True


def test_symbol_not_in_allow_list_blocked():
    assert is_symbol_allowed("TSLA", ["AAPL", "MSFT"]) is False


def test_allow_list_check_is_case_and_whitespace_insensitive():
    assert is_symbol_allowed(" aapl ", ["AAPL"]) is True
    assert is_symbol_allowed("AAPL", [" aapl "]) is True


def test_empty_symbol_never_permitted_even_with_populated_allow_list():
    assert is_symbol_allowed("", ["AAPL"]) is False
    assert is_symbol_allowed("   ", ["AAPL"]) is False
