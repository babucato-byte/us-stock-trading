"""Side-by-side comparison of multiple strategies' backtest results.

This module produces a comparison table ONLY -- it never selects,
ranks-and-picks, or activates a strategy. It does not import
strategy.registry and must never gain a call to
StrategyRegistry.activate()/register() with status=ACTIVE: candidate
strategies (including anything sourced from strategy_sources/'s
YouTube/user-chart catalog) are comparison subjects here, nothing more.
Turning a comparison winner into the live ACTIVE strategy is Stage 8's
exclusive, explainable-rules responsibility -- this module intentionally
has no opinion about which row is "best."
"""

from backtest.metrics import compute_metrics_with_best_trade_removed, costs_summary
from backtest.models import STATUS_INSUFFICIENT_DATA, STATUS_OK


def compare_strategies(results):
    """`results`: dict of strategy_id -> BacktestResult (one per strategy,
    same symbol/period ideally, but not enforced here). Returns a list of
    row dicts, one per strategy, in the input's iteration order.

    A strategy whose BacktestResult.status is INSUFFICIENT_DATA gets a row
    with status=INSUFFICIENT_DATA and no metrics fields at all -- it is
    never assigned a placeholder score, silently skipped, or averaged in
    with strategies that do have enough data.
    """
    rows = []
    for strategy_id, result in results.items():
        if result.status == STATUS_INSUFFICIENT_DATA:
            rows.append({
                "strategy_id": strategy_id,
                "strategy_version": result.strategy_version,
                "symbol": result.symbol,
                "status": STATUS_INSUFFICIENT_DATA,
                "reason": result.reason,
                "bars_evaluated": result.bars_evaluated,
            })
            continue

        metrics = compute_metrics_with_best_trade_removed(result.trades)
        rows.append({
            "strategy_id": strategy_id,
            "strategy_version": result.strategy_version,
            "symbol": result.symbol,
            "status": STATUS_OK,
            "bars_evaluated": result.bars_evaluated,
            "metrics": metrics["all_trades"],
            "best_trade_removed": metrics["best_trade_removed"],
            "costs": costs_summary(result.trades),
        })
    return rows
