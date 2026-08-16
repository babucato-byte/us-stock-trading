"""Strategy registry (Stage 3).

Enforces, structurally rather than by convention (roadmap: "초기에는 최대
1개", constitution: "1차 전략: VWAP_MICRO_PULLBACK_MOMENTUM_V1 단 하나만
활성화한다"):

  (a) invalid strategy_id / version / status are rejected at registration
      time, fail-closed -- mirrors scalping_watchlist/models.py's sentinel
      validation and kill_switch_state.py's "reject unknown state, never
      guess" convention.
  (b) at most one registered strategy may have status ACTIVE at any time.
  (c) `get_active_strategy()` returns None when nothing is ACTIVE.
      `require_active(strategy_id)` / `select_strategy_for_order(strategy_id)`
      raise `StrategyNotActiveError` for any status other than ACTIVE,
      including PAPER_APPROVED and LIMITED_LIVE_APPROVED (roadmap: "ACTIVE
      이전 전략은 주문 엔진에 연결하지 않는다").

Deterministic "second ACTIVE" policy (documented choice, per Stage 3
instructions): registering or activating a strategy as ACTIVE while a
*different* strategy is already ACTIVE is rejected outright. Nothing is
auto-deactivated on the caller's behalf -- an operator (or, later, an
explicit automated promotion step) must first move the current ACTIVE
strategy to PAUSED/REJECTED before another can become ACTIVE. This mirrors
kill_switch_state.py's release()/activate() split: a state transition that
has safety consequences requires an explicit, separate call, never an
implicit side effect of an unrelated action.

Integration note (Stage 3 vs Stage 4): `paper_strategy_order.submit_order()`
has no `strategy_id` parameter today -- there is no strategy-lifecycle
concept anywhere in the current order path, only a single hardcoded
scoring function (`analyze_stock`) feeding it. Stage 4 (roadmap Phase 5,
position lifecycle) is what introduces a real "a strategy decided to
submit an order" call site. Wiring `require_active()` into
`paper_strategy_order.py` today would mean inventing a call site that
doesn't correspond to anything the codebase actually does yet, which the
Stage 3 instructions explicitly call out as unacceptable. `require_active()`
/ `select_strategy_for_order()` are therefore standalone, directly callable,
and directly tested here; Stage 4 calls them from the real
order-triggering path once that path exists.
"""

from strategy.interface import TradingStrategy
from strategy.status import ACTIVE, can_generate_orders, is_valid_status


class StrategyRegistrationError(Exception):
    """Raised when register()/activate() is given an invalid strategy or
    would violate the at-most-one-ACTIVE invariant."""


class StrategyNotActiveError(Exception):
    """Raised by require_active()/select_strategy_for_order() when the
    requested strategy's status is not ACTIVE (unknown strategy_id is
    treated the same way -- fail closed, not "assume inactive is fine")."""


