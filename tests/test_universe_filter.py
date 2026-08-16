"""T8: account-budget + liquidity universe filter (universe_filter.py).

Pure calculation tests -- no broker, no network, no file I/O except the
scanner-rules reader, which is pointed at tmp_path.
"""

import json

import pytest

import universe_filter as uf
from live_readiness.trusted_operator_config import get_cash_usage_percent
from risk_config import MAX_POSITION_RATE

THRESHOLDS = uf.ScannerThresholds(
    min_price_usd=5.0, min_avg_dollar_volume_usd=20_000_000.0, source="test",
)


def _budget(cash=10_000.0, **overrides):
    kwargs = dict(available_cash_usd=cash, as_of="2026-08-06T00:00:00+00:00", source="kis_balance")
    kwargs.update(overrides)
    return uf.UniverseBudget(**kwargs)


def _row(symbol="AAPL", exchange="NASDAQ", **overrides):
    row = {"symbol": symbol, "name": "Test", "exchange": exchange,
           "tradable": True, "shortable": True}
    row.update(overrides)
    return row


def _metrics(symbol="AAPL", price=100.0, dollar_volume=100_000_000.0):
    return uf.SymbolMetrics(symbol=symbol, price_usd=price, avg_dollar_volume_usd=dollar_volume)


# -- ceiling formula ----------------------------------------------------

def test_price_ceiling_is_cash_times_usage_percent_times_position_rate():
    budget = _budget(cash=10_000.0)
    expected = 10_000.0 * (get_cash_usage_percent() / 100.0) * MAX_POSITION_RATE
    assert budget.price_ceiling_usd == pytest.approx(expected)
    # The trusted operator percent makes the ceiling strictly tighter than
    # the position ratio alone -- fail-safe direction, never looser.
    assert budget.price_ceiling_usd < 10_000.0 * MAX_POSITION_RATE


def test_caller_cash_usage_percent_can_only_lower_never_raise():
    trusted = get_cash_usage_percent()
    lower = _budget(cash_usage_percent=10.0)
    higher = _budget(cash_usage_percent=100.0)
    assert lower.effective_cash_usage_percent == 10.0
    assert higher.effective_cash_usage_percent == trusted


@pytest.mark.parametrize("bad_cash", [None, float("nan"), float("inf"), -1.0, "1000", True])
def test_unusable_cash_is_a_validation_error(bad_cash):
    assert _budget(cash=bad_cash).validation_error() is not None


@pytest.mark.parametrize("bad_rate", [0, -0.1, 1.5, float("nan"), None])
def test_unusable_position_rate_is_a_validation_error(bad_rate):
    assert _budget(position_rate=bad_rate).validation_error() is not None


def test_filter_universe_refuses_to_run_on_an_unusable_budget():
    with pytest.raises(uf.UniverseFilterError):
        uf.filter_universe([_row()], {"AAPL": _metrics()}, _budget(cash=-1.0), THRESHOLDS)


# -- whole-share arithmetic ---------------------------------------------

@pytest.mark.parametrize("ceiling,price,expected", [
    (900.0, 100.0, 9),
    (900.0, 899.99, 1),
    (900.0, 900.0, 1),
    (900.0, 900.01, 0),      # one whole share does not fit -> excluded
    (900.0, 350.0, 2),       # 2.57 shares -> floor, never round up
    (0.0, 10.0, 0),
])
def test_max_affordable_whole_shares_floors(ceiling, price, expected):
    assert uf.max_affordable_whole_shares(ceiling, price) == expected


@pytest.mark.parametrize("bad", [None, 0, -5.0, float("nan"), float("inf"), "10", True])
def test_max_affordable_whole_shares_rejects_unusable_price(bad):
    assert uf.max_affordable_whole_shares(900.0, bad) == 0


# -- per-symbol decisions -----------------------------------------------

