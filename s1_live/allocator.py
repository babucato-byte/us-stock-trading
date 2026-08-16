"""Turn a ranked candidate list and a cash pool into whole-share budgets.

Pure. No broker, no database, no clock. Every account fact arrives as an
argument, which is what makes the dry-run and the live path able to share
one implementation instead of agreeing by inspection.

The three rules, in the order they apply
----------------------------------------
1. rank weight        rank 1 gets 35% of the pool, rank 2 30%, rank 3 25%
2. single-symbol cap  ...but never more than MAX_SINGLE_POSITION_PCT
3. remaining cash     ...and never more than is actually left

Rule 2 outranks rule 1 by construction (`min`), so raising a rank weight
above the cap cannot widen a position -- the config refuses that
combination outright anyway.

Cash is reserved as it is committed
-----------------------------------
The bug this prevents is specific and easy to write: evaluate three
candidates, ask "how much cash is available?" three times, get the same
answer three times, and commit 35% + 30% + 25% of a pool you only have
one of. Every allocation here subtracts from `remaining`, so the third
candidate sees what the first two already took.

The same applies to cash already committed elsewhere. `reserved_usd`
covers open buy orders and unsettled entries: money the account still
shows but has already promised.

Whole shares only
-----------------
`domain.cash_sizing.whole_shares_affordable` does the division, in
Decimal, flooring. Reused rather than reimplemented because a second
rounding rule is how one path buys a share the cash does not cover.
A budget that cannot afford one share is
`SKIP_INSUFFICIENT_POSITION_BUDGET` -- a recorded outcome, not a
silent zero.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config import s1_allocation
from domain.cash_sizing import is_usable_amount, whole_shares_affordable

logger = logging.getLogger(__name__)

STATUS_ALLOCATED = "ALLOCATED"
#: The rank's budget could not buy one whole share. Spec-named.
SKIP_INSUFFICIENT_POSITION_BUDGET = "SKIP_INSUFFICIENT_POSITION_BUDGET"
#: Earlier ranks consumed the deployable pool.
SKIP_NO_CASH_REMAINING = "SKIP_NO_CASH_REMAINING"
#: The broker did not give a usable orderable figure for this symbol.
#: Distinct from a real zero -- an outage must not read as "no money".
SKIP_ORDERABLE_UNKNOWN = "SKIP_ORDERABLE_UNKNOWN"
#: More candidates than configured rank weights.
SKIP_BEYOND_TARGET_COUNT = "SKIP_BEYOND_TARGET_COUNT"
#: The candidate carried no usable price.
SKIP_PRICE_UNKNOWN = "SKIP_PRICE_UNKNOWN"

#: Why a budget ended up where it did, for the audit trail.
CAP_RANK_WEIGHT = "rank_weight"
CAP_SINGLE_POSITION = "max_single_position_pct"
CAP_REMAINING_CASH = "remaining_cash"
CAP_ORDERABLE = "broker_orderable"


class AllocatorError(Exception):
    """The plan could not be computed. A hard block, never a partial plan."""


@dataclass(frozen=True)
class Allocation:
    symbol: str
    rank: int
    status: str
    weight: Optional[float] = None
    rank_budget_usd: Optional[float] = None
    budget_usd: Optional[float] = None
    capped_by: Optional[str] = None
    price_usd: Optional[float] = None
    orderable_usd: Optional[float] = None
    quantity: int = 0
    cost_usd: float = 0.0
    reason: str = ""

    @property
    def funded(self) -> bool:
        return self.status == STATUS_ALLOCATED and self.quantity > 0


@dataclass
class AllocationPlan:
    allocation_version: str
    cash_pool_usd: float
    deployable_usd: float
    reserve_usd: float
    reserved_usd: float
    allocations: List[Allocation] = field(default_factory=list)

    @property
    def funded(self) -> List[Allocation]:
        return [item for item in self.allocations if item.funded]

    @property
    def committed_usd(self) -> float:
        return round(sum(item.cost_usd for item in self.funded), 6)

    @property
    def remaining_usd(self) -> float:
        return round(self.deployable_usd - self.committed_usd, 6)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "allocation_version": self.allocation_version,
            "cash_pool_usd": round(self.cash_pool_usd, 6),
            "deployable_usd": round(self.deployable_usd, 6),
            "reserve_usd": round(self.reserve_usd, 6),
            "reserved_usd": round(self.reserved_usd, 6),
            "committed_usd": self.committed_usd,
            "remaining_usd": self.remaining_usd,
            "funded_count": len(self.funded),
            "allocations": [vars(item) for item in self.allocations],
        }


def _usable_price(value) -> Optional[float]:
    return float(value) if is_usable_amount(value) and float(value) > 0 else None


def allocate(candidates, *, cash_pool_usd, price_lookup, orderable_lookup=None,
             reserved_usd: float = 0.0) -> AllocationPlan:
    """Build the plan. `candidates` must already be in rank order.

    `price_lookup(symbol) -> price` is the price the order would actually
    be placed at (buffered, if the caller buffers). `orderable_lookup`,
    when supplied, is the broker's per-symbol answer and is applied as a
    CEILING on that symbol's budget -- never as a floor, and never as a
    substitute for the pool. KIS answers orderable cash per (symbol,
    exchange, limit price), so it is asked per candidate at the price the
    candidate would be ordered at.

    Raises `AllocatorError` only for an unusable pool or an inconsistent
    policy. A candidate that cannot be funded is a recorded skip, not an
    error: "nothing was affordable today" is a normal result.
    """
    s1_allocation.validate()

    if not is_usable_amount(cash_pool_usd):
        raise AllocatorError(
            f"cash pool must be a finite non-negative number, got {cash_pool_usd!r}")
    if not is_usable_amount(reserved_usd):
        raise AllocatorError(
            f"reserved cash must be a finite non-negative number, got {reserved_usd!r}")

    pool = float(cash_pool_usd)
    # Money the account still shows but has already promised to an open
    # order comes off the top, before any weight is applied.
    available = max(pool - float(reserved_usd), 0.0)
    deployable = available * s1_allocation.deployable_fraction()
    reserve = available - deployable
    single_cap = pool * s1_allocation.MAX_SINGLE_POSITION_PCT

    plan = AllocationPlan(
        allocation_version=s1_allocation.ALLOCATION_VERSION,
        cash_pool_usd=pool, deployable_usd=deployable, reserve_usd=reserve,
        reserved_usd=float(reserved_usd),
    )

    remaining = deployable
    weights = s1_allocation.RANK_WEIGHTS

    for index, candidate in enumerate(candidates):
        symbol = str(candidate.get("symbol") if isinstance(candidate, dict) else candidate).upper()
        rank = index + 1

        if index >= len(weights):
            plan.allocations.append(Allocation(
                symbol=symbol, rank=rank, status=SKIP_BEYOND_TARGET_COUNT,
                reason=f"only {len(weights)} rank weights are configured"))
            continue

        price = _usable_price(price_lookup(symbol))
        if price is None:
            plan.allocations.append(Allocation(
                symbol=symbol, rank=rank, status=SKIP_PRICE_UNKNOWN,
                reason="no usable price for this symbol"))
            continue

        weight = weights[index]
        rank_budget = pool * weight

        # The cap outranks the weight. Both are fractions of the POOL,
        # not of what happens to be left, so a symbol's ceiling does not
        # move because an earlier rank was cheap.
        budget = rank_budget
        capped_by = CAP_RANK_WEIGHT
        if single_cap < budget:
            budget, capped_by = single_cap, CAP_SINGLE_POSITION
        if remaining < budget:
            budget, capped_by = remaining, CAP_REMAINING_CASH

        orderable = None
        if orderable_lookup is not None:
            raw = orderable_lookup(symbol, price)
            if raw is None:
                plan.allocations.append(Allocation(
                    symbol=symbol, rank=rank, status=SKIP_ORDERABLE_UNKNOWN,
                    weight=weight, rank_budget_usd=round(rank_budget, 6),
                    price_usd=price,
                    reason="the broker did not report a usable orderable amount"))
                continue
            if not is_usable_amount(raw):
                plan.allocations.append(Allocation(
                    symbol=symbol, rank=rank, status=SKIP_ORDERABLE_UNKNOWN,
                    weight=weight, rank_budget_usd=round(rank_budget, 6),
                    price_usd=price,
                    reason=f"orderable amount {raw!r} is not a usable number"))
                continue
            orderable = float(raw)
            if orderable < budget:
                budget, capped_by = orderable, CAP_ORDERABLE

        if budget <= 0:
            plan.allocations.append(Allocation(
                symbol=symbol, rank=rank, status=SKIP_NO_CASH_REMAINING,
                weight=weight, rank_budget_usd=round(rank_budget, 6),
                budget_usd=0.0, capped_by=capped_by, price_usd=price,
                orderable_usd=orderable,
                reason="no deployable cash left for this rank"))
            continue

        quantity = whole_shares_affordable(budget, price)
        if quantity < 1:
            plan.allocations.append(Allocation(
                symbol=symbol, rank=rank, status=SKIP_INSUFFICIENT_POSITION_BUDGET,
                weight=weight, rank_budget_usd=round(rank_budget, 6),
                budget_usd=round(budget, 6), capped_by=capped_by, price_usd=price,
                orderable_usd=orderable, quantity=0,
                reason=f"budget ${budget:.2f} cannot afford one share at ${price:.2f}"))
            continue

        cost = quantity * price
        remaining -= cost
        plan.allocations.append(Allocation(
            symbol=symbol, rank=rank, status=STATUS_ALLOCATED,
            weight=weight, rank_budget_usd=round(rank_budget, 6),
            budget_usd=round(budget, 6), capped_by=capped_by, price_usd=price,
            orderable_usd=orderable, quantity=int(quantity), cost_usd=round(cost, 6),
            reason=""))

    logger.info("allocation plan: pool=%.2f deployable=%.2f funded=%s committed=%.2f",
                pool, deployable, len(plan.funded), plan.committed_usd)
    return plan
