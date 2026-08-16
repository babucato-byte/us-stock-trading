"""Strategy plugins.

Extension pattern for adding a new strategy (ORB, EMA-pullback, a
Ross-Cameron-style momentum strategy, a YouTube-sourced strategy that has
worked through COLLECTED -> ... -> PAPER_APPROVED, etc.):

  1. Add `strategy/plugins/<your_strategy>.py` implementing
     `strategy.interface.TradingStrategy` (see
     `strategy/plugins/vwap_micro_pullback_v1.py` for a complete real
     example, and `strategy/plugins/_example_orb_stub.py` in this
     directory for a minimal skeleton).
  2. Give it a unique, stable `strategy_id` and construct it with whatever
     `status` it has actually earned (roadmap: a strategy starts at
     COLLECTED and only reaches ACTIVE after REVIEWED/BACKTESTED/
     PAPER_APPROVED/LIMITED_LIVE_APPROVED have each been genuinely done --
     never construct a new plugin pre-set to ACTIVE).
  3. Register an instance with a `StrategyRegistry`:

         from strategy.registry import StrategyRegistry
         from strategy.plugins.your_strategy import YourStrategy

         registry = StrategyRegistry()
         registry.register(YourStrategy())

     The registry enforces at registration time that strategy_id/version/
     status are all valid, and that registering a second ACTIVE strategy
     while one is already ACTIVE is rejected (constitution: "1차 전략:
     VWAP_MICRO_PULLBACK_MOMENTUM_V1 단 하나만 활성화한다").
  4. Nothing before ACTIVE status can generate real orders, structurally --
     see `strategy.registry.require_active` / `select_strategy_for_order`.
     This is enforced by the registry itself, not by each plugin
     remembering to check its own status.

This package intentionally does not auto-discover/import every module
under `strategy/plugins/` on import (no side-effecting registration at
import time) -- whatever process assembles the live registry decides
explicitly which plugins to construct and register, which keeps "what's
live" an explicit, auditable list rather than "whatever happens to be
importable."
"""
