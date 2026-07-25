"""Per-factor scoring functions and the composite weighting formula.

Every threshold/reference value here is an explicit, documented
ASSUMPTION -- none derived by fitting to any particular strategy's
numbers (mirrors Stage 7's backtest/config.py discipline: fixed before
use, recorded in DECISION_LOG.md, never tuned after seeing a result).
This is a plain weighted-average formula, not a model -- every score is
independently reproducible by re-running the same function on the same
inputs, which is the whole point of "explainable, not LLM judgment."
"""

MIN_TRADES_FOR_SAMPLE_SIZE_SCORE = 30  # ASSUMPTION: 30 completed trades treated as "fully sufficient" sample
MDD_REFERENCE_DOLLARS = 100.0  # ASSUMPTION: same nominal_qty=100 basis as backtest/config.py; -100 or worse -> score 0
AVG_R_REFERENCE = 2.0  # ASSUMPTION: average R of 2.0 or better maps to a perfect backtest/paper performance sub-score
PROFIT_FACTOR_REFERENCE = 3.0  # ASSUMPTION: Profit Factor of 3.0 or better maps to a perfect sub-score

# Composite weights -- must sum to 1.0. Equal-ish weighting across the
# seven named factors from the user's Stage 8 instruction, with market
# fit and data-quality-adjacent factors (sample size, MDD, slippage
# sensitivity) weighted slightly lower than the two direct performance
# factors, on the reasoning that performance is the primary signal and
# the others are risk/applicability adjustments to it. This weighting is
# itself an ASSUMPTION -- documented in DECISION_LOG.md's Stage 8 section,
# not derived from any strategy's actual results.
COMPOSITE_WEIGHTS = {
    "market_state_fit": 0.10,
    "symbol_condition_fit": 0.10,
    "backtest_performance": 0.20,
    "paper_performance": 0.20,
    "sample_size": 0.15,
    "mdd": 0.15,
    "slippage_sensitivity": 0.10,
}
assert abs(sum(COMPOSITE_WEIGHTS.values()) - 1.0) < 1e-9


def _clip(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def score_trade_metrics(metrics):
    """Shared scorer for backtest_performance and paper_performance --
    both are "how good were this strategy's completed trades," just from
    different data sources. Returns None if metrics is missing or has
    zero trades (nothing to score, not a bad score)."""
    if not metrics or not metrics.get("num_trades"):
        return None
    win_rate = metrics.get("win_rate") or 0.0
    avg_r = metrics.get("avg_r")
    avg_r_component = _clip((avg_r or 0.0) / AVG_R_REFERENCE) if avg_r is not None else 0.0
    profit_factor = metrics.get("profit_factor")
    pf_component = _clip((profit_factor or 0.0) / PROFIT_FACTOR_REFERENCE) if profit_factor is not None else 0.0
    return (win_rate + avg_r_component + pf_component) / 3.0


def score_sample_size(metrics):
    if not metrics:
        return None
    num_trades = metrics.get("num_trades") or 0
    return _clip(num_trades / MIN_TRADES_FOR_SAMPLE_SIZE_SCORE)


def score_mdd(metrics):
    if not metrics or metrics.get("max_drawdown") is None:
        return None
    return _clip(1.0 - abs(metrics["max_drawdown"]) / MDD_REFERENCE_DOLLARS)


def score_slippage_sensitivity(sensitivity_result):
    """Higher score = expectancy degrades less as slippage increases
    (a more robust edge). Compares the lowest-slippage variant's
    expectancy to the highest-slippage variant's; if the strategy is
    already unprofitable at low slippage, or the data isn't present,
    returns None (not evaluated) rather than a misleading 0."""
    if not sensitivity_result:
        return None
    ok_variants = {bps: v for bps, v in sensitivity_result.items() if v.get("status") == "OK"}
    if len(ok_variants) < 2:
        return None
    lowest_bps = min(ok_variants)
    highest_bps = max(ok_variants)
    base_expectancy = ok_variants[lowest_bps].get("expectancy")
    stressed_expectancy = ok_variants[highest_bps].get("expectancy")
    if base_expectancy is None or stressed_expectancy is None or base_expectancy <= 0:
        return None
    retained_fraction = stressed_expectancy / base_expectancy
    return _clip(retained_fraction)


def score_symbol_condition_fit(symbol, backtest_result):
    """1.0 if the backtest was actually run on this exact symbol, 0.5 if
    on a different symbol (ASSUMPTION: a strategy's edge partially
    transfers across symbols but is not fully proven for an untested
    one), None if there's no backtest to compare against at all."""
    if backtest_result is None:
        return None
    return 1.0 if backtest_result.symbol == symbol else 0.5


def compute_composite_score(factors: "SelectionFactors"):
    """Average of every non-None factor, weighted by COMPOSITE_WEIGHTS
    and renormalized over only the factors actually present -- a missing
    factor is excluded, never treated as 0 or averaged in as neutral."""
    present = {name: value for name, value in factors.as_dict().items() if value is not None}
    if not present:
        return None
    weight_sum = sum(COMPOSITE_WEIGHTS[name] for name in present)
    return sum(COMPOSITE_WEIGHTS[name] * value for name, value in present.items()) / weight_sum
