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
from typing import FrozenSet, Optional

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


def _env_optional_int(env, name):
    """A count cap that may legitimately be absent.

    LIMITED_LIVE pinned these to 1 and 2 so the first real orders could
    be counted on one hand. That was scaffolding for a test, not a risk
    model, and leaving it in place would make the test's shape permanent.
    Unset -- or set to an empty value -- now means the cap is not
    enforced, and capacity is decided by the things that actually bound
    it: orderable cash, one position per symbol, same-day re-entry,
    ownership and reconciliation.

    A VALUE is still honoured. An operator who wants a hard ceiling sets
    one and gets it; what is gone is a ceiling nobody chose.
    """
    value = env.get(name)
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = int(value)
    except ValueError:
        raise LiveRolloutConfigError(f"{name} must be an integer, got {value!r}")
    if parsed < 1:
        raise LiveRolloutConfigError(
            f"{name} must be a positive int when set, got {parsed!r}; "
            "leave it empty to run without a count cap")
    return parsed


def _env_float(env, name, default):
    value = env.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        raise LiveRolloutConfigError(f"{name} must be a number, got {value!r}")


def _env_symbol_set(env, name, default):
    """Parse a symbol allow-list, keeping UNSET and EMPTY distinct.

    The distinction is the whole safety property, so it is preserved
    rather than collapsed:

    * variable ABSENT -> `default` (None) -- no operator symbol
      restriction. This is the NORMAL LIVE posture: what a strategy may
      buy is decided by the scanner and the execution gates, not by a
      hand-maintained list.
    * variable SET BUT EMPTY -> `frozenset()` -- deny everything. An
      operator who blanks the value is asking for a hard stop, and a
      truncated or half-written env file reads this way too.

    Collapsing the two would make a failed env load mean "every symbol
    is permitted", which is the one reading a missing file must never
    have.
    """
    value = env.get(name)
    if value is None:
        return default
    return frozenset(s.strip().upper() for s in value.split(",") if s.strip())


@dataclass(frozen=True)
class LiveRolloutConfig:
    enabled: bool
    #: None = no operator symbol restriction (NORMAL LIVE default).
    #: An empty frozenset is NOT the same thing -- it denies everything.
    allowed_symbols: Optional[FrozenSet[str]]
    max_quantity_per_order: Optional[int]
    max_open_positions: Optional[int]
    #: The cap EACH live strategy gets inside `max_open_positions`.
    #:
    #: The global cap alone cannot express the posture that is actually
    #: authorised. With S1 and S6 both live and a global cap of 2, a
    #: global-only limit permits S1 holding two names, which is a
    #: different and larger risk than one name each. Both caps are
    #: enforced and neither substitutes for the other: a strategy is
    #: blocked at its own cap even when the account has a free slot.
    max_positions_per_strategy: Optional[int]
    max_daily_entries: Optional[int]
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
            allowed_symbols=_env_symbol_set(
                mapping, "LIVE_ROLLOUT_ALLOWED_SYMBOLS", None),
            max_quantity_per_order=_env_optional_int(
                mapping, "LIVE_ROLLOUT_MAX_QUANTITY"),
            max_open_positions=_env_optional_int(
                mapping, "LIVE_ROLLOUT_MAX_POSITIONS"),
            max_positions_per_strategy=_env_optional_int(
                mapping, "LIVE_ROLLOUT_MAX_POSITIONS_PER_STRATEGY"),
            max_daily_entries=_env_optional_int(
                mapping, "LIVE_ROLLOUT_MAX_DAILY_ENTRIES"),
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
        # None means "not enforced". A SET value is still validated
        # exactly as before: an operator who chose a cap must get the
        # cap they chose, and a malformed one must never read as absent.
        for name in ("max_open_positions", "max_positions_per_strategy",
                     "max_daily_entries", "max_quantity_per_order"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise LiveRolloutConfigError(
                    f"{name} must be a positive int when set, got {value!r}")
        # A per-strategy cap above the global one is not a stricter
        # setting that happens to be unreachable -- it is a contradiction
        # between two numbers that are both meant to be enforced, and
        # the operator cannot be assumed to have meant either. Refusing
        # is the only reading that does not silently pick one.
        if (self.max_positions_per_strategy is not None
                and self.max_open_positions is not None
                and self.max_positions_per_strategy > self.max_open_positions):
            raise LiveRolloutConfigError(
                f"max_positions_per_strategy ({self.max_positions_per_strategy}) exceeds "
                f"max_open_positions ({self.max_open_positions}) -- a single strategy may "
                "never be allowed more slots than the whole account has")
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
