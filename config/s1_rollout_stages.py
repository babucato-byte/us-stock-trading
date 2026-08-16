"""Named rollout stages, their profiles, and what each one requires.

The distinction this file exists to hold
----------------------------------------
    PLANNED   what the pilot is being built toward -- simulated only
    ACTUAL    what `config/live_rollout_config.py` will let through

Nothing here changes the actual limits. `LIVE_ROLLOUT_MAX_QUANTITY`,
`LIVE_ROLLOUT_MAX_POSITIONS` and `LIVE_ROLLOUT_MAX_DAILY_ENTRIES` remain
1/1/1 and `LIVE_ROLLOUT_ENABLED` remains false; a stage profile is a
description of an intended configuration, and moving the real one is a
separate, deliberate act.

Why stages rather than one aggressive profile
---------------------------------------------
The aggressive shape -- three to four positions, five entries a day,
35/30/25 -- is the destination. It is a bad place to take the FIRST live
order, because the first order is not testing the strategy: it is testing
whether the wire format, the fill path, the position read and the
accounting all do what this codebase believes they do. That is a
one-share, one-position, one-entry question, and answering it with four
concurrent positions means a wrong answer arrives four times at once.

So STAGE 1 is deliberately the narrowest configuration the system can
express, and each later stage requires evidence the previous one
produced.

Promotion is not automatic
--------------------------
`requirements_for()` lists what a stage needs, and
`s1_live/readiness.py` evaluates them against real state. A few
profitable trades do not promote anything: the requirements are about
whether facts have been VERIFIED, not whether the account is up.
"""

STAGE_OBSERVE = "STAGE_0_OBSERVE"
STAGE_FIRST_LIVE = "STAGE_1_FIRST_LIVE_VALIDATION"
STAGE_LIMITED_ROTATION = "STAGE_2_LIMITED_ROTATION"
STAGE_AGGRESSIVE = "STAGE_3_AGGRESSIVE_LIMITED"

#: Ordered. Index is the stage number; promotion only ever moves one
#: step and only when every requirement of the target stage is met.
STAGE_ORDER = (STAGE_OBSERVE, STAGE_FIRST_LIVE, STAGE_LIMITED_ROTATION,
               STAGE_AGGRESSIVE)


class RolloutStageError(Exception):
    """A stage or profile could not be resolved. Callers treat this as
    STAGE_0 -- never as "use the highest one you found"."""


#: The destination shape. SIMULATION ONLY in PHASE 4D.
S1_AGGRESSIVE_V1 = {
    "profile": "S1_AGGRESSIVE_V1",
    "live_cash_limit_percent": 100,
    "target_positions": 3,
    "hard_max_positions": 4,
    "max_new_entries_per_day": 5,
    "rank_weights": (0.35, 0.30, 0.25),
    "reserve_weight": 0.10,
    "max_single_position_pct": 0.35,
    "max_quantity_per_order": None,   # cash-derived, not a share count
}

#: The first real order's configuration. The narrowest the system can
#: express, on purpose -- see the module docstring.
S1_FIRST_LIVE_VALIDATION = {
    "profile": "S1_FIRST_LIVE_VALIDATION",
    "live_cash_limit_percent": 100,
    "target_positions": 1,
    "hard_max_positions": 1,
    "max_new_entries_per_day": 1,
    "rank_weights": (0.35,),
    # The other 65% is RESERVE, not a second position. A one-position
    # stage that quietly deployed the rest would be a two-position stage
    # wearing the wrong name -- and the whole point of STAGE 1 is that
    # exactly one thing is in flight when the first fill is examined.
    "reserve_weight": 0.65,
    "max_single_position_pct": 0.35,
    "max_quantity_per_order": 1,
}

#: Two positions. The step that first exercises cash reservation between
#: candidates with real money -- which is why it is its own stage rather
#: than folded into the jump to four.
S1_LIMITED_ROTATION = {
    "profile": "S1_LIMITED_ROTATION",
    "live_cash_limit_percent": 100,
    "target_positions": 2,
    "hard_max_positions": 2,
    "max_new_entries_per_day": 2,
    "rank_weights": (0.35, 0.30),
    # Same rule: the 35% this stage does not rank stays uninvested.
    "reserve_weight": 0.35,
    "max_single_position_pct": 0.35,
    "max_quantity_per_order": None,
}

S1_OBSERVE = {
    "profile": "S1_OBSERVE",
    "live_cash_limit_percent": 0,
    "target_positions": 0,
    "hard_max_positions": 0,
    "max_new_entries_per_day": 0,
    "rank_weights": (),
    "reserve_weight": 1.0,
    "max_single_position_pct": 0.0,
    "max_quantity_per_order": 0,
}

PROFILES = {
    STAGE_OBSERVE: S1_OBSERVE,
    STAGE_FIRST_LIVE: S1_FIRST_LIVE_VALIDATION,
    STAGE_LIMITED_ROTATION: S1_LIMITED_ROTATION,
    STAGE_AGGRESSIVE: S1_AGGRESSIVE_V1,
}

