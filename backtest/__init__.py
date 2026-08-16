"""Stage 7 (사용자 지시서): intraday strategy backtest/replay engine.

Feeds a strategy's own `generate_entry()`/`invalidate()` (Stage 3's
TradingStrategy interface) through historical 1-minute bars bar-by-bar,
simulating the same exit rules Stage 4's positions/lifecycle.py enforces
live (1R 50% partial, 2R/stop-loss full exit, time-stop, EOD forced
close), with fees/spread/slippage/entry-delay/partial-fill assumptions
applied to every fill. Produces a list of simulated Trade records and
summary metrics (win rate, average R, Profit Factor, Expectancy, MDD,
consecutive losses, several sensitivity breakdowns, best-trade-removed
result).

Look-ahead prevention: at simulated bar index i, the engine only ever
passes `bars.iloc[:i+1]` to the strategy -- it can never see a future bar
when deciding whether to enter, exit, or invalidate. This is enforced
structurally by engine.py's loop, not left to strategy authors to get
right themselves.

This package produces no real orders and touches no operational file --
it is pure historical replay against a caller-supplied bar DataFrame.

Modules:
  config.py   -- BacktestConfig (fees/spread/slippage/entry-delay/same-bar
                 collision policy), all documented ASSUMPTIONs since none
                 of these numbers were specified by the user's instruction.
  models.py   -- Trade / ExitEvent dataclasses.
  engine.py   -- run_backtest(): the bar-by-bar replay loop.
  metrics.py  -- compute_metrics(): win rate/avg R/PF/expectancy/MDD/
                 consecutive losses/best-trade-removed, plus
                 time-of-day/price-range/liquidity/slippage-sensitivity
                 breakdowns.
  compare.py  -- compare_strategies(): side-by-side metrics table across
                 multiple strategies' backtest results (no scoring/
                 ranking judgment -- that is Stage 8's explicit scope).
"""
