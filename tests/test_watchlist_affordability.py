"""Watchlist affordability filter tests (percent-of-balance model).

Pure unit tests -- no SQLite, no network, no broker. See
live_readiness/watchlist_affordability.py's module docstring for scope.
"""
import pytest

from live_readiness.watchlist_affordability import (
    STATUS_AFFORDABLE_FRACTIONAL,
    STATUS_AFFORDABLE_WHOLE_SHARE,
    STATUS_BELOW_MINIMUM_ORDER,
    STATUS_INSUFFICIENT_BALANCE,
    STATUS_NOT_FRACTIONABLE,
    STATUS_UNKNOWN_ACCOUNT_STATE,
    AccountState,
    WatchlistCandidate,
    evaluate_affordability,
    filter_watchlist,
)


def _account(**overrides):
    defaults = dict(
        available_cash_krw=30_000,
        cash_usage_percent=100,
        fx_rate_krw_per_usd=1_350.0,
    )
    defaults.update(overrides)
    return AccountState(**defaults)


def _candidate(**overrides):
    defaults = dict(
        symbol="AAPL",
        latest_price_usd=10.0,
        estimated_entry_price_usd=10.0,
        fractionable=False,
    )
    defaults.update(overrides)
    return WatchlistCandidate(**defaults)


# ---------------------------------------------------------------------------
# Account-state validation -- fail-closed on any missing/invalid input.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_cash", [None, "30000", float("nan"), float("inf"), -1])
def test_invalid_cash_blocks_with_unknown_account_state(bad_cash):
    result = evaluate_affordability(_candidate(), _account(available_cash_krw=bad_cash))
    assert result.affordability_status == STATUS_UNKNOWN_ACCOUNT_STATE
    assert not result.is_affordable


@pytest.mark.parametrize("bad_percent", [0, -1, 101, None, "50", float("nan"), float("inf"), True])
def test_invalid_cash_usage_percent_blocks(bad_percent):
    result = evaluate_affordability(_candidate(), _account(cash_usage_percent=bad_percent))
    assert result.affordability_status == STATUS_UNKNOWN_ACCOUNT_STATE


@pytest.mark.parametrize("bad_fx", [None, 0, -1, float("nan"), float("inf")])
def test_invalid_fx_rate_blocks(bad_fx):
    result = evaluate_affordability(_candidate(), _account(fx_rate_krw_per_usd=bad_fx))
    assert result.affordability_status == STATUS_UNKNOWN_ACCOUNT_STATE


def test_cash_lookup_failure_blocks_entire_scan_not_just_one_symbol():
    account = _account(available_cash_krw=None)
    candidates = [_candidate(symbol="AAPL"), _candidate(symbol="MSFT")]
    results = filter_watchlist(candidates, account)
    assert all(r.affordability_status == STATUS_UNKNOWN_ACCOUNT_STATE for r in results)
    assert all(not r.is_affordable for r in results)


# ---------------------------------------------------------------------------
# Core percent-of-balance formula, mirrored from order_gateway.py.
# ---------------------------------------------------------------------------

def test_100_percent_full_balance_whole_share_affordable():
    account = _account(available_cash_krw=30_000, cash_usage_percent=100, fx_rate_krw_per_usd=1_000.0)
    candidate = _candidate(estimated_entry_price_usd=25.0, fractionable=False)
    result = evaluate_affordability(candidate, account)
    assert result.affordability_status == STATUS_AFFORDABLE_WHOLE_SHARE
    assert result.whole_share_affordable
    assert result.max_allocatable_cash_krw == 30_000
    assert result.estimated_order_quantity >= 1


def test_90_percent_caps_allocatable_cash():
    account = _account(available_cash_krw=30_000, cash_usage_percent=90, fx_rate_krw_per_usd=1_000.0)
    result = evaluate_affordability(_candidate(estimated_entry_price_usd=25.0), account)
    assert result.max_allocatable_cash_krw == 27_000
    assert result.available_for_new_order_krw == 27_000


def test_pending_unknown_open_position_exposure_deducted():
    account = _account(
        available_cash_krw=30_000, cash_usage_percent=100, fx_rate_krw_per_usd=1_000.0,
        pending_buy_reservations_krw=10_000, unknown_submission_reservations_krw=5_000,
        current_open_position_cost_krw=5_000,
    )
    result = evaluate_affordability(_candidate(estimated_entry_price_usd=25.0), account)
    assert result.available_for_new_order_krw == 10_000  # 30,000 - 10,000 - 5,000 - 5,000


def test_no_budget_at_all_is_insufficient_balance():
    account = _account(
        available_cash_krw=10_000, cash_usage_percent=100, fx_rate_krw_per_usd=1_000.0,
        current_open_position_cost_krw=10_000,
    )
    result = evaluate_affordability(_candidate(), account)
    assert result.affordability_status == STATUS_INSUFFICIENT_BALANCE
    assert not result.is_affordable


