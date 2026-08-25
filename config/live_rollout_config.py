"""live_rollout policy (spec §19) -- every initial-rollout limit as a
validated, env-overridable setting, never a quantity hardcoded into
order-path code. Mirrors live_readiness/trusted_operator_config.py's
established pattern in this codebase (fail-closed `get_*()` functions,
never bare module attributes, so a corrupted env value blocks new
entries instead of silently using a garbage number).

`enabled` defaults to False -- spec §19's `live_rollout.enabled: false`
starting point. Every boolean policy flag below defaults to the SAFEST
(most restrictive) value spec §19/§30 requires: no fractional, no
market orders, no extended hours, no leverage, no inverse, no short, no
margin. These are not meant to be loosened by editing this file casually
-- spec §19 explicitly says later expansion should be config-only, but
"config-only" still means a deliberate, reviewed change to the deployed
environment, not a silent default drift.
"""

import math
import os
from dataclasses import dataclass, field
from typing import FrozenSet

from dotenv import load_dotenv

load_dotenv()


class LiveRolloutConfigError(Exception):
    """Raised when a live_rollout setting cannot be safely validated.
    Callers must treat this as a hard block on new entries."""


def _env_bool(env, name, default):
    value = env.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(env, name, default):
    value = env.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        raise LiveRolloutConfigError(f"{name} must be an integer, got {value!r}")


def _env_float(env, name, default):
    value = env.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        raise LiveRolloutConfigError(f"{name} must be a number, got {value!r}")


def _env_symbol_set(env, name, default):
    value = env.get(name)
    if value is None:
        return default
    return frozenset(s.strip().upper() for s in value.split(",") if s.strip())


