"""How many positions may be open at once, globally and per strategy.

Status: IMPLEMENTED, NOT ACTIVATED
----------------------------------
The limits below are the DESIGN. Raising what the account may actually
hold is a real-money risk change and is the operator's decision, not a
side effect of merging this file, so `ACTIVE` is False and
`effective_limits()` returns the currently-live posture until someone
changes that deliberately.

That distinction is the whole point of the module: the shape of the rule
can be reviewed, tested and argued about now, while the number of shares
the account can lose money on stays exactly where it is.

The proposed matrix
-------------------
    global   2      the account may hold two positions at once
    S1       1      one HMA early-trend position
    S2       1      one volume-accumulation position

Both limits apply; neither is a fallback for the other. Two S1 positions
is refused by the S1 limit even though the global limit would allow two,
and one S1 plus one S2 is allowed by both. The global cap exists so that
adding a third strategy later cannot silently raise the account's total
exposure just by adding a row to the per-strategy table -- which is
precisely how limit systems usually fail.

Fail closed
-----------
An unknown strategy gets no allowance rather than an unlimited one. A
strategy that is not in the table has not had a limit agreed for it, and
"not yet decided" must not read as "no ceiling".
"""

from typing import Dict, Mapping, Optional

#: Flip to True only as a deliberate, approved risk change. Nothing in
#: this repository should set it from code, a test, or an env default:
#: the value is the record of a decision, and a decision that can be
#: made by a fixture is not one.
ACTIVE = False

#: The proposed matrix. Not in force while ACTIVE is False.
PROPOSED_GLOBAL_MAX = 2
PROPOSED_STRATEGY_MAX: Dict[str, int] = {
    "S1_HMA_EARLY_TREND_V1": 1,
    "S2_VOLUME_ACCUMULATION_V1": 1,
}

#: What is actually enforced today: one S1 position, and nothing else
#: trades. This mirrors the live posture -- S1 is LIMITED_LIVE and S2..S6
#: are DISCOVERY_ONLY -- rather than describing an intention.
CURRENT_GLOBAL_MAX = 1
CURRENT_STRATEGY_MAX: Dict[str, int] = {
    "S1_HMA_EARLY_TREND_V1": 1,
}

ALLOW = "ALLOW"
BLOCK_GLOBAL = "BLOCK_GLOBAL_LIMIT"
BLOCK_STRATEGY = "BLOCK_STRATEGY_LIMIT"
BLOCK_UNKNOWN_STRATEGY = "BLOCK_UNKNOWN_STRATEGY"


class LimitDecision:
    """Why an entry was allowed or refused, in a form a log can carry."""

    __slots__ = ("allowed", "reason", "detail", "limits")

    def __init__(self, allowed: bool, reason: str, detail: str = "",
                 limits: Optional[Dict[str, int]] = None):
        self.allowed, self.reason = allowed, reason
        self.detail, self.limits = detail, limits or {}

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"LimitDecision({self.allowed}, {self.reason!r}, {self.detail!r})"

    def as_dict(self) -> Dict[str, object]:
        return {"allowed": self.allowed, "reason": self.reason,
                "detail": self.detail, "limits": dict(self.limits)}


def effective_limits(active: Optional[bool] = None):
    """The limits in force. The proposed matrix only when ACTIVE.

    `active` is a parameter so a TEST can exercise the proposed matrix
    without changing what production enforces. A test that had to set the
    module flag to check the new shape would leave the flag set for
    whatever ran next, which is how an unapproved limit reaches an
    account.
    """
    if ACTIVE if active is None else active:
        return PROPOSED_GLOBAL_MAX, dict(PROPOSED_STRATEGY_MAX)
    return CURRENT_GLOBAL_MAX, dict(CURRENT_STRATEGY_MAX)


def check_entry(strategy_id: str, open_positions: Mapping[str, int], *,
                active: Optional[bool] = None) -> LimitDecision:
    """May `strategy_id` open one more position, given what is open now?

    `open_positions` maps strategy_id -> count currently held. It is the
    caller's job to supply a count that matches reality; this function
    decides, it does not reconcile.
    """
    global_max, strategy_max = effective_limits(active)
    counts = {str(k): int(v) for k, v in (open_positions or {}).items()}
    total = sum(counts.values())
    held = counts.get(str(strategy_id), 0)
    limits = {"global_max": global_max,
              "strategy_max": strategy_max.get(str(strategy_id))}

    if str(strategy_id) not in strategy_max:
        return LimitDecision(
            False, BLOCK_UNKNOWN_STRATEGY,
            f"{strategy_id} has no agreed limit; not-yet-decided is not "
            f"no-ceiling", limits)

    if held + 1 > strategy_max[str(strategy_id)]:
        return LimitDecision(
            False, BLOCK_STRATEGY,
            f"{strategy_id} holds {held}, limit {strategy_max[str(strategy_id)]}",
            limits)

    if total + 1 > global_max:
        return LimitDecision(
            False, BLOCK_GLOBAL,
            f"{total} open across all strategies, global limit {global_max}",
            limits)

    return LimitDecision(True, ALLOW,
                         f"{strategy_id} holds {held}, total {total}", limits)
