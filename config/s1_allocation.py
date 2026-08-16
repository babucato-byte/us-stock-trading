"""S1 Limited Live allocation policy (PHASE 4A §4).

Versioned so that a plan produced last month is reproducible this month:
every allocation records `ALLOCATION_VERSION`, and changing any number
below changes that string too. Two plans built a week apart are then
never silently compared under the assumption they used the same weights.

The distinction the whole file exists to keep
---------------------------------------------
    LIVE_CASH_LIMIT_PERCENT = 100     the ACCOUNT may be fully deployed
    MAX_SINGLE_POSITION_PCT = 0.35    ONE NAME may not be

"100% cash usage" is a statement about how much of the account is
allowed to be working. It is not permission for one symbol to be the
whole account, and the single-position cap outranks the rank weights
whenever they disagree -- see `allocator.allocate()`, which applies the
cap after the weight and takes the smaller.

PLANNED_* values are not applied
--------------------------------
`PLANNED_MAX_POSITION_COUNT` and the rank weights describe the shape the
pilot is being built toward. The values that actually gate an order are
`LIVE_ROLLOUT_MAX_POSITIONS` / `LIVE_ROLLOUT_MAX_DAILY_ENTRIES` /
`LIVE_ROLLOUT_MAX_QUANTITY`, all of which remain 1 in PHASE 4A. Nothing
here raises them, and a test asserts that.
"""

import os

ALLOCATION_VERSION = "s1_alloc_v1"

#: Fraction of the established cash pool the S1 pilot may deploy in
#: total. 100 means "all of it", subject to RESERVE_WEIGHT below.
LIVE_CASH_LIMIT_PERCENT = 100

#: How many positions the plan aims to hold. Descriptive: the allocator
#: produces at most this many funded ranks.
TARGET_POSITION_COUNT = 3

#: The shape being built toward. NOT applied -- the live cap is
#: LIVE_ROLLOUT_MAX_POSITIONS, which is 1.
PLANNED_MAX_POSITION_COUNT = 4

#: Weight per rank, highest scanner_score first. The list length is the
#: number of ranks that can be funded.
RANK_WEIGHTS = (0.35, 0.30, 0.25)

#: Never deployed. Kept back so an exit that has not settled, a fee, or
#: an FX movement cannot push the account negative -- this codebase
#: forbids negative cash, and a plan that deploys exactly 100% has no
#: room for the costs that land after the fill.
RESERVE_WEIGHT = 0.10

#: Hard ceiling for ONE symbol, as a fraction of the pool. Outranks the
#: rank weight in every case.
MAX_SINGLE_POSITION_PCT = 0.35


class AllocationConfigError(Exception):
    """The allocation policy is not internally consistent. Callers must
    treat this as a hard block -- there is no partial policy."""


def _env_float(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        raise AllocationConfigError(f"{name} must be a number, got {raw!r}")


def cash_limit_fraction() -> float:
    value = _env_float("S1_LIVE_CASH_LIMIT_PERCENT", float(LIVE_CASH_LIMIT_PERCENT))
    if not (0 < value <= 100):
        raise AllocationConfigError(
            f"LIVE_CASH_LIMIT_PERCENT must be in (0, 100], got {value!r}")
    return value / 100.0


def validate() -> bool:
    """Refuse a policy whose parts contradict each other.

    Checked on every allocation rather than once at import: a config
    edited into an inconsistent state must block new entries, not be
    discovered the next time the process restarts.
    """
    if not RANK_WEIGHTS:
        raise AllocationConfigError("RANK_WEIGHTS must not be empty")
    for weight in RANK_WEIGHTS:
        if not isinstance(weight, (int, float)) or isinstance(weight, bool) or weight <= 0:
            raise AllocationConfigError(f"every rank weight must be positive, got {weight!r}")
    if not (0 <= RESERVE_WEIGHT < 1):
        raise AllocationConfigError(f"RESERVE_WEIGHT must be in [0, 1), got {RESERVE_WEIGHT!r}")
    if not (0 < MAX_SINGLE_POSITION_PCT <= 1):
        raise AllocationConfigError(
            f"MAX_SINGLE_POSITION_PCT must be in (0, 1], got {MAX_SINGLE_POSITION_PCT!r}")

    total = sum(RANK_WEIGHTS) + RESERVE_WEIGHT
    # Exact equality on floats would fail on 0.35+0.30+0.25+0.10; the
    # tolerance is for representation, not for a sloppy policy.
    if abs(total - 1.0) > 1e-9:
        raise AllocationConfigError(
            f"RANK_WEIGHTS {RANK_WEIGHTS} plus RESERVE_WEIGHT {RESERVE_WEIGHT} "
            f"must sum to 1.0, got {total}")
    # A rank weight ABOVE the single-position cap is deliberately NOT an
    # error. The cap always takes precedence over the weight, so such a
    # config is safe -- the weight is simply clamped, and the allocator
    # records `capped_by="max_single_position_pct"` so the clamp is
    # visible rather than silent.
    #
    # Refusing the combination instead would make the cap unreachable:
    # if every weight must already be <= the cap, the cap can never bind
    # and is dead code dressed as a safety control. A guard that cannot
    # fire is worse than no guard, because it reads like protection.
    if TARGET_POSITION_COUNT > len(RANK_WEIGHTS):
        raise AllocationConfigError(
            f"TARGET_POSITION_COUNT {TARGET_POSITION_COUNT} exceeds the "
            f"{len(RANK_WEIGHTS)} configured rank weights")
    return True


def deployable_fraction() -> float:
    """Fraction of the pool that may be deployed in total, after reserve."""
    return cash_limit_fraction() * (1.0 - RESERVE_WEIGHT)


def as_dict() -> dict:
    return {
        "allocation_version": ALLOCATION_VERSION,
        "live_cash_limit_percent": LIVE_CASH_LIMIT_PERCENT,
        "target_position_count": TARGET_POSITION_COUNT,
        "planned_max_position_count": PLANNED_MAX_POSITION_COUNT,
        "rank_weights": list(RANK_WEIGHTS),
        "reserve_weight": RESERVE_WEIGHT,
        "max_single_position_pct": MAX_SINGLE_POSITION_PCT,
    }
