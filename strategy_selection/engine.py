"""Eligibility gating + scoring + top-1 selection across a candidate pool."""

from strategy.status import ACTIVE, COLLECTED, PAUSED, REJECTED, STRUCTURED
from strategy_selection import scoring
from strategy_selection.models import (
    DISABLED,
    INSUFFICIENT_DATA,
    MARKET_MISMATCH,
    NOT_SELECTED,
    SELECTED,
    SelectionFactors,
    SelectionResult,
)

MIN_TRADES_FOR_SCORING = 10
# ASSUMPTION: fewer than 10 completed backtest trades is treated as too
# small a sample to responsibly score at all (distinct from, and a lower
# bar than, the eventual live Paper Trading approval gate's much stricter
# minimum -- see PROJECT_CONSTITUTION.md's Paper approval criteria, which
# this selection engine does not replace).

DISABLED_STATUSES = {REJECTED, PAUSED}
NOT_YET_REVIEWED_STATUSES = {COLLECTED, STRUCTURED}

# ASSUMPTION: per-strategy preferred market session(s). No metadata for
# this exists on TradingStrategy itself, so it is recorded here as an
# explicit, editable table rather than guessed from the strategy's code.
# A strategy_id absent from this table has no market-state gate applied
# (never mismatched) -- silence here means "not yet documented," not
# "matches everything by design."
PREFERRED_MARKET_STATES = {
    "VWAP_MICRO_PULLBACK_MOMENTUM_V1": {"regular"},
}


def _eligibility_state(candidate, market_state):
    status = candidate.strategy.status
    if status in DISABLED_STATUSES:
        return DISABLED, f"strategy status is {status!r} (operator-disabled)"
    if status in NOT_YET_REVIEWED_STATUSES:
        return INSUFFICIENT_DATA, f"strategy status is {status!r} (not yet reviewed/backtested)"

    result = candidate.backtest_result
    if result is None or result.status != "OK":
        reason = "no backtest result" if result is None else f"backtest status={result.status}"
        return INSUFFICIENT_DATA, reason
    if len(result.trades) < MIN_TRADES_FOR_SCORING:
        return INSUFFICIENT_DATA, f"only {len(result.trades)} backtest trades, {MIN_TRADES_FOR_SCORING} required"

    preferred = PREFERRED_MARKET_STATES.get(candidate.strategy.strategy_id)
    if preferred is not None and market_state is not None and market_state not in preferred:
        return MARKET_MISMATCH, f"current market_state={market_state!r} not in preferred {sorted(preferred)}"

    return None, None  # eligible for scoring


def _score_candidate(candidate, market_state):
    from backtest.metrics import compute_metrics

    backtest_metrics = compute_metrics(candidate.backtest_result.trades)
    preferred = PREFERRED_MARKET_STATES.get(candidate.strategy.strategy_id)
    market_fit = None
    if preferred is not None and market_state is not None:
        market_fit = 1.0 if market_state in preferred else 0.0

    factors = SelectionFactors(
        market_state_fit=market_fit,
        symbol_condition_fit=scoring.score_symbol_condition_fit(candidate.symbol, candidate.backtest_result),
        backtest_performance=scoring.score_trade_metrics(backtest_metrics),
        paper_performance=scoring.score_trade_metrics(candidate.paper_metrics),
        sample_size=scoring.score_sample_size(backtest_metrics),
        mdd=scoring.score_mdd(backtest_metrics),
        slippage_sensitivity=scoring.score_slippage_sensitivity(candidate.slippage_sensitivity_result),
    )
    return factors, scoring.compute_composite_score(factors)


def select_strategy(candidates, *, market_state=None):
    """Evaluate every candidate, gate ineligible ones to their explicit
    state (DISABLED/INSUFFICIENT_DATA/MARKET_MISMATCH), score the rest,
    and mark the single highest-scoring one SELECTED (all other scored
    candidates become NOT_SELECTED). Ties break by input order
    (ASSUMPTION: deterministic, not random -- the first candidate in the
    input list wins a tie). Returns a list of SelectionResult, one per
    input candidate, in input order. If no candidate is eligible/scoreable,
    every result has a non-SELECTED state -- no SELECTED is ever fabricated.
    """
    results = []
    scored = []  # (index, candidate, factors, score)

    for i, candidate in enumerate(candidates):
        gate_state, gate_reason = _eligibility_state(candidate, market_state)
        if gate_state is not None:
            results.append(SelectionResult(
                strategy_id=candidate.strategy.strategy_id, strategy_version=candidate.strategy.version,
                symbol=candidate.symbol, state=gate_state, rationale=gate_reason,
            ))
            continue
        factors, score = _score_candidate(candidate, market_state)
        results.append(None)  # placeholder, filled in after ranking
        scored.append((i, candidate, factors, score))

    if scored:
        best_index, best_candidate, best_factors, best_score = max(
            scored, key=lambda entry: (entry[3] if entry[3] is not None else -1.0, -entry[0])
        )
        for i, candidate, factors, score in scored:
            is_best = i == best_index
            results[i] = SelectionResult(
                strategy_id=candidate.strategy.strategy_id, strategy_version=candidate.strategy.version,
                symbol=candidate.symbol, state=SELECTED if is_best else NOT_SELECTED,
                composite_score=score, factors=factors,
                rationale=("highest composite score among eligible candidates" if is_best
                           else f"composite score {score} below top candidate's {best_score}"),
            )

    return results
