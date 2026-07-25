"""Stage 8 (사용자 지시서): strategy selection engine.

Given a pool of candidate strategies (each already registered in
strategy/registry.py with a strategy/status.py lifecycle status, and
optionally backed by a Stage 7 backtest result and paper-trading
performance summary), decide which single strategy -- if any -- should
be selected. Explicitly rule-based/explainable, NOT an LLM free-judgment
call: every factor is a plain, documented, deterministic function of its
inputs, and every SelectionResult carries the per-factor breakdown that
produced its composite score, so "why was X selected over Y" is always
answerable by re-reading the factors, never "the model decided."

This engine only ever produces a SelectionResult with state SELECTED --
it never itself calls strategy.registry.activate() or flips a strategy's
status to ACTIVE. Turning a selection into a live-order-generating
strategy remains an explicit, separate, operator-driven step (mirrors
Stage 7's compare.py boundary: comparison/selection and activation are
structurally different responsibilities).

Eligibility (which strategies are even scored) is itself explainable and
fail-closed, not merely a low score:
  - status in {REJECTED, PAUSED} -> DISABLED (operator already turned it off)
  - status in {COLLECTED, STRUCTURED} -> INSUFFICIENT_DATA (not reviewed/
    backtested enough yet to score responsibly)
  - no backtest result, backtest status != OK, or fewer than
    MIN_TRADES_FOR_SCORING completed trades -> INSUFFICIENT_DATA
  - current market_state not in the strategy's documented preferred set
    -> MARKET_MISMATCH
  - otherwise: scored, and the single highest-scoring candidate (if any)
    is SELECTED, every other scored candidate is NOT_SELECTED.

Modules:
  models.py   -- SelectionState constants, SelectionInput/SelectionResult
                 dataclasses, SelectionFactors breakdown.
  scoring.py  -- per-factor scoring functions (all pure, all documented
                 ASSUMPTIONs where a specific number/threshold was chosen)
                 and the explicit composite-score weighting.
  engine.py   -- select_strategy(): eligibility gating + scoring + top-1
                 selection across a candidate pool.
"""
