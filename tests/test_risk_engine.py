"""live_readiness/risk_engine.py unit tests."""
import math

import pytest

from live_readiness import risk_engine as re


def test_valid_inputs_produce_whole_share_qty():
    decision = re.compute_risk_decision(10.0, 9.0, 1_350.0, 13_500.0)
    # risk_per_share = 1 * 1350 = 1350 KRW; budget 13,500 -> qty 10
    assert decision.risk_based_qty == 10
    assert decision.stop_distance_usd == pytest.approx(1.0)
    assert decision.risk_decision_id.startswith("risk-")


def test_fractional_allowed_produces_fractional_qty():
    decision = re.compute_risk_decision(10.0, 9.0, 1_350.0, 1_350.0, fractional_shares_allowed=True)
    assert decision.risk_based_qty == pytest.approx(1.0)


def test_max_risk_per_trade_tightens_budget():
    decision = re.compute_risk_decision(10.0, 9.0, 1_350.0, 13_500.0, max_risk_per_trade_krw=1_350.0)
    assert decision.risk_based_qty == 1


def test_max_risk_per_trade_looser_than_daily_remaining_does_not_loosen():
    decision = re.compute_risk_decision(10.0, 9.0, 1_350.0, 1_350.0, max_risk_per_trade_krw=1_000_000.0)
    assert decision.risk_based_qty == 1


def test_zero_daily_loss_remaining_gives_zero_qty():
    decision = re.compute_risk_decision(10.0, 9.0, 1_350.0, 0.0)
    assert decision.risk_based_qty == 0


def test_negative_daily_loss_remaining_gives_zero_qty_not_negative():
    decision = re.compute_risk_decision(10.0, 9.0, 1_350.0, -5_000.0)
    assert decision.risk_based_qty == 0


def test_stop_price_not_below_entry_price_blocked():
    with pytest.raises(re.RiskEngineError, match="no defined risk"):
        re.compute_risk_decision(10.0, 10.0, 1_350.0, 13_500.0)
    with pytest.raises(re.RiskEngineError, match="no defined risk"):
        re.compute_risk_decision(10.0, 11.0, 1_350.0, 13_500.0)


_INVALID_NUMBERS = [None, float("nan"), float("inf"), float("-inf"), "10", True, False]


@pytest.mark.parametrize("bad_value", _INVALID_NUMBERS)
def test_invalid_entry_price_blocked(bad_value):
    with pytest.raises(re.RiskEngineError, match="entry_price_usd"):
        re.compute_risk_decision(bad_value, 9.0, 1_350.0, 13_500.0)


@pytest.mark.parametrize("bad_value", _INVALID_NUMBERS)
def test_invalid_stop_price_blocked(bad_value):
    with pytest.raises(re.RiskEngineError, match="stop_price_usd"):
        re.compute_risk_decision(10.0, bad_value, 1_350.0, 13_500.0)


@pytest.mark.parametrize("bad_value", [None, float("nan"), float("inf"), "1350", True])
def test_invalid_fx_rate_blocked(bad_value):
    with pytest.raises(re.RiskEngineError, match="fx_rate_krw_per_usd"):
        re.compute_risk_decision(10.0, 9.0, bad_value, 13_500.0)


@pytest.mark.parametrize("bad_value", [None, float("nan"), float("inf"), float("-inf"), "1", True])
def test_invalid_daily_loss_remaining_blocked(bad_value):
    with pytest.raises(re.RiskEngineError, match="daily_loss_remaining_krw"):
        re.compute_risk_decision(10.0, 9.0, 1_350.0, bad_value)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), -1, 0, "1350", True])
def test_invalid_max_risk_per_trade_blocked(bad_value):
    with pytest.raises(re.RiskEngineError, match="max_risk_per_trade_krw"):
        re.compute_risk_decision(10.0, 9.0, 1_350.0, 13_500.0, max_risk_per_trade_krw=bad_value)


def test_zero_entry_price_blocked():
    with pytest.raises(re.RiskEngineError, match="entry_price_usd"):
        re.compute_risk_decision(0, 9.0, 1_350.0, 13_500.0)


def test_negative_stop_price_blocked():
    with pytest.raises(re.RiskEngineError, match="stop_price_usd"):
        re.compute_risk_decision(10.0, -1.0, 1_350.0, 13_500.0)


# --- compute_daily_loss_remaining_krw ---

def test_daily_loss_remaining_basic():
    assert re.compute_daily_loss_remaining_krw(10_000.0, 3_000.0) == pytest.approx(7_000.0)


def test_daily_loss_remaining_can_go_negative_when_exceeded():
    assert re.compute_daily_loss_remaining_krw(10_000.0, 15_000.0) == pytest.approx(-5_000.0)


@pytest.mark.parametrize("bad_value", [None, float("nan"), float("inf"), -1, 0, "1", True])
def test_daily_loss_remaining_invalid_max_blocked(bad_value):
    with pytest.raises(re.RiskEngineError):
        re.compute_daily_loss_remaining_krw(bad_value, 1_000.0)


@pytest.mark.parametrize("bad_value", [None, float("nan"), float("inf"), "1", True, -1])
def test_daily_loss_remaining_invalid_current_blocked(bad_value):
    with pytest.raises(re.RiskEngineError):
        re.compute_daily_loss_remaining_krw(10_000.0, bad_value)
