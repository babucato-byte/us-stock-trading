import math

import pytest

from account_risk import check_account_exposure_limits
from risk_config import MAX_OPEN_POSITIONS, MAX_TOTAL_EXPOSURE_RATE


def _positions(count, market_value):
    return [{"symbol": f"SYM{i}", "market_value": market_value} for i in range(count)]


def test_allows_order_when_below_position_count_and_exposure_caps():
    positions = _positions(MAX_OPEN_POSITIONS - 1, 100)
    account = {"equity": 100000}
    assert check_account_exposure_limits(positions, account) is True


def test_blocks_when_open_position_count_reaches_cap():
    # Total exposure is trivially small; only the count boundary should trip.
    positions = _positions(MAX_OPEN_POSITIONS, 1)
    account = {"equity": 100000}
    assert check_account_exposure_limits(positions, account) is False


def test_blocks_when_total_exposure_rate_reaches_cap_exactly():
    equity = 100000
    per_position_value = (equity * MAX_TOTAL_EXPOSURE_RATE) / (MAX_OPEN_POSITIONS - 1)
    positions = _positions(MAX_OPEN_POSITIONS - 1, per_position_value)
    account = {"equity": equity}
    assert check_account_exposure_limits(positions, account) is False


def test_blocks_when_total_exposure_rate_exceeds_cap():
    equity = 100000
    positions = _positions(2, equity * 0.4)  # 2 * 0.4 = 0.8 total exposure rate
    account = {"equity": equity}
    assert check_account_exposure_limits(positions, account) is False


@pytest.mark.parametrize(
    "positions, account",
    [
        (None, {"equity": 100000}),
        ("not-a-list", {"equity": 100000}),
        (_positions(1, 100), None),
        (_positions(1, 100), "not-a-dict"),
        (_positions(1, 100), {}),
        (_positions(1, 100), {"equity": None}),
        (_positions(1, 100), {"equity": "not-a-number"}),
        (_positions(1, 100), {"equity": float("nan")}),
        (_positions(1, 100), {"equity": float("inf")}),
        (_positions(1, 100), {"equity": 0}),
        (_positions(1, 100), {"equity": -100000}),
        ([{"symbol": "AAA", "market_value": None}], {"equity": 100000}),
        ([{"symbol": "AAA", "market_value": "not-a-number"}], {"equity": 100000}),
        ([{"symbol": "AAA", "market_value": float("nan")}], {"equity": 100000}),
        ([{"symbol": "AAA", "market_value": -50}], {"equity": 100000}),
        (["not-a-dict-position"], {"equity": 100000}),
    ],
)
def test_fails_closed_on_missing_or_corrupted_data(positions, account):
    assert check_account_exposure_limits(positions, account) is False


def test_allows_order_with_zero_open_positions():
    assert check_account_exposure_limits([], {"equity": 100000}) is True
