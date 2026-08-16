"""Risk Engine -- converts a validated strategy signal's entry/stop prices
plus the Account Engine's authoritative snapshot into a risk-based
maximum quantity, per the layered architecture:

    Market Data -> Strategy Engine -> Signal -> Risk Engine ->
    Account Engine -> Sizing Engine -> Execution Engine -> Broker

This module NEVER uses a quantity the strategy might have attached to a
signal -- `strategy/interface.py::EvaluationResult` has no such field in
the first place (see PROJECT_CONSTITUTION.md), and even if a caller
passed one in, `compute_risk_decision()`'s signature has no parameter
for it. The only strategy-originated inputs here are `entry_price_usd`/
`stop_price_usd` (legitimate strategy outputs per the constitution),
never a quantity, never an account fact.

Every numeric input is validated finite before use; any invalid value
(None/NaN/Infinity/bool/non-numeric/non-positive-where-required) blocks
the ENTIRE decision -- `compute_risk_decision()` raises `RiskEngineError`
rather than silently computing with the other, valid values. A NaN
`daily_loss_remaining_krw` must never let `entry_price_usd`/
`stop_price_usd` alone size an unconstrained order.
"""

import math
import uuid
from dataclasses import dataclass
from typing import Optional


class RiskEngineError(Exception):
    """Raised whenever the risk decision cannot be safely computed.
    Callers must treat this as a hard block on the order -- there is no
    partial/best-effort risk decision."""


@dataclass(frozen=True)
class RiskDecision:
    risk_decision_id: str
    risk_based_qty: float
    risk_amount_krw: float
    stop_distance_usd: float
    daily_loss_remaining_krw: float
    max_risk_per_trade_krw: float


def _require_finite(value, name):
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RiskEngineError(f"{name} must be a number, got {value!r}")
    if not math.isfinite(value):
        raise RiskEngineError(f"{name} must be finite, got {value!r}")
    return value


def _require_positive_finite(value, name):
    _require_finite(value, name)
    if value <= 0:
        raise RiskEngineError(f"{name} must be positive, got {value!r}")
    return value


def compute_risk_decision(
    entry_price_usd, stop_price_usd, fx_rate_krw_per_usd,
    daily_loss_remaining_krw, *, max_risk_per_trade_krw=None,
    fractional_shares_allowed=False, now=None,
):
    """Returns a `RiskDecision` with `risk_based_qty` -- the largest
    quantity whose (entry_price - stop_price) * qty stays within the
    smaller of `daily_loss_remaining_krw` and `max_risk_per_trade_krw`
    (if supplied). Raises `RiskEngineError` -- never returns a partial
    result -- if any input is missing/non-finite/non-positive-where-
    required, or if `stop_price_usd` does not define a real risk (must
    be strictly below `entry_price_usd`).

    `daily_loss_remaining_krw` may be validated even though a caller
    passing 0 or a negative value is a legitimate "no more risk budget
    today" state -- that still produces `risk_based_qty=0`, a valid
    (not erroneous) decision, distinct from an invalid/NaN input which
    raises.
    """
    _require_positive_finite(entry_price_usd, "entry_price_usd")
    _require_positive_finite(stop_price_usd, "stop_price_usd")
    _require_positive_finite(fx_rate_krw_per_usd, "fx_rate_krw_per_usd")
    _require_finite(daily_loss_remaining_krw, "daily_loss_remaining_krw")
    if max_risk_per_trade_krw is not None:
        _require_positive_finite(max_risk_per_trade_krw, "max_risk_per_trade_krw")

    stop_distance_usd = entry_price_usd - stop_price_usd
    if stop_distance_usd <= 0:
        raise RiskEngineError(
            f"stop_price_usd {stop_price_usd!r} is not below entry_price_usd "
            f"{entry_price_usd!r} -- no defined risk"
        )

    effective_max_risk_krw = max(daily_loss_remaining_krw, 0.0)
    if max_risk_per_trade_krw is not None:
        effective_max_risk_krw = min(effective_max_risk_krw, max_risk_per_trade_krw)

    risk_per_share_krw = stop_distance_usd * fx_rate_krw_per_usd
    risk_based_qty = effective_max_risk_krw / risk_per_share_krw
    if not fractional_shares_allowed:
        risk_based_qty = math.floor(risk_based_qty)

    risk_based_qty = _require_finite(risk_based_qty, "risk_based_qty")
    risk_amount_krw = _require_finite(risk_based_qty * risk_per_share_krw, "risk_amount")

    return RiskDecision(
        risk_decision_id=f"risk-{uuid.uuid4().hex[:16]}",
        risk_based_qty=risk_based_qty,
        risk_amount_krw=risk_amount_krw,
        stop_distance_usd=stop_distance_usd,
        daily_loss_remaining_krw=daily_loss_remaining_krw,
        max_risk_per_trade_krw=max_risk_per_trade_krw if max_risk_per_trade_krw is not None
        else effective_max_risk_krw,
    )


def compute_daily_loss_remaining_krw(max_daily_loss_krw, current_daily_loss_krw):
    """`current_daily_loss_krw` is a POSITIVE number representing loss
    already incurred today (not a signed P&L). Both inputs validated
    finite; the result may legitimately be <= 0 (today's loss budget is
    exhausted) -- that is not itself an error."""
    _require_positive_finite(max_daily_loss_krw, "max_daily_loss_krw")
    _require_finite(current_daily_loss_krw, "current_daily_loss_krw")
    if current_daily_loss_krw < 0:
        raise RiskEngineError(
            f"current_daily_loss_krw must not be negative, got {current_daily_loss_krw!r}"
        )
    return max_daily_loss_krw - current_daily_loss_krw
