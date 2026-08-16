"""Micro-order quantity calculation for the 30,000 KRW limited-live pilot.

The pilot capital (30,000 KRW) is a fixed, tiny budget -- naive sizing
(spend the whole budget on entry) would either buy zero shares of most
US equities after FX conversion, or (for very cheap stocks) tie up the
entire pilot budget in a single position with no room for a second
attempt if the first is stopped out. calculate_micro_order_quantity()
makes both failure modes explicit results (INSUFFICIENT_FUNDS /
BELOW_MINIMUM_ORDER_AMOUNT) instead of silently returning 0 or a
misleading number.
"""

from dataclasses import dataclass
from typing import Optional

STATUS_OK = "OK"
STATUS_INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"  # rounds down to 0 whole shares
STATUS_BELOW_MINIMUM_ORDER_AMOUNT = "BELOW_MINIMUM_ORDER_AMOUNT"  # affordable qty's cost is below the broker's minimum

DEFAULT_MIN_ORDER_AMOUNT_USD = 1.0
# ASSUMPTION: Alpaca's actual minimum order value for whole-share market
# orders is not confirmed for this account (TBD_OPERATOR, see
# docs/live_review/LIMITED_LIVE_30K_KRW_PLAYBOOK.md) -- $1.00 is a
# conservative placeholder floor, not a verified broker limit. Must be
# replaced with the real value before any live order is ever submitted.


class InvalidSizingInputError(Exception):
    pass


@dataclass
class SizingResult:
    status: str
    quantity: int
    estimated_cost_usd: Optional[float]
    budget_usd: float
    reason: str = ""


def calculate_micro_order_quantity(available_krw, fx_rate_krw_per_usd, share_price_usd, *,
                                    min_order_amount_usd=DEFAULT_MIN_ORDER_AMOUNT_USD,
                                    fractional_shares_allowed=False):
    """Convert a KRW budget to a whole-share USD order quantity.

    fractional_shares_allowed defaults to False: PROJECT_CONSTITUTION.md's
    v1.0 scope has never enabled Alpaca fractional-share trading, and this
    stage does not change that -- enabling fractional orders is a separate,
    explicit future decision, not something a sizing helper should assume.

    Fails closed rather than returning a misleading number:
      - non-positive inputs raise InvalidSizingInputError (programmer error,
        not a normal trading condition).
      - a budget that rounds down to 0 whole shares -> STATUS_INSUFFICIENT_FUNDS,
        quantity=0.
      - a budget that affords >=1 share but whose total cost is still below
        min_order_amount_usd -> STATUS_BELOW_MINIMUM_ORDER_AMOUNT, quantity=0
        (the affordable share count is never silently submitted below the
        broker's own minimum).
    """
    if available_krw <= 0:
        raise InvalidSizingInputError(f"available_krw must be positive, got {available_krw!r}")
    if fx_rate_krw_per_usd <= 0:
        raise InvalidSizingInputError(f"fx_rate_krw_per_usd must be positive, got {fx_rate_krw_per_usd!r}")
    if share_price_usd <= 0:
        raise InvalidSizingInputError(f"share_price_usd must be positive, got {share_price_usd!r}")

    budget_usd = available_krw / fx_rate_krw_per_usd

    if fractional_shares_allowed:
        quantity_value = budget_usd / share_price_usd
        estimated_cost = quantity_value * share_price_usd
        if estimated_cost < min_order_amount_usd:
            return SizingResult(STATUS_BELOW_MINIMUM_ORDER_AMOUNT, 0, None, budget_usd,
                                 reason=f"affordable value ${estimated_cost:.2f} < minimum ${min_order_amount_usd:.2f}")
        return SizingResult(STATUS_OK, quantity_value, estimated_cost, budget_usd)

    whole_shares = int(budget_usd // share_price_usd)
    if whole_shares < 1:
        return SizingResult(STATUS_INSUFFICIENT_FUNDS, 0, None, budget_usd,
                             reason=f"budget ${budget_usd:.2f} cannot afford even 1 share at ${share_price_usd:.2f}")

    estimated_cost = whole_shares * share_price_usd
    if estimated_cost < min_order_amount_usd:
        return SizingResult(STATUS_BELOW_MINIMUM_ORDER_AMOUNT, 0, None, budget_usd,
                             reason=f"affordable order value ${estimated_cost:.2f} < minimum ${min_order_amount_usd:.2f}")

    return SizingResult(STATUS_OK, whole_shares, estimated_cost, budget_usd)
