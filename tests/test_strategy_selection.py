"""Stage 8: strategy selection engine tests."""
import pytest

from backtest.models import STATUS_INSUFFICIENT_DATA, STATUS_OK, BacktestResult, CostBreakdown, Trade
from strategy.interface import STATE_ENTRY_SIGNAL, EvaluationResult, TradingStrategy
from strategy.status import ACTIVE, COLLECTED, PAUSED, REJECTED, REVIEWED, STRUCTURED
from strategy_selection import scoring
from strategy_selection.engine import MIN_TRADES_FOR_SCORING, select_strategy
from strategy_selection.models import (
    DISABLED,
    INSUFFICIENT_DATA,
    MARKET_MISMATCH,
    NOT_SELECTED,
    SELECTED,
    SelectionFactors,
    SelectionInput,
)


class FakeStrategy(TradingStrategy):
    def __init__(self, strategy_id, status):
        super().__init__(strategy_id=strategy_id, version="1.0.0", status=status)

    def evaluate_setup(self, bars, *, symbol, as_of=None):
        raise NotImplementedError

    def generate_entry(self, bars, *, symbol, as_of=None):
        raise NotImplementedError

    def calculate_stop(self, bars, *, entry_price):
        raise NotImplementedError

    def calculate_targets(self, *, entry_price, stop_price):
        raise NotImplementedError

    def invalidate(self, bars, *, symbol):
        raise NotImplementedError


def _trade(pnl, r=1.0, entry_time="2026-07-21T09:30:00-04:00"):
    return Trade(
        symbol="AAPL", strategy_id="S", strategy_version="1.0", signal_time=entry_time, signal_price=100.0,
        entry_time=entry_time, entry_price=100.0, entry_session="regular", entry_bar_volume=100_000,
        stop_price=99.0, target_1_price=101.0, target_2_price=102.0, requested_qty=100, filled_qty=100,
        exit_events=[], exit_reason="TEST", realized_pnl=pnl, r_multiple=r, costs=CostBreakdown(),
    )


def _good_backtest_result(strategy_id="S", symbol="AAPL", num_trades=30, win_pnl=10, r=1.5):
    trades = [_trade(win_pnl, r) for _ in range(num_trades)]
    return BacktestResult(strategy_id=strategy_id, strategy_version="1.0", symbol=symbol,
                           status=STATUS_OK, trades=trades, bars_evaluated=600)


def _candidate(strategy_id="S", status=REVIEWED, symbol="AAPL", backtest_result="default", **overrides):
    if backtest_result == "default":
        backtest_result = _good_backtest_result(strategy_id=strategy_id, symbol=symbol)
    strategy = FakeStrategy(strategy_id, status)
    defaults = dict(strategy=strategy, symbol=symbol, backtest_result=backtest_result)
    defaults.update(overrides)
    return SelectionInput(**defaults)


# ---------------------------------------------------------------------------
# Eligibility gating
# ---------------------------------------------------------------------------

def test_rejected_strategy_is_disabled():
    candidate = _candidate(status=REJECTED)
    results = select_strategy([candidate])
    assert results[0].state == DISABLED


def test_paused_strategy_is_disabled():
    candidate = _candidate(status=PAUSED)
    results = select_strategy([candidate])
    assert results[0].state == DISABLED


def test_collected_strategy_is_insufficient_data():
    candidate = _candidate(status=COLLECTED, backtest_result=None)
    results = select_strategy([candidate])
    assert results[0].state == INSUFFICIENT_DATA


def test_no_backtest_result_is_insufficient_data():
    candidate = _candidate(status=REVIEWED, backtest_result=None)
    results = select_strategy([candidate])
    assert results[0].state == INSUFFICIENT_DATA


def test_backtest_status_insufficient_data_propagates():
    bt = BacktestResult(strategy_id="S", strategy_version="1.0", symbol="AAPL",
                         status=STATUS_INSUFFICIENT_DATA, trades=[], bars_evaluated=10, reason="too few bars")
    candidate = _candidate(status=REVIEWED, backtest_result=bt)
    results = select_strategy([candidate])
    assert results[0].state == INSUFFICIENT_DATA


def test_too_few_backtest_trades_is_insufficient_data():
    bt = _good_backtest_result(num_trades=MIN_TRADES_FOR_SCORING - 1)
    candidate = _candidate(status=REVIEWED, backtest_result=bt)
    results = select_strategy([candidate])
    assert results[0].state == INSUFFICIENT_DATA


def test_market_mismatch_for_known_strategy():
    candidate = _candidate(strategy_id="VWAP_MICRO_PULLBACK_MOMENTUM_V1", status=REVIEWED,
                            backtest_result=_good_backtest_result(strategy_id="VWAP_MICRO_PULLBACK_MOMENTUM_V1"))
    results = select_strategy([candidate], market_state="premarket")
    assert results[0].state == MARKET_MISMATCH


def test_regular_market_state_no_mismatch_for_known_strategy():
    candidate = _candidate(strategy_id="VWAP_MICRO_PULLBACK_MOMENTUM_V1", status=REVIEWED,
                            backtest_result=_good_backtest_result(strategy_id="VWAP_MICRO_PULLBACK_MOMENTUM_V1"))
    results = select_strategy([candidate], market_state="regular")
    assert results[0].state == SELECTED