@dataclass(frozen=True)
class LiveRolloutConfig:
    enabled: bool
    allowed_symbols: FrozenSet[str]
    max_quantity_per_order: int
    max_open_positions: int
    #: The cap EACH live strategy gets inside `max_open_positions`.
    #:
    #: The global cap alone cannot express the posture that is actually
    #: authorised. With S1 and S6 both live and a global cap of 2, a
    #: global-only limit permits S1 holding two names, which is a
    #: different and larger risk than one name each. Both caps are
    #: enforced and neither substitutes for the other: a strategy is
    #: blocked at its own cap even when the account has a free slot.
    max_positions_per_strategy: int
    max_daily_entries: int
    regular_session_only: bool
    allow_fractional: bool
    allow_market_order: bool
    allow_extended_hours: bool
    allow_leverage: bool
    allow_inverse: bool
    allow_short: bool
    allow_margin: bool
    max_price_deviation_percent: float

    @classmethod
    def from_env(cls, env=None):
        mapping = env if env is not None else os.environ
        return cls(
            enabled=_env_bool(mapping, "LIVE_ROLLOUT_ENABLED", False),
            allowed_symbols=_env_symbol_set(mapping, "LIVE_ROLLOUT_ALLOWED_SYMBOLS", frozenset()),
            max_quantity_per_order=_env_int(mapping, "LIVE_ROLLOUT_MAX_QUANTITY", 1),
            max_open_positions=_env_int(mapping, "LIVE_ROLLOUT_MAX_POSITIONS", 1),
            max_positions_per_strategy=_env_int(
                mapping, "LIVE_ROLLOUT_MAX_POSITIONS_PER_STRATEGY", 1),
            max_daily_entries=_env_int(mapping, "LIVE_ROLLOUT_MAX_DAILY_ENTRIES", 1),
            regular_session_only=_env_bool(mapping, "REGULAR_SESSION_ONLY", True),
            allow_fractional=_env_bool(mapping, "ALLOW_FRACTIONAL", False),
            allow_market_order=_env_bool(mapping, "MARKET_ORDER_ENABLED", False),
            allow_extended_hours=_env_bool(mapping, "EXTENDED_HOURS_ENABLED", False),
            allow_leverage=_env_bool(mapping, "ALLOW_LEVERAGE", False),
            allow_inverse=_env_bool(mapping, "ALLOW_INVERSE", False),
            allow_short=_env_bool(mapping, "ALLOW_SHORT", False),
            allow_margin=_env_bool(mapping, "ALLOW_MARGIN", False),
            max_price_deviation_percent=_env_float(mapping, "MAX_PRICE_DEVIATION_PERCENT", 0.30),
        )

    def validate(self):
        """Raises LiveRolloutConfigError on any structurally-unsafe
        combination. Called by the pipeline before every entry attempt
        (fail-closed re-validation, not just at process startup)."""
        if isinstance(self.max_quantity_per_order, bool) or not isinstance(self.max_quantity_per_order, int) \
                or self.max_quantity_per_order < 1:
            raise LiveRolloutConfigError(f"max_quantity_per_order must be a positive int, got {self.max_quantity_per_order!r}")
        if isinstance(self.max_open_positions, bool) or not isinstance(self.max_open_positions, int) \
                or self.max_open_positions < 1:
            raise LiveRolloutConfigError(f"max_open_positions must be a positive int, got {self.max_open_positions!r}")
        if isinstance(self.max_positions_per_strategy, bool) \
                or not isinstance(self.max_positions_per_strategy, int) \
                or self.max_positions_per_strategy < 1:
            raise LiveRolloutConfigError(
                f"max_positions_per_strategy must be a positive int, got "
                f"{self.max_positions_per_strategy!r}")
        # A per-strategy cap above the global one is not a stricter
        # setting that happens to be unreachable -- it is a contradiction
        # between two numbers that are both meant to be enforced, and
        # the operator cannot be assumed to have meant either. Refusing
        # is the only reading that does not silently pick one.
        if self.max_positions_per_strategy > self.max_open_positions:
            raise LiveRolloutConfigError(
                f"max_positions_per_strategy ({self.max_positions_per_strategy}) exceeds "
                f"max_open_positions ({self.max_open_positions}) -- a single strategy may "
                "never be allowed more slots than the whole account has")
        if isinstance(self.max_daily_entries, bool) or not isinstance(self.max_daily_entries, int) \
                or self.max_daily_entries < 1:
            raise LiveRolloutConfigError(f"max_daily_entries must be a positive int, got {self.max_daily_entries!r}")
        if not isinstance(self.max_price_deviation_percent, (int, float)) \
                or isinstance(self.max_price_deviation_percent, bool) \
                or not math.isfinite(self.max_price_deviation_percent) or self.max_price_deviation_percent <= 0:
            raise LiveRolloutConfigError(
                f"max_price_deviation_percent must be a positive finite number, got "
                f"{self.max_price_deviation_percent!r}"
            )
        if self.allow_fractional:
            raise LiveRolloutConfigError("allow_fractional=True is not permitted -- spec §19/§30 forbids fractional live orders in this pilot")
        if self.allow_market_order:
            raise LiveRolloutConfigError("allow_market_order=True is not permitted -- spec §19/§30 forbids market orders in this pilot")
        if self.allow_extended_hours:
            raise LiveRolloutConfigError("allow_extended_hours=True is not permitted -- spec §19/§30 forbids premarket/afterhours orders in this pilot")
        if self.allow_leverage:
            raise LiveRolloutConfigError("allow_leverage=True is not permitted -- spec §19/§30 forbids leveraged products in this pilot")
        if self.allow_inverse:
            raise LiveRolloutConfigError("allow_inverse=True is not permitted -- spec §19/§30 forbids inverse products in this pilot")
        if self.allow_short:
            raise LiveRolloutConfigError("allow_short=True is not permitted -- spec §19/§30 forbids short selling in this pilot")
        if self.allow_margin:
            raise LiveRolloutConfigError("allow_margin=True is not permitted -- spec §19/§30 forbids margin in this pilot")
        return True
