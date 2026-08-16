"""Stage 7: backtest/replay engine tests.

Every test builds synthetic OHLCV bars with real Eastern timestamps (no
network, no real market data) so session separation (premarket vs
regular) is evaluated against the project's actual market_hours.py
calendar logic, not a mock.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from backtest import compare, engine, metrics
from backtest.config import BacktestConfig
from backtest.models import STATUS_INSUFFICIENT_DATA, STATUS_OK, CostBreakdown, ExitEvent, Trade
from strategy.interface import STATE_ENTRY_SIGNAL, STATE_NO_SETUP, EvaluationResult, TradingStrategy
from strategy.status import STRUCTURED

EASTERN = ZoneInfo("America/New_York")
TRADING_DAY = datetime(2026, 7, 21, tzinfo=EASTERN)  # verified trading day, not a holiday


def _bars(start_hour, start_minute, n, *, open_=100.0, high=100.5, low=99.5, close=100.0, volume=100_000, freq_minutes=1):
    start = TRADING_DAY.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    index = pd.date_range(start, periods=n, freq=f"{freq_minutes}min")
    return pd.DataFrame({
        "Open": [open_] * n, "High": [high] * n, "Low": [low] * n,
        "Close": [close] * n, "Volume": [volume] * n,
    }, index=index)


class FakeStrategy(TradingStrategy):
    """Signals exactly once, when the visible-bars window first reaches
    `signal_index` (0-based, i.e. bars.iloc[:signal_index+1]) -- lets
    tests place a signal at a precise bar without real indicator math."""

    def __init__(self, signal_index=None, stop_price=95.0, target_1=105.0, target_2=110.0,
                 invalidate_at_index=None, status=STRUCTURED):
        super().__init__(strategy_id="FAKE_BACKTEST_STRATEGY", version="1.0.0", status=status)
        self.signal_index = signal_index
        self.stop_price = stop_price
        self.target_1 = target_1
        self.target_2 = target_2
        self.invalidate_at_index = invalidate_at_index
        self._fired = False

    def evaluate_setup(self, bars, *, symbol, as_of=None):
        return self.generate_entry(bars, symbol=symbol, as_of=as_of)

    def generate_entry(self, bars, *, symbol, as_of=None):
        current_index = len(bars) - 1
        signal = (not self._fired) and self.signal_index is not None and current_index == self.signal_index
        if signal:
            self._fired = True
        return EvaluationResult(
            strategy_id=self.strategy_id, symbol=symbol, evaluated_at="2026-07-21T00:00:00+00:00",
            state=STATE_ENTRY_SIGNAL if signal else STATE_NO_SETUP, signal=signal,
            stop_price=self.stop_price, target_1=self.target_1, target_2=self.target_2,
        )

    def calculate_stop(self, bars, *, entry_price):
        return self.stop_price

    def calculate_targets(self, *, entry_price, stop_price):
        return {"target_1": self.target_1, "target_2": self.target_2}

    def invalidate(self, bars, *, symbol):
        return self.invalidate_at_index is not None and (len(bars) - 1) >= self.invalidate_at_index


def _no_cost_config(**overrides):
    defaults = dict(spread_bps=0.0, slippage_bps=0.0, fee_per_share=0.0, entry_delay_bars=0,
                     max_fill_fraction_of_bar_volume=1.0, min_bars_required=5)
    defaults.update(overrides)
    return BacktestConfig(**defaults)


# ---------------------------------------------------------------------------
# INSUFFICIENT_DATA
# ---------------------------------------------------------------------------

def test_insufficient_bars_reports_insufficient_data_not_a_score():
    bars = _bars(9, 30, 10)
    strategy = FakeStrategy(signal_index=2)
    result = engine.run_backtest(strategy, bars, symbol="AAPL", config=_no_cost_config(min_bars_required=500))
    assert result.status == STATUS_INSUFFICIENT_DATA
    assert result.trades == []
    assert "500" in result.reason


# ---------------------------------------------------------------------------
# Look-ahead / basic replay correctness
# ---------------------------------------------------------------------------

def test_signal_fills_at_next_bar_open_not_signal_bar_close():
    bars = _bars(9, 30, 20, open_=100.0, high=100.5, low=99.5, close=100.2, volume=1_000_000)
    bars.iloc[15, bars.columns.get_loc("Low")] = 80.0  # forces a later stop-loss so the trade actually closes
    strategy = FakeStrategy(signal_index=5, stop_price=90.0, target_1=200.0, target_2=210.0)
    config = _no_cost_config(entry_delay_bars=0)
    result = engine.run_backtest(strategy, bars, symbol="AAPL", config=config)
    assert result.status == STATUS_OK
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_price == pytest.approx(100.0)  # bar 6's Open, not bar 5's Close
    assert trade.entry_time != trade.signal_time


def test_no_signal_produces_no_trades():
    bars = _bars(9, 30, 20)
    strategy = FakeStrategy(signal_index=None)
    result = engine.run_backtest(strategy, bars, symbol="AAPL", config=_no_cost_config())
    assert result.trades == []


# ---------------------------------------------------------------------------
# Session separation
# ---------------------------------------------------------------------------

def test_premarket_signal_is_never_entered():
    bars = _bars(6, 0, 20)  # 06:00 ET -- premarket
    strategy = FakeStrategy(signal_index=3, stop_price=90.0, target_1=200.0, target_2=210.0)
    result = engine.run_backtest(strategy, bars, symbol="AAPL", config=_no_cost_config())
    assert result.trades == []  # signal fired in premarket, never allowed to fill


def test_regular_session_signal_is_entered():
    bars = _bars(9, 30, 20, high=100.5, low=99.5)  # 09:30 ET -- regular session start
    bars.iloc[15, bars.columns.get_loc("Low")] = 80.0  # forces a later stop-loss so the trade actually closes
    strategy = FakeStrategy(signal_index=3, stop_price=90.0, target_1=200.0, target_2=210.0)
    result = engine.run_backtest(strategy, bars, symbol="AAPL", config=_no_cost_config())
    assert len(result.trades) == 1


# ---------------------------------------------------------------------------
# Same-bar stop/target collision -> conservative STOP_FIRST
# ---------------------------------------------------------------------------

def test_same_bar_stop_and_target_collision_resolves_as_stop_loss():
    bars = _bars(9, 30, 10, open_=100.0, high=100.1, low=99.9, close=100.0, volume=1_000_000)
    bars.iloc[6, bars.columns.get_loc("Low")] = 89.0    # touches stop
    bars.iloc[6, bars.columns.get_loc("High")] = 111.0  # ALSO touches target_2 same bar
    strategy = FakeStrategy(signal_index=3, stop_price=90.0, target_1=105.0, target_2=110.0)
    result = engine.run_backtest(strategy, bars, symbol="AAPL", config=_no_cost_config())
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "STOP_LOSS"


# ---------------------------------------------------------------------------
# Cost breakdown: spread / slippage / fee / entry-delay reported separately
# ---------------------------------------------------------------------------

def test_costs_are_broken_out_separately_and_all_nonzero_when_configured():
    bars = _bars(9, 30, 15, open_=100.0, close=99.0, high=100.5, low=99.5, volume=1_000_000)
    bars.iloc[8, bars.columns.get_loc("Low")] = 80.0  # forces a stop-loss so the trade actually closes
    strategy = FakeStrategy(signal_index=2, stop_price=90.0, target_1=200.0, target_2=210.0)
    config = BacktestConfig(spread_bps=10.0, slippage_bps=10.0, fee_per_share=0.01,
                             entry_delay_bars=1, min_bars_required=5, max_fill_fraction_of_bar_volume=1.0)
    result = engine.run_backtest(strategy, bars, symbol="AAPL", config=config)
    assert len(result.trades) == 1
    costs = result.trades[0].costs
    assert costs.spread_cost > 0
    assert costs.slippage_cost > 0
    assert costs.fee_cost > 0
    # entry_delay_bars=1 with flat prices -> delay cost is 0 here (no drift to measure);
    # verify it's tracked as a distinct field regardless (not folded into spread/slippage).
    assert isinstance(costs.entry_delay_cost, float)


def test_zero_cost_config_yields_zero_costs():
    bars = _bars(9, 30, 10, high=100.5, low=99.5)
    bars.iloc[8, bars.columns.get_loc("Low")] = 80.0  # forces a stop-loss so the trade actually closes
    strategy = FakeStrategy(signal_index=2, stop_price=90.0, target_1=200.0, target_2=210.0)
    result = engine.run_backtest(strategy, bars, symbol="AAPL", config=_no_cost_config())
    trade = result.trades[0]
    assert trade.costs.spread_cost == 0
    assert trade.costs.slippage_cost == 0
    assert trade.costs.fee_cost == 0


# ---------------------------------------------------------------------------
# Volume constraint / partial fills
# ---------------------------------------------------------------------------

def test_zero_volume_fill_bar_prevents_entry():
    bars = _bars(9, 30, 10, high=100.5, low=99.5, volume=0)
    strategy = FakeStrategy(signal_index=2, stop_price=90.0, target_1=200.0, target_2=210.0)
    result = engine.run_backtest(strategy, bars, symbol="AAPL", config=_no_cost_config())
    assert result.trades == []


def test_illiquid_exit_bar_carries_remainder_to_next_bar():
    bars = _bars(9, 30, 15, open_=100.0, high=100.5, low=99.5, close=100.0, volume=1_000_000)
    bars.iloc[6, bars.columns.get_loc("Low")] = 80.0   # stop touched but...
    bars.iloc[6, bars.columns.get_loc("Volume")] = 0   # ...bar is too illiquid to exit anything
    bars.iloc[7, bars.columns.get_loc("Low")] = 80.0   # stop still breached next bar, liquid
    strategy = FakeStrategy(signal_index=3, stop_price=90.0, target_1=200.0, target_2=210.0)
    result = engine.run_backtest(strategy, bars, symbol="AAPL", config=_no_cost_config())
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "STOP_LOSS"
    assert result.trades[0].exit_events[-1].time != result.trades[0].entry_time  # exited on a later bar


# ---------------------------------------------------------------------------
# Partial exit at target_1, then target_2 closes remainder
# ---------------------------------------------------------------------------

def test_partial_target_1_then_target_2_produces_two_exit_events():
    bars = _bars(9, 30, 15, open_=100.0, high=100.5, low=99.5, close=100.0, volume=1_000_000)
    bars.iloc[6, bars.columns.get_loc("High")] = 106.0  # target_1 touched
    bars.iloc[8, bars.columns.get_loc("High")] = 111.0  # target_2 touched later
    strategy = FakeStrategy(signal_index=3, stop_price=50.0, target_1=105.0, target_2=110.0)
    result = engine.run_backtest(strategy, bars, symbol="AAPL", config=_no_cost_config())
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "TARGET_2"
    assert len(trade.exit_events) == 2
    assert trade.exit_events[0].reason == "PARTIAL_TARGET_1"
    assert trade.exit_events[0].qty == 50  # 50% of nominal_qty=100
    assert trade.exit_events[1].reason == "TARGET_2"
    assert trade.exit_events[1].qty == 50


# ---------------------------------------------------------------------------
# Time-stop / EOD forced close
# ---------------------------------------------------------------------------

def test_time_stop_forces_exit_after_max_hold_minutes():
    bars = _bars(9, 30, 90, open_=100.0, high=100.2, low=99.8, close=100.0, volume=1_000_000)
    strategy = FakeStrategy(signal_index=3, stop_price=1.0, target_1=200.0, target_2=210.0)
    result = engine.run_backtest(strategy, bars, symbol="AAPL", config=_no_cost_config())
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "TIME_STOP"


def test_eod_forces_exit_near_market_close():
    bars = _bars(15, 50, 15, open_=100.0, high=100.2, low=99.8, close=100.0, volume=1_000_000)
    strategy = FakeStrategy(signal_index=1, stop_price=1.0, target_1=200.0, target_2=210.0)
    result = engine.run_backtest(strategy, bars, symbol="AAPL", config=_no_cost_config())
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "EOD_FORCED_CLOSE"


def test_strategy_invalidation_forces_exit():
    bars = _bars(9, 30, 20, open_=100.0, high=100.2, low=99.8, close=100.0, volume=1_000_000)
    strategy = FakeStrategy(signal_index=2, stop_price=1.0, target_1=200.0, target_2=210.0, invalidate_at_index=6)
    result = engine.run_backtest(strategy, bars, symbol="AAPL", config=_no_cost_config())
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "STRATEGY_INVALIDATION"


# ---------------------------------------------------------------------------
# Bar validation
# ---------------------------------------------------------------------------

def test_non_datetime_index_rejected():
    bars = pd.DataFrame({"Open": [1], "High": [1], "Low": [1], "Close": [1], "Volume": [1]})
    with pytest.raises(engine.BacktestError):
        engine.run_backtest(FakeStrategy(), bars, symbol="AAPL", config=_no_cost_config())


def test_out_of_order_bars_rejected():
    bars = _bars(9, 30, 10)
    shuffled = bars.iloc[::-1]
    with pytest.raises(engine.BacktestError):
        engine.run_backtest(FakeStrategy(), shuffled, symbol="AAPL", config=_no_cost_config())


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _trade(pnl, r=None, entry_time="2026-07-21T09:30:00-04:00", entry_price=100.0, entry_bar_volume=100_000):
    return Trade(
        symbol="AAPL", strategy_id="S", strategy_version="1.0", signal_time=entry_time,
        signal_price=entry_price, entry_time=entry_time, entry_price=entry_price,
        entry_session="regular", entry_bar_volume=entry_bar_volume,
        stop_price=entry_price - 1, target_1_price=entry_price + 1, target_2_price=entry_price + 2,
        requested_qty=1, filled_qty=1, exit_events=[], exit_reason="TEST",
        realized_pnl=pnl, r_multiple=r, costs=CostBreakdown(),
    )


def test_compute_metrics_basic_stats():
    trades = [_trade(10, r=1.0), _trade(-5, r=-0.5), _trade(20, r=2.0)]
    m = metrics.compute_metrics(trades)
    assert m["num_trades"] == 3
    assert m["win_rate"] == pytest.approx(2 / 3)
    assert m["avg_r"] == pytest.approx((1.0 - 0.5 + 2.0) / 3)
    assert m["profit_factor"] == pytest.approx(30 / 5)
    assert m["expectancy"] == pytest.approx((10 - 5 + 20) / 3)


def test_compute_metrics_no_losers_profit_factor_is_none_not_infinity():
    trades = [_trade(10, r=1.0), _trade(5, r=0.5)]
    m = metrics.compute_metrics(trades)
    assert m["profit_factor"] is None
    assert m["profit_factor_reason"] == metrics.NO_LOSING_TRADES_REASON


def test_compute_metrics_empty_trades():
    m = metrics.compute_metrics([])
    assert m["num_trades"] == 0
    assert m["reason"] == metrics.NO_TRADES_REASON


def test_max_consecutive_losses_counts_correctly():
    trades = [
        _trade(10, entry_time="2026-07-21T09:30:00-04:00"),
        _trade(-1, entry_time="2026-07-21T09:31:00-04:00"),
        _trade(-1, entry_time="2026-07-21T09:32:00-04:00"),
        _trade(-1, entry_time="2026-07-21T09:33:00-04:00"),
        _trade(10, entry_time="2026-07-21T09:34:00-04:00"),
        _trade(-1, entry_time="2026-07-21T09:35:00-04:00"),
    ]
    m = metrics.compute_metrics(trades)
    assert m["max_consecutive_losses"] == 3


def test_max_drawdown_is_negative_when_equity_dips_below_peak():
    trades = [
        _trade(10, entry_time="2026-07-21T09:30:00-04:00"),
        _trade(-8, entry_time="2026-07-21T09:31:00-04:00"),
        _trade(-8, entry_time="2026-07-21T09:32:00-04:00"),
    ]
    m = metrics.compute_metrics(trades)
    assert m["max_drawdown"] == pytest.approx(-16)


def test_best_trade_removed_reported_alongside_not_instead_of():
    trades = [_trade(100, r=10.0), _trade(-5, r=-0.5), _trade(-5, r=-0.5)]
    result = metrics.compute_metrics_with_best_trade_removed(trades)
    assert result["all_trades"]["num_trades"] == 3
    assert result["best_trade_removed"]["num_trades"] == 2
    assert result["all_trades"]["expectancy"] > result["best_trade_removed"]["expectancy"]


def test_best_trade_removed_none_when_fewer_than_two_trades():
    result = metrics.compute_metrics_with_best_trade_removed([_trade(10)])
    assert result["best_trade_removed"] is None
    assert result["best_trade_removed_reason"] == "FEWER_THAN_2_TRADES"


def test_costs_summary_aggregates_all_four_components():
    t1 = _trade(10)
    t1.costs = CostBreakdown(spread_cost=1, slippage_cost=2, fee_cost=3, entry_delay_cost=4)
    t2 = _trade(5)
    t2.costs = CostBreakdown(spread_cost=1, slippage_cost=2, fee_cost=3, entry_delay_cost=4)
    summary = metrics.costs_summary([t1, t2])
    assert summary == {"spread_cost": 2, "slippage_cost": 4, "fee_cost": 6, "entry_delay_cost": 8}


def test_breakdown_by_time_of_day_groups_by_entry_hour():
    trades = [
        _trade(10, entry_time="2026-07-21T09:30:00-04:00"),
        _trade(-5, entry_time="2026-07-21T09:45:00-04:00"),
        _trade(20, entry_time="2026-07-21T14:00:00-04:00"),
    ]
    breakdown = metrics.breakdown_by_time_of_day(trades)
    assert set(breakdown) == {9, 14}
    assert breakdown[9]["num_trades"] == 2
    assert breakdown[14]["num_trades"] == 1


def test_breakdown_by_price_range_and_liquidity():
    trades = [_trade(10, entry_price=5.0, entry_bar_volume=5_000), _trade(10, entry_price=150.0, entry_bar_volume=500_000)]
    by_price = metrics.breakdown_by_price_range(trades)
    by_liquidity = metrics.breakdown_by_liquidity(trades)
    assert "$0-10" in by_price
    assert "$50-200" in by_price
    assert len(by_liquidity) == 2


def test_slippage_sensitivity_reruns_backtest_per_variant():
    bars = _bars(9, 30, 20, open_=100.0, high=100.5, low=99.5, close=100.0, volume=1_000_000)
    strategy = FakeStrategy(signal_index=3, stop_price=1.0, target_1=200.0, target_2=210.0)
    base_config = _no_cost_config()
    result = metrics.slippage_sensitivity(
        strategy, bars, symbol="AAPL", base_config=base_config, slippage_bps_variants=[0, 20, 50]
    )
    assert set(result) == {0, 20, 50}
    assert all(v["status"] == STATUS_OK for v in result.values())


# ---------------------------------------------------------------------------
# Strategy comparison -- never activates anything
# ---------------------------------------------------------------------------

def test_compare_strategies_insufficient_data_gets_no_metrics_fields():
    from backtest.models import BacktestResult
    ok_result = BacktestResult(strategy_id="A", strategy_version="1.0", symbol="AAPL",
                                status=STATUS_OK, trades=[_trade(10), _trade(-5)], bars_evaluated=600)
    insufficient_result = BacktestResult(strategy_id="B", strategy_version="1.0", symbol="AAPL",
                                          status=STATUS_INSUFFICIENT_DATA, trades=[], bars_evaluated=10,
                                          reason="10 bars supplied, 500 required")
    rows = compare.compare_strategies({"A": ok_result, "B": insufficient_result})
    row_a, row_b = rows
    assert row_a["status"] == STATUS_OK
    assert "metrics" in row_a
    assert row_b["status"] == STATUS_INSUFFICIENT_DATA
    assert "metrics" not in row_b


def test_compare_module_never_imports_strategy_registry():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(compare))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_names.add(node.module)
            imported_names.update(alias.name for alias in node.names)
    assert not any("registry" in (name or "") for name in imported_names)
    assert "registry" not in dir(compare)
    assert not hasattr(compare, "StrategyRegistry")