def test_unknown_strategy_id_has_no_market_gate():
    candidate = _candidate(strategy_id="SOME_NEW_STRATEGY", status=REVIEWED,
                            backtest_result=_good_backtest_result(strategy_id="SOME_NEW_STRATEGY"))
    results = select_strategy([candidate], market_state="premarket")
    assert results[0].state == SELECTED  # no table entry -> no mismatch gate applied


# ---------------------------------------------------------------------------
# Selection among multiple eligible candidates
# ---------------------------------------------------------------------------

def test_single_eligible_candidate_is_selected():
    candidate = _candidate()
    results = select_strategy([candidate])
    assert results[0].state == SELECTED
    assert results[0].composite_score is not None


def test_only_one_candidate_selected_among_many():
    strong = _candidate(strategy_id="STRONG", backtest_result=_good_backtest_result(strategy_id="STRONG", r=3.0))
    weak = _candidate(strategy_id="WEAK", backtest_result=_good_backtest_result(strategy_id="WEAK", win_pnl=1, r=0.1))
    results = select_strategy([weak, strong])
    states = {r.strategy_id: r.state for r in results}
    assert states["STRONG"] == SELECTED
    assert states["WEAK"] == NOT_SELECTED
    assert sum(1 for r in results if r.state == SELECTED) == 1


def test_disabled_and_insufficient_candidates_never_selected_even_if_alone():
    disabled = _candidate(strategy_id="D", status=REJECTED)
    results = select_strategy([disabled])
    assert results[0].state == DISABLED
    assert not any(r.state == SELECTED for r in results)


def test_ties_break_by_input_order():
    a = _candidate(strategy_id="A", backtest_result=_good_backtest_result(strategy_id="A", r=1.0))
    b = _candidate(strategy_id="B", backtest_result=_good_backtest_result(strategy_id="B", r=1.0))
    results = select_strategy([a, b])
    assert results[0].strategy_id == "A"
    assert results[0].state == SELECTED
    assert results[1].state == NOT_SELECTED


def test_no_eligible_candidates_selects_nothing():
    results = select_strategy([_candidate(status=REJECTED), _candidate(strategy_id="X", status=COLLECTED, backtest_result=None)])
    assert not any(r.state == SELECTED for r in results)


# ---------------------------------------------------------------------------
# Scoring functions -- pure, explainable
# ---------------------------------------------------------------------------

def test_score_trade_metrics_none_when_no_trades():
    assert scoring.score_trade_metrics({"num_trades": 0}) is None
    assert scoring.score_trade_metrics(None) is None


def test_score_trade_metrics_perfect_inputs_near_one():
    metrics = {"num_trades": 50, "win_rate": 1.0, "avg_r": 2.0, "profit_factor": 3.0}
    assert scoring.score_trade_metrics(metrics) == pytest.approx(1.0)


def test_score_sample_size_scales_linearly_and_caps_at_one():
    assert scoring.score_sample_size({"num_trades": 15}) == pytest.approx(0.5)
    assert scoring.score_sample_size({"num_trades": 1000}) == pytest.approx(1.0)
    assert scoring.score_sample_size(None) is None


def test_score_mdd_zero_drawdown_is_perfect():
    assert scoring.score_mdd({"max_drawdown": 0.0}) == pytest.approx(1.0)


def test_score_mdd_at_reference_is_zero():
    assert scoring.score_mdd({"max_drawdown": -scoring.MDD_REFERENCE_DOLLARS}) == pytest.approx(0.0)


def test_score_mdd_beyond_reference_clips_to_zero():
    assert scoring.score_mdd({"max_drawdown": -1000.0}) == 0.0


def test_score_slippage_sensitivity_none_with_insufficient_variants():
    assert scoring.score_slippage_sensitivity({0: {"status": "OK", "expectancy": 5.0}}) is None
    assert scoring.score_slippage_sensitivity(None) is None


def test_score_slippage_sensitivity_full_retention_scores_one():
    result = {0: {"status": "OK", "expectancy": 5.0}, 50: {"status": "OK", "expectancy": 5.0}}
    assert scoring.score_slippage_sensitivity(result) == pytest.approx(1.0)


def test_score_slippage_sensitivity_degraded_expectancy_scores_below_one():
    result = {0: {"status": "OK", "expectancy": 10.0}, 50: {"status": "OK", "expectancy": 5.0}}
    assert scoring.score_slippage_sensitivity(result) == pytest.approx(0.5)


def test_score_symbol_condition_fit_exact_match():
    bt = _good_backtest_result(symbol="AAPL")
    assert scoring.score_symbol_condition_fit("AAPL", bt) == 1.0
    assert scoring.score_symbol_condition_fit("MSFT", bt) == 0.5
    assert scoring.score_symbol_condition_fit("AAPL", None) is None


def test_compute_composite_score_excludes_none_factors_not_zero():
    factors_full = SelectionFactors(market_state_fit=1.0, symbol_condition_fit=1.0, backtest_performance=1.0,
                                     paper_performance=1.0, sample_size=1.0, mdd=1.0, slippage_sensitivity=1.0)
    factors_partial = SelectionFactors(backtest_performance=1.0)  # everything else None
    assert scoring.compute_composite_score(factors_full) == pytest.approx(1.0)
    assert scoring.compute_composite_score(factors_partial) == pytest.approx(1.0)  # renormalized, not dragged to 0


def test_compute_composite_score_none_when_no_factors():
    assert scoring.compute_composite_score(SelectionFactors()) is None


def test_composite_weights_sum_to_one():
    assert abs(sum(scoring.COMPOSITE_WEIGHTS.values()) - 1.0) < 1e-9
