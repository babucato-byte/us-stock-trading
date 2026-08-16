"""Metrics computed from a BacktestResult's trade list.

Every function here is a plain, auditable calculation -- no smoothing,
no outlier rejection beyond the one explicitly requested
(best_trade_removed, always reported *alongside* the unfiltered numbers,
never in place of them), no threshold chosen to make a particular
strategy look better. If a metric is undefined for the given trades
(e.g. Profit Factor with zero losing trades), it is reported as None
with an explicit reason rather than substituted with an inflated
placeholder like infinity.
"""

from datetime import datetime

from backtest.models import STATUS_OK

NO_TRADES_REASON = "NO_TRADES"
NO_LOSING_TRADES_REASON = "NO_LOSING_TRADES"


def _entry_dt(trade):
    return datetime.fromisoformat(trade.entry_time)


def _win(trade):
    return trade.realized_pnl > 0


def compute_metrics(trades):
    """Core metrics for a single trade list. Returns a dict; every trade
    must belong to the same (strategy_id, symbol) backtest run."""
    num_trades = len(trades)
    if num_trades == 0:
        return {
            "num_trades": 0, "win_rate": None, "avg_r": None, "profit_factor": None,
            "profit_factor_reason": NO_TRADES_REASON, "expectancy": None,
            "max_drawdown": None, "max_consecutive_losses": None, "reason": NO_TRADES_REASON,
        }

    wins = [t for t in trades if _win(t)]
    losses = [t for t in trades if not _win(t)]
    r_values = [t.r_multiple for t in trades if t.r_multiple is not None]

    win_rate = len(wins) / num_trades
    avg_r = (sum(r_values) / len(r_values)) if r_values else None
    expectancy = sum(t.realized_pnl for t in trades) / num_trades

    gross_profit = sum(t.realized_pnl for t in wins)
    gross_loss = abs(sum(t.realized_pnl for t in losses))
    if gross_loss == 0:
        profit_factor, pf_reason = None, NO_LOSING_TRADES_REASON
    else:
        profit_factor, pf_reason = gross_profit / gross_loss, None

    ordered = sorted(trades, key=_entry_dt)
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    consecutive_losses = 0
    max_consecutive_losses = 0
    for t in ordered:
        equity += t.realized_pnl
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)
        if _win(t):
            consecutive_losses = 0
        else:
            consecutive_losses += 1
            max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)

    return {
        "num_trades": num_trades,
        "win_rate": win_rate,
        "avg_r": avg_r,
        "profit_factor": profit_factor,
        "profit_factor_reason": pf_reason,
        "expectancy": expectancy,
        "max_drawdown": max_drawdown,  # cumulative-PnL drawdown, not portfolio-equity % (no starting balance modeled)
        "max_consecutive_losses": max_consecutive_losses,
    }


def compute_metrics_with_best_trade_removed(trades):
    """Returns {"all_trades": {...}, "best_trade_removed": {...} or None}.
    best_trade_removed excludes the single trade with the highest
    realized_pnl, reported ALONGSIDE all_trades (never replacing it) so a
    reviewer can see how much of the edge depends on one outlier win."""
    all_metrics = compute_metrics(trades)
    if len(trades) < 2:
        return {"all_trades": all_metrics, "best_trade_removed": None,
                "best_trade_removed_reason": "FEWER_THAN_2_TRADES"}
    best_trade = max(trades, key=lambda t: t.realized_pnl)
    remaining = [t for t in trades if t is not best_trade]
    return {
        "all_trades": all_metrics,
        "best_trade_removed": compute_metrics(remaining),
        "best_trade_removed_reason": None,
    }


def costs_summary(trades):
    """Aggregate the four separately-tracked cost components across every
    trade -- spread/slippage/entry-delay are informational (already
    reflected in realized_pnl via fill price), fee_cost is additionally
    subtracted from realized_pnl. Reported separately per the requirement
    that these never be folded into one opaque number."""
    return {
        "spread_cost": sum(t.costs.spread_cost for t in trades),
        "slippage_cost": sum(t.costs.slippage_cost for t in trades),
        "fee_cost": sum(t.costs.fee_cost for t in trades),
        "entry_delay_cost": sum(t.costs.entry_delay_cost for t in trades),
    }


def breakdown_by_time_of_day(trades):
    """Group by the entry fill's Eastern hour."""
    buckets = {}
    for t in trades:
        hour = _entry_dt(t).hour
        buckets.setdefault(hour, []).append(t)
    return {hour: compute_metrics(group) for hour, group in sorted(buckets.items())}


PRICE_RANGE_BINS = [(0, 10), (10, 50), (50, 200), (200, float("inf"))]


def _price_range_label(price):
    for low, high in PRICE_RANGE_BINS:
        if low <= price < high:
            return f"${low}-{'inf' if high == float('inf') else high}"
    return "unknown"


def breakdown_by_price_range(trades):
    buckets = {}
    for t in trades:
        label = _price_range_label(t.entry_price)
        buckets.setdefault(label, []).append(t)
    return {label: compute_metrics(group) for label, group in buckets.items()}


LIQUIDITY_BINS = [(0, 10_000), (10_000, 100_000), (100_000, 1_000_000), (1_000_000, float("inf"))]


def _liquidity_label(volume):
    for low, high in LIQUIDITY_BINS:
        if low <= volume < high:
            return f"{low}-{'inf' if high == float('inf') else high}"
    return "unknown"


def breakdown_by_liquidity(trades):
    """Bucketed by the entry fill bar's own volume -- a proxy for how
    liquid conditions were at the moment of entry."""
    buckets = {}
    for t in trades:
        label = _liquidity_label(t.entry_bar_volume)
        buckets.setdefault(label, []).append(t)
    return {label: compute_metrics(group) for label, group in buckets.items()}


def slippage_sensitivity(strategy, bars, *, symbol, base_config, slippage_bps_variants):
    """Re-run the full backtest under each slippage_bps in
    `slippage_bps_variants`, holding every other config field fixed, and
    report each variant's metrics. A full re-run (not a post-hoc PnL
    adjustment) is used deliberately: slippage changes which bars can
    fill at all under the volume constraint, so it can change *which*
    trades occur, not just their price -- a cheaper approximation would
    silently misrepresent that."""
    from backtest.engine import run_backtest
    from dataclasses import replace

    results = {}
    for bps in slippage_bps_variants:
        variant_config = replace(base_config, slippage_bps=bps)
        result = run_backtest(strategy, bars, symbol=symbol, config=variant_config)
        if result.status != STATUS_OK:
            results[bps] = {"status": result.status, "reason": result.reason}
        else:
            results[bps] = {"status": STATUS_OK, **compute_metrics(result.trades)}
    return results