def test_affordable_liquid_symbol_is_included():
    budget = _budget(cash=10_000.0)  # ceiling 900
    decision = uf.evaluate_symbol(_row(), _metrics(price=100.0), budget, THRESHOLDS)
    assert decision.included
    assert decision.reason == uf.REASON_INCLUDED
    assert decision.max_affordable_shares == 9
    assert decision.exchange == "NASDAQ"
    assert decision.price_ceiling_usd == pytest.approx(900.0)


def test_price_above_ceiling_is_excluded_with_its_own_reason():
    budget = _budget(cash=10_000.0)  # ceiling 900
    decision = uf.evaluate_symbol(_row(), _metrics(price=901.0), budget, THRESHOLDS)
    assert not decision.included
    assert decision.reason == uf.REASON_PRICE_ABOVE_BUDGET
    assert decision.max_affordable_shares == 0


def test_price_below_scanner_floor_is_excluded():
    decision = uf.evaluate_symbol(_row(), _metrics(price=4.99), _budget(), THRESHOLDS)
    assert decision.reason == uf.REASON_PRICE_BELOW_FLOOR


def test_illiquid_symbol_is_excluded_even_when_affordable():
    decision = uf.evaluate_symbol(
        _row(), _metrics(price=10.0, dollar_volume=19_999_999.0), _budget(), THRESHOLDS)
    assert decision.reason == uf.REASON_ILLIQUID


def test_liquidity_exactly_at_the_floor_is_included():
    decision = uf.evaluate_symbol(
        _row(), _metrics(price=10.0, dollar_volume=20_000_000.0), _budget(), THRESHOLDS)
    assert decision.included


@pytest.mark.parametrize("exchange", ["OTC", "ARCA", "BATS", "", None, "NYSE ARCA"])
def test_unsupported_exchange_is_excluded_before_any_price_check(exchange):
    decision = uf.evaluate_symbol(
        _row(exchange=exchange), _metrics(price=10.0), _budget(), THRESHOLDS)
    assert decision.reason == uf.REASON_UNSUPPORTED_EXCHANGE
    assert decision.exchange is None


def test_missing_metrics_is_no_price_data_not_a_silent_include():
    decision = uf.evaluate_symbol(_row(), None, _budget(), THRESHOLDS)
    assert not decision.included
    assert decision.reason == uf.REASON_NO_PRICE_DATA


@pytest.mark.parametrize("bad_price", [None, 0.0, -1.0, float("nan"), float("inf")])
def test_unusable_price_is_no_price_data(bad_price):
    decision = uf.evaluate_symbol(
        _row(), uf.SymbolMetrics("AAPL", bad_price, 100_000_000.0), _budget(), THRESHOLDS)
    assert decision.reason == uf.REASON_NO_PRICE_DATA


@pytest.mark.parametrize("bad_volume", [None, -1.0, float("nan"), float("inf")])
def test_unusable_liquidity_is_its_own_reason(bad_volume):
    decision = uf.evaluate_symbol(
        _row(), uf.SymbolMetrics("AAPL", 10.0, bad_volume), _budget(), THRESHOLDS)
    assert decision.reason == uf.REASON_NO_LIQUIDITY_DATA


def test_zero_cash_excludes_everything_rather_than_erroring():
    decisions = uf.filter_universe(
        [_row("AAPL"), _row("MSFT")],
        {"AAPL": _metrics("AAPL"), "MSFT": _metrics("MSFT")},
        _budget(cash=0.0), THRESHOLDS,
    )
    assert [d.reason for d in decisions] == [uf.REASON_PRICE_ABOVE_BUDGET] * 2


def test_every_input_row_produces_exactly_one_decision():
    rows = [_row("AAPL"), _row("MSFT"), _row("PENNY"), _row("OTCX", exchange="OTC")]
    metrics = {
        "AAPL": _metrics("AAPL", price=100.0),
        "MSFT": _metrics("MSFT", price=100_000.0),
        "PENNY": _metrics("PENNY", price=1.0),
    }
    decisions = uf.filter_universe(rows, metrics, _budget(), THRESHOLDS)
    assert len(decisions) == len(rows)
    assert [d.symbol for d in decisions] == ["AAPL", "MSFT", "PENNY", "OTCX"]