# --------------------------------------------------------------------------
# Requirement keys. Each is evaluated by s1_live/readiness.py against real
# state; the names are shared so a requirement cannot be listed here and
# silently never checked.
# --------------------------------------------------------------------------

REQ_CANDIDATE_DECISION_DISABLED = "candidate_decision_disabled"
REQ_CANDIDATE_SOURCE = "s1_candidate_source"
REQ_ACCOUNT_CASH = "account_cash"
REQ_ACCOUNT_EQUITY = "account_equity"
REQ_START_EQUITY = "start_equity"
REQ_PEAK_EQUITY = "peak_equity"
REQ_DAILY_LOSS = "daily_loss"
REQ_DRAWDOWN = "drawdown"
REQ_KILL_SWITCH = "kill_switch"
REQ_RECONCILIATION = "reconciliation"
REQ_WHOLE_SHARE = "integer_quantity"
REQ_NO_DUPLICATE_ORDER = "duplicate_order_guard"
REQ_MINIMUM_ORDER = "minimum_order_amount"
REQ_EXIT_POLICY = "s1_exit_policy"
REQ_POSITION_VALUATION = "position_valuation"
REQ_RESERVED_ORDER_CASH = "reserved_order_cash"
REQ_FEES = "fee_accounting"

#: STAGE 1 -- everything needed before the FIRST real order.
#: `REQ_EXIT_POLICY` and `REQ_MINIMUM_ORDER` are here deliberately: an
#: entry with no defined exit is a position nobody has decided how to
#: close, and an order below the broker's minimum is a rejection this
#: codebase would record as a strategy outcome.
_STAGE_1_REQUIREMENTS = (
    REQ_CANDIDATE_DECISION_DISABLED, REQ_CANDIDATE_SOURCE,
    REQ_ACCOUNT_CASH, REQ_ACCOUNT_EQUITY, REQ_START_EQUITY, REQ_PEAK_EQUITY,
    REQ_DAILY_LOSS, REQ_DRAWDOWN, REQ_KILL_SWITCH, REQ_RECONCILIATION,
    REQ_WHOLE_SHARE, REQ_NO_DUPLICATE_ORDER,
    REQ_MINIMUM_ORDER, REQ_EXIT_POLICY,
)

#: STAGE 2 adds the thing only a real position can establish: that the
#: internally computed position value agrees with the broker's.
_STAGE_2_REQUIREMENTS = _STAGE_1_REQUIREMENTS + (REQ_POSITION_VALUATION,)

#: STAGE 3 adds what only concurrent orders and settled trades can:
#: whether an open order's cash is actually reserved, and whether fees
#: are reported well enough for net P&L to mean anything.
_STAGE_3_REQUIREMENTS = _STAGE_2_REQUIREMENTS + (REQ_RESERVED_ORDER_CASH, REQ_FEES)

STAGE_REQUIREMENTS = {
    STAGE_OBSERVE: (),
    STAGE_FIRST_LIVE: _STAGE_1_REQUIREMENTS,
    STAGE_LIMITED_ROTATION: _STAGE_2_REQUIREMENTS,
    STAGE_AGGRESSIVE: _STAGE_3_REQUIREMENTS,
}


def stage_index(stage) -> int:
    try:
        return STAGE_ORDER.index(stage)
    except ValueError:
        raise RolloutStageError(f"unknown rollout stage {stage!r}") from None


def profile_for(stage) -> dict:
    if stage not in PROFILES:
        raise RolloutStageError(f"unknown rollout stage {stage!r}")
    return dict(PROFILES[stage])


def requirements_for(stage):
    if stage not in STAGE_REQUIREMENTS:
        raise RolloutStageError(f"unknown rollout stage {stage!r}")
    return STAGE_REQUIREMENTS[stage]


def next_stage(stage):
    """The single step up, or None at the top. Never skips a stage."""
    index = stage_index(stage)
    return STAGE_ORDER[index + 1] if index + 1 < len(STAGE_ORDER) else None


def validate_profile(profile) -> bool:
    """A profile must be internally consistent on the same rules the
    live allocator enforces -- see config/s1_allocation.py."""
    weights = tuple(profile.get("rank_weights") or ())
    reserve = profile.get("reserve_weight")
    cap = profile.get("max_single_position_pct")
    if not weights:
        # OBSERVE has no ranks; reserve must then be everything.
        if reserve != 1.0:
            raise RolloutStageError(
                f"{profile.get('profile')}: no rank weights, so reserve must be 1.0")
        return True
    total = sum(weights) + reserve
    if abs(total - 1.0) > 1e-9:
        raise RolloutStageError(
            f"{profile.get('profile')}: rank weights {weights} plus reserve "
            f"{reserve} must sum to 1.0, got {total}")
    if not (0 < cap <= 1):
        raise RolloutStageError(f"{profile.get('profile')}: single-position cap {cap!r}")
    if profile.get("target_positions", 0) > profile.get("hard_max_positions", 0):
        raise RolloutStageError(
            f"{profile.get('profile')}: target positions exceed the hard maximum")
    if len(weights) > profile.get("hard_max_positions", 0):
        raise RolloutStageError(
            f"{profile.get('profile')}: more rank weights than positions allowed")
    return True
