"""live_readiness/sizing_engine.py unit tests."""
import pytest

from live_readiness import sizing_engine as se


# --- apply_entry_price_buffer ---

def test_buffer_zero_returns_unchanged_price():
    assert se.apply_entry_price_buffer(10.0) == pytest.approx(10.0)


def test_buffer_bps_increases_price():
    assert se.apply_entry_price_buffer(100.0, buffer_bps=50) == pytest.approx(100.5)


def test_slippage_increases_price():
    assert se.apply_entry_price_buffer(10.0, slippage_usd=0.05) == pytest.approx(10.05)


def test_buffer_and_slippage_combine():
    result = se.apply_entry_price_buffer(100.0, buffer_bps=100, slippage_usd=0.10)
    assert result == pytest.approx(100 * 1.01 + 0.10)


@pytest.mark.parametrize("bad_price", [None, 0, -1, float("nan"), float("inf"), "10", True])
def test_invalid_price_blocked(bad_price):
    with pytest.raises(se.SizingEngineError):
        se.apply_entry_price_buffer(bad_price)


@pytest.mark.parametrize("bad_value", [None, -1, float("nan"), float("inf"), "1", True])
def test_invalid_buffer_bps_blocked(bad_value):
    with pytest.raises(se.SizingEngineError):
        se.apply_entry_price_buffer(10.0, buffer_bps=bad_value)


# --- compute_sizing_decision: happy paths ---

def test_whole_share_balance_binds():
    decision = se.compute_sizing_decision(
        13_500.0, 10.0, 1_350.0, False, risk_based_qty=100, strategy_max_qty=100,
    )
    assert decision.actual_qty == 1  # floor(13500/1350/10) = 1
    assert decision.balance_based_qty == 1


def test_fractional_balance_qty():
    decision = se.compute_sizing_decision(
        6_750.0, 10.0, 1_350.0, True, risk_based_qty=100, strategy_max_qty=100,
    )
    assert decision.balance_based_qty == pytest.approx(0.5)
    assert decision.actual_qty == pytest.approx(0.5)


def test_risk_qty_binds_when_smaller():
    decision = se.compute_sizing_decision(
        1_000_000.0, 10.0, 1_350.0, False, risk_based_qty=2, strategy_max_qty=100,
    )
    assert decision.actual_qty == 2


def test_strategy_cap_binds_when_smaller():
    decision = se.compute_sizing_decision(
        1_000_000.0, 10.0, 1_350.0, False, risk_based_qty=100, strategy_max_qty=3,
    )
    assert decision.actual_qty == 3


def test_no_strategy_cap_means_unconstrained():
    decision = se.compute_sizing_decision(
        27_000.0, 10.0, 1_350.0, False, risk_based_qty=100, strategy_max_qty=None,
    )
    assert decision.strategy_max_qty is None
    assert decision.actual_qty == 2  # balance binds: floor(27000/1350/10)=2


def test_actual_qty_takes_the_minimum_of_all_three():
    decision = se.compute_sizing_decision(
        1_000_000.0, 10.0, 1_350.0, False, risk_based_qty=5, strategy_max_qty=3,
    )
    assert decision.actual_qty == 3


# --- negative/zero/below-minimum budgets ---

def test_negative_available_for_new_order_clamped_to_zero_qty():
    decision = se.compute_sizing_decision(
        -5_000.0, 10.0, 1_350.0, False, risk_based_qty=100, strategy_max_qty=100,
    )
    assert decision.actual_qty == 0
    assert decision.balance_based_qty == 0


def test_below_minimum_order_amount_zeroes_balance_qty():
    decision = se.compute_sizing_decision(
        700.0, 10.0, 1_350.0, True, risk_based_qty=100, strategy_max_qty=100,
        min_order_amount_usd=1.0,
    )
    # budget_usd ~= 0.518, well below the $1 minimum
    assert decision.below_minimum_order
    assert decision.balance_based_qty == 0
    assert decision.actual_qty == 0


# --- fail-closed validation ---

_INVALID_QTY = [None, float("nan"), float("inf"), -1, "5", True]


@pytest.mark.parametrize("bad_value", _INVALID_QTY)
def test_invalid_risk_based_qty_blocked(bad_value):
    with pytest.raises(se.SizingEngineError, match="risk_based_qty"):
        se.compute_sizing_decision(27_000.0, 10.0, 1_350.0, False, risk_based_qty=bad_value)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), -1, "5", True, 0])
def test_invalid_strategy_max_qty_blocked(bad_value):
    with pytest.raises(se.SizingEngineError, match="strategy_max_qty"):
        se.compute_sizing_decision(
            27_000.0, 10.0, 1_350.0, False, risk_based_qty=100, strategy_max_qty=bad_value,
        )


@pytest.mark.parametrize("bad_value", [None, 0, -1, float("nan"), float("inf"), "10", True])
def test_invalid_buffered_entry_price_blocked(bad_value):
    with pytest.raises(se.SizingEngineError, match="buffered_entry_price_usd"):
        se.compute_sizing_decision(27_000.0, bad_value, 1_350.0, False, risk_based_qty=100)


@pytest.mark.parametrize("bad_value", [None, 0, -1, float("nan"), float("inf"), "1350", True])
def test_invalid_fx_rate_blocked(bad_value):
    with pytest.raises(se.SizingEngineError, match="fx_rate_krw_per_usd"):
        se.compute_sizing_decision(27_000.0, 10.0, bad_value, False, risk_based_qty=100)


@pytest.mark.parametrize("bad_value", [None, float("nan"), float("inf"), "27000", True])
def test_invalid_available_for_new_order_blocked(bad_value):
    with pytest.raises(se.SizingEngineError, match="available_for_new_order_krw"):
        se.compute_sizing_decision(bad_value, 10.0, 1_350.0, False, risk_based_qty=100)


# CODEX-037-style repro: NaN cap must never silently be ignored and allow
# an unconstrained order through.
def test_codex037_style_nan_risk_qty_blocked_no_partial_result():
    with pytest.raises(se.SizingEngineError):
        se.compute_sizing_decision(
            13_500.0, 10.0, 1_350.0, True, risk_based_qty=float("nan"), strategy_max_qty=100,
        )


def test_codex037_style_nan_strategy_cap_blocked_no_partial_result():
    with pytest.raises(se.SizingEngineError):
        se.compute_sizing_decision(
            13_500.0, 10.0, 1_350.0, True, risk_based_qty=100, strategy_max_qty=float("nan"),
        )