# -- summary / report ---------------------------------------------------

def test_summary_counts_every_reason_including_zeroes():
    rows = [_row("AAPL"), _row("MSFT"), _row("OTCX", exchange="OTC")]
    metrics = {"AAPL": _metrics("AAPL", 100.0), "MSFT": _metrics("MSFT", 100_000.0)}
    budget = _budget()
    decisions = uf.filter_universe(rows, metrics, budget, THRESHOLDS)
    summary = uf.summarize(decisions, budget=budget, thresholds=THRESHOLDS)

    assert summary.total == 3
    assert summary.included == 1
    assert summary.excluded == 2
    assert set(summary.reason_counts) >= set(uf.ALL_REASONS)
    assert summary.reason_counts[uf.REASON_INCLUDED] == 1
    assert summary.reason_counts[uf.REASON_PRICE_ABOVE_BUDGET] == 1
    assert summary.reason_counts[uf.REASON_UNSUPPORTED_EXCHANGE] == 1
    assert summary.reason_counts[uf.REASON_ILLIQUID] == 0
    assert summary.budget_source == "kis_balance"
    assert summary.price_ceiling_usd == pytest.approx(budget.price_ceiling_usd)


def test_summary_reason_counts_sum_to_total():
    rows = [_row(f"S{i}") for i in range(5)]
    metrics = {f"S{i}": _metrics(f"S{i}", price=10.0 * (i + 1)) for i in range(5)}
    budget = _budget()
    summary = uf.summarize(uf.filter_universe(rows, metrics, budget, THRESHOLDS), budget=budget)
    assert sum(summary.reason_counts.values()) == summary.total == 5


def test_format_summary_lines_mentions_every_reason():
    summary = uf.summarize([], budget=_budget(), thresholds=THRESHOLDS)
    text = "\n".join(uf.format_summary_lines(summary))
    for reason in uf.ALL_REASONS:
        assert reason in text


# -- scanner threshold reuse --------------------------------------------

def test_thresholds_are_read_from_the_scanner_rules_file(tmp_path):
    rules = tmp_path / "scanner_rules.json"
    rules.write_text(json.dumps({"filters": [
        {"field": "price", "operator": ">=", "value": 7},
        {"field": "avg_dollar_volume", "operator": ">=", "value": 33_000_000},
    ]}), encoding="utf-8")
    thresholds = uf.load_scanner_thresholds(rules)
    assert thresholds.min_price_usd == 7.0
    assert thresholds.min_avg_dollar_volume_usd == 33_000_000.0


def test_repo_scanner_rules_file_is_actually_readable():
    """Guards against the shipped rules file drifting into a shape this
    module silently falls back on."""
    thresholds = uf.load_scanner_thresholds()
    assert thresholds.source.endswith("scanner_rules.json")
    assert thresholds.min_price_usd == 5.0
    assert thresholds.min_avg_dollar_volume_usd == 20_000_000.0


def test_non_ge_operator_is_not_mirrored(tmp_path):
    rules = tmp_path / "scanner_rules.json"
    rules.write_text(json.dumps({"filters": [
        {"field": "price", "operator": "between", "min": 1, "max": 2},
    ]}), encoding="utf-8")
    thresholds = uf.load_scanner_thresholds(rules)
    assert thresholds.min_price_usd == uf.DEFAULT_MIN_PRICE_USD


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"filters": "nope"}'])
def test_corrupt_rules_file_falls_back_to_documented_defaults(tmp_path, content):
    rules = tmp_path / "scanner_rules.json"
    rules.write_text(content, encoding="utf-8")
    thresholds = uf.load_scanner_thresholds(rules)
    assert thresholds.min_price_usd == uf.DEFAULT_MIN_PRICE_USD
    assert thresholds.min_avg_dollar_volume_usd == uf.DEFAULT_MIN_AVG_DOLLAR_VOLUME_USD


def test_missing_rules_file_falls_back(tmp_path):
    thresholds = uf.load_scanner_thresholds(tmp_path / "nope.json")
    assert thresholds.source == "defaults"