class StrategyRegistry:
    def __init__(self):
        self._strategies = {}  # strategy_id -> TradingStrategy instance

    def register(self, strategy: TradingStrategy) -> None:
        """Register a strategy *instance* (not a class -- an instance is
        what actually carries strategy_id/version/status and the concrete
        method implementations a caller invokes).

        Fail-closed validation, in order:
          1. `strategy` must actually be a TradingStrategy (its own
             __init__ already validated strategy_id/version/status, but we
             re-check here defensively rather than trust a caller that
             mutated the attributes after construction).
          2. strategy_id must not already be registered.
          3. if status == ACTIVE, no *other* strategy_id may currently be
             ACTIVE.
        """
        if not isinstance(strategy, TradingStrategy):
            raise StrategyRegistrationError(
                f"register() requires a TradingStrategy instance, got {type(strategy)!r}"
            )
        if not isinstance(strategy.strategy_id, str) or not strategy.strategy_id.strip():
            raise StrategyRegistrationError(f"Invalid strategy_id: {strategy.strategy_id!r}")
        if not isinstance(strategy.version, str) or not strategy.version.strip():
            raise StrategyRegistrationError(f"Invalid version: {strategy.version!r}")
        if not is_valid_status(strategy.status):
            raise StrategyRegistrationError(f"Invalid status: {strategy.status!r}")

        if strategy.strategy_id in self._strategies:
            raise StrategyRegistrationError(
                f"strategy_id already registered: {strategy.strategy_id!r}"
            )

        if strategy.status == ACTIVE:
            current_active = self.get_active_strategy()
            if current_active is not None:
                raise StrategyRegistrationError(
                    f"Cannot register {strategy.strategy_id!r} as ACTIVE: "
                    f"{current_active.strategy_id!r} is already ACTIVE "
                    "(at most one ACTIVE strategy is allowed)"
                )

        self._strategies[strategy.strategy_id] = strategy

    def activate(self, strategy_id: str) -> None:
        """Move an already-registered strategy to ACTIVE. Same at-most-one
        enforcement as register(); rejects (does not auto-deactivate) if a
        different strategy is already ACTIVE."""
        strategy = self._strategies.get(strategy_id)
        if strategy is None:
            raise StrategyRegistrationError(f"Unknown strategy_id: {strategy_id!r}")

        current_active = self.get_active_strategy()
        if current_active is not None and current_active.strategy_id != strategy_id:
            raise StrategyRegistrationError(
                f"Cannot activate {strategy_id!r}: {current_active.strategy_id!r} "
                "is already ACTIVE (at most one ACTIVE strategy is allowed)"
            )
        strategy.status = ACTIVE

    def get(self, strategy_id: str):
        """Return the registered strategy instance, or None if unknown."""
        return self._strategies.get(strategy_id)

    def list_all(self):
        """Return all registered strategy instances (insertion order)."""
        return list(self._strategies.values())

    def get_active_strategy(self):
        """Return the single ACTIVE strategy instance, or None if none is
        ACTIVE."""
        for strategy in self._strategies.values():
            if strategy.status == ACTIVE:
                return strategy
        return None

    def require_active(self, strategy_id: str) -> TradingStrategy:
        """Return the registered strategy if and only if its status is
        ACTIVE. Raises StrategyNotActiveError otherwise -- unknown
        strategy_id, or any non-ACTIVE status (including PAPER_APPROVED /
        LIMITED_LIVE_APPROVED), all raise rather than silently allowing an
        order to proceed."""
        strategy = self._strategies.get(strategy_id)
        if strategy is None:
            raise StrategyNotActiveError(f"Unknown strategy_id: {strategy_id!r}")
        if not can_generate_orders(strategy.status):
            raise StrategyNotActiveError(
                f"Strategy {strategy_id!r} has status {strategy.status!r}; "
                "only ACTIVE strategies may generate orders"
            )
        return strategy

    def select_strategy_for_order(self, strategy_id: str) -> TradingStrategy:
        """Alias for require_active(), named for the order-submission call
        site's perspective ("select the strategy that is allowed to place
        this order"). Stage 4 calls this (or require_active) from the real
        order-triggering path once it exists -- see module docstring."""
        return self.require_active(strategy_id)


# A module-level default registry mirrors kill_switch_state.py's pattern of
# a single process-wide source of truth (there, a state file; here, an
# in-memory registry populated at process startup by whatever imports the
# plugins). Tests should construct their own StrategyRegistry() instances
# instead of using this one, exactly like tests point kill_switch_state at
# an isolated tmp_path file rather than mutating the real one.
default_registry = StrategyRegistry()


def get_active_strategy():
    return default_registry.get_active_strategy()


def require_active(strategy_id: str) -> TradingStrategy:
    return default_registry.require_active(strategy_id)


def select_strategy_for_order(strategy_id: str) -> TradingStrategy:
    return default_registry.select_strategy_for_order(strategy_id)