# ---------------------------------------------------------------------------
# fractionable=True: a symbol whose 1-share price exceeds the balance must
# NOT be excluded merely for that reason -- explicit user requirement.
# ---------------------------------------------------------------------------

def test_fractionable_high_price_symbol_kept_as_fractional_candidate():
    account = _account(available_cash_krw=1_000, cash_usage_percent=100, fx_rate_krw_per_usd=1_000.0)
    # 1 share = $10 = 10,000 KRW, far more than the 1,000 KRW balance --
    # but fractionable=True and the fractional order clears the $1 minimum.
    candidate = _candidate(estimated_entry_price_usd=10.0, fractionable=True, minimum_order_amount_usd=0.5)
    result = evaluate_affordability(candidate, account)
    assert result.affordability_status == STATUS_AFFORDABLE_FRACTIONAL
    assert result.fractional_order_affordable
    assert not result.whole_share_affordable
    assert result.estimated_order_quantity > 0


def test_non_fractionable_high_price_symbol_excluded():
    account = _account(available_cash_krw=1_000, cash_usage_percent=100, fx_rate_krw_per_usd=1_000.0)
    candidate = _candidate(estimated_entry_price_usd=10.0, fractionable=False)
    result = evaluate_affordability(candidate, account)
    assert result.affordability_status == STATUS_NOT_FRACTIONABLE
    assert not result.is_affordable
    assert not result.whole_share_affordable
    assert not result.fractional_order_affordable


def test_fractionable_but_below_minimum_order_excluded():
    account = _account(available_cash_krw=100, cash_usage_percent=100, fx_rate_krw_per_usd=1_000.0)
    # budget = $0.10, 1 share = $10 -- fractional value ($0.10) is below
    # even a tiny $0.50 minimum order amount.
    candidate = _candidate(estimated_entry_price_usd=10.0, fractionable=True, minimum_order_amount_usd=0.50)
    result = evaluate_affordability(candidate, account)
    assert result.affordability_status == STATUS_BELOW_MINIMUM_ORDER
    assert not result.is_affordable


def test_whole_share_below_minimum_order_excluded():
    account = _account(available_cash_krw=600, cash_usage_percent=100, fx_rate_krw_per_usd=1_000.0)
    # budget = $0.60, 1 share = $0.50 -- affordable in raw terms, but the
    # order value ($0.50) is below a $1.00 minimum.
    candidate = _candidate(estimated_entry_price_usd=0.50, fractionable=False, minimum_order_amount_usd=1.0)
    result = evaluate_affordability(candidate, account)
    assert result.affordability_status == STATUS_BELOW_MINIMUM_ORDER


# ---------------------------------------------------------------------------
# Slippage is added to the effective price used for sizing.
# ---------------------------------------------------------------------------

def test_slippage_reduces_affordable_quantity():
    account = _account(available_cash_krw=100_000, cash_usage_percent=100, fx_rate_krw_per_usd=1_000.0)
    no_slip = evaluate_affordability(_candidate(estimated_entry_price_usd=10.0, estimated_slippage_usd=0.0), account)
    with_slip = evaluate_affordability(
        _candidate(estimated_entry_price_usd=10.0, estimated_slippage_usd=5.0), account
    )
    assert with_slip.estimated_order_quantity <= no_slip.estimated_order_quantity


# ---------------------------------------------------------------------------
# filter_watchlist: mixed results, order preserved, non-affordable kept in
# results (not silently dropped) so callers can audit exclusions.
# ---------------------------------------------------------------------------

def test_filter_watchlist_preserves_order_and_all_results():
    account = _account(available_cash_krw=30_000, cash_usage_percent=100, fx_rate_krw_per_usd=1_000.0)
    candidates = [
        _candidate(symbol="CHEAP", estimated_entry_price_usd=1.0, fractionable=False),
        _candidate(symbol="EXPENSIVE", estimated_entry_price_usd=1_000_000.0, fractionable=False),
    ]
    results = filter_watchlist(candidates, account)
    assert [r.symbol for r in results] == ["CHEAP", "EXPENSIVE"]
    assert results[0].is_affordable
    assert not results[1].is_affordable


def test_affordable_only_convenience_filtering():
    account = _account(available_cash_krw=30_000, cash_usage_percent=100, fx_rate_krw_per_usd=1_000.0)
    candidates = [
        _candidate(symbol="A", estimated_entry_price_usd=1.0, fractionable=False),
        _candidate(symbol="B", estimated_entry_price_usd=1_000_000.0, fractionable=False),
    ]
    results = filter_watchlist(candidates, account)
    affordable = [r for r in results if r.is_affordable]
    assert [r.symbol for r in affordable] == ["A"]
