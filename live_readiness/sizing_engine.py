"""Sizing Engine -- the ONLY module that computes a final order quantity,
per the layered architecture:

    Market Data -> Strategy Engine -> Signal -> Risk Engine ->
    Account Engine -> Sizing Engine -> Execution Engine -> Broker

`compute_sizing_decision()` takes the Account Engine's
`available_for_new_order_krw`, the Risk Engine's `risk_based_qty`, and an
OPTIONAL strategy-side quantity cap (`strategy_max_qty` -- never a
strategy-declared final quantity, only an upper bound the strategy's own
setup logic may impose, e.g. "this setup shouldn't exceed N shares
regardless of budget"), and computes:

    balance_based_qty = available_for_new_order / buffered_entry_price   (fractionable)
                       = floor(available_for_new_order / buffered_entry_price)  (non-fractionable)
    actual_qty = min(balance_based_qty, risk_based_qty, strategy_max_qty)

`actual_qty` is only ever computed when all three candidates are
explicitly valid (finite, non-bool, non-string, non-negative) -- any
invalid input blocks the WHOLE decision via `SizingEngineError`, never a
partial/best-effort quantity. `strategy_max_qty=None` means "no cap" and
is simply excluded from the min(); an explicitly-supplied cap of exactly
0 is treated as an invalid input (same convention as CODEX-037's optional
numeric caps: "no cap" is spelled `None`, not `0`), not as "submit a
zero-quantity order".

`apply_entry_price_buffer()` is the single place a price gets adjusted
for expected slippage/upward drift before sizing -- callers must use its
output (not the raw estimated price) as `buffered_entry_price_usd`, and
Execution Engine recomputes this + `compute_sizing_decision()` fresh
immediately before the broker call (never reusing a sizing decision made
during watchlist/signal-time price discovery).
"""

import math
import uuid
from dataclasses import dataclass
from typing import Optional


class SizingEngineError(Exception):
    """Raised whenever a final quantity cannot be safely computed.
    Callers must treat this as a hard block on the order."""


@dataclass(frozen=True)
class SizingDecision:
    sizing_decision_id: str
    actual_qty: float
    balance_based_qty: float
    risk_based_qty: float
    strategy_max_qty: Optional[float]
    buffered_entry_price_usd: float
    below_minimum_order: bool


def apply_entry_price_buffer(estimated_entry_price_usd, *, buffer_bps=0.0, slippage_usd=0.0):
    """Returns a buffered entry price >= the raw estimate, accounting for
    expected upward price drift (`buffer_bps`, basis points) and expected
    slippage (`slippage_usd`, an absolute per-share amount). Both default
    to 0 (no buffer) so existing callers that don't yet supply a buffer
    policy see unchanged behavior."""
    if estimated_entry_price_usd is None or isinstance(estimated_entry_price_usd, bool) \
            or not isinstance(estimated_entry_price_usd, (int, float)) \
            or not math.isfinite(estimated_entry_price_usd) or estimated_entry_price_usd <= 0:
        raise SizingEngineError(
            f"estimated_entry_price_usd must be a positive finite number, got {estimated_entry_price_usd!r}"
        )
    for name, value in (("buffer_bps", buffer_bps), ("slippage_usd", slippage_usd)):
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value) or value < 0:
            raise SizingEngineError(f"{name} must be a non-negative finite number, got {value!r}")
    return estimated_entry_price_usd * (1 + buffer_bps / 10_000.0) + slippage_usd


def _validate_quantity_candidate(value, name, *, allow_none=False):
    if value is None:
        if allow_none:
            return None
        raise SizingEngineError(f"{name} must not be None")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SizingEngineError(f"{name} must be a number, got {value!r}")
    if not math.isfinite(value):
        raise SizingEngineError(f"{name} must be finite, got {value!r}")
    if value < 0:
        raise SizingEngineError(f"{name} must not be negative, got {value!r}")
    return value


def compute_sizing_decision(
    available_for_new_order_krw, buffered_entry_price_usd, fx_rate_krw_per_usd,
    fractionable, risk_based_qty, strategy_max_qty=None, *, min_order_amount_usd=1.0,
):
    """Raises `SizingEngineError` if any candidate is invalid. Returns a
    `SizingDecision` otherwise -- `actual_qty` may legitimately be 0 (no
    affordable/risk-permitted/strategy-permitted quantity), which is a
    valid result, not an error; callers must check for `actual_qty <= 0`
    themselves before proceeding to a reservation."""
    if available_for_new_order_krw is None or isinstance(available_for_new_order_krw, bool) \
            or not isinstance(available_for_new_order_krw, (int, float)) \
            or not math.isfinite(available_for_new_order_krw):
        raise SizingEngineError(
            f"available_for_new_order_krw must be a finite number, got {available_for_new_order_krw!r}"
        )
    if buffered_entry_price_usd is None or isinstance(buffered_entry_price_usd, bool) \
            or not isinstance(buffered_entry_price_usd, (int, float)) \
            or not math.isfinite(buffered_entry_price_usd) or buffered_entry_price_usd <= 0:
        raise SizingEngineError(
            f"buffered_entry_price_usd must be a positive finite number, got {buffered_entry_price_usd!r}"
        )
    if fx_rate_krw_per_usd is None or isinstance(fx_rate_krw_per_usd, bool) \
            or not isinstance(fx_rate_krw_per_usd, (int, float)) \
            or not math.isfinite(fx_rate_krw_per_usd) or fx_rate_krw_per_usd <= 0:
        raise SizingEngineError(
            f"fx_rate_krw_per_usd must be a positive finite number, got {fx_rate_krw_per_usd!r}"
        )
    risk_based_qty = _validate_quantity_candidate(risk_based_qty, "risk_based_qty")
    strategy_max_qty = _validate_quantity_candidate(
        strategy_max_qty, "strategy_max_qty", allow_none=True,
    )
    if strategy_max_qty == 0:
        raise SizingEngineError(
            "strategy_max_qty=0 is not a valid cap -- omit (None) for 'no cap', "
            "or supply a positive value"
        )
    if min_order_amount_usd is None or isinstance(min_order_amount_usd, bool) \
            or not isinstance(min_order_amount_usd, (int, float)) \
            or not math.isfinite(min_order_amount_usd) or min_order_amount_usd <= 0:
        raise SizingEngineError(
            f"min_order_amount_usd must be a positive finite number, got {min_order_amount_usd!r}"
        )

    budget_usd = max(available_for_new_order_krw, 0.0) / fx_rate_krw_per_usd
    if fractionable:
        balance_based_qty = budget_usd / buffered_entry_price_usd
    else:
        balance_based_qty = math.floor(budget_usd / buffered_entry_price_usd)

    below_minimum_order = (balance_based_qty * buffered_entry_price_usd) < min_order_amount_usd
    if below_minimum_order:
        balance_based_qty = 0

    candidates = [balance_based_qty, risk_based_qty]
    if strategy_max_qty is not None:
        strategy_qty = strategy_max_qty if fractionable else math.floor(strategy_max_qty)
        candidates.append(strategy_qty)

    actual_qty = min(candidates)
    if not fractionable:
        actual_qty = math.floor(actual_qty)
    actual_qty = max(actual_qty, 0)

    return SizingDecision(
        sizing_decision_id=f"sizing-{uuid.uuid4().hex[:16]}",
        actual_qty=actual_qty,
        balance_based_qty=balance_based_qty,
        risk_based_qty=risk_based_qty,
        strategy_max_qty=strategy_max_qty,
        buffered_entry_price_usd=buffered_entry_price_usd,
        below_minimum_order=below_minimum_order,
    )
