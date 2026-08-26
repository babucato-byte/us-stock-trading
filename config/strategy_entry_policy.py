"""Which strategies may OPEN a position, asked separately from which may CLOSE one.

Why this is not a live-mode value
---------------------------------
Standing a strategy down was first attempted by moving it in
`scanner_live_mode` from LIMITED_LIVE to DISCOVERY_ONLY. That table
answers a different question -- "is this scanner's output allowed to
reach a live order path at all" -- and turning it down took the
scanner's publisher with it (`S1PublishRefused`), which is not what
"stop opening new positions" means. It also cannot express the thing
that actually matters here, because it has no notion of a side.

The distinction that matters
----------------------------
    ENTRY   opening a NEW position      may be withdrawn at any time
    EXIT    leaving one already held    must never be withdrawn

A stand-down that also removes the exit is not a stand-down, it is a
trap: capital is committed, the thesis has been abandoned, and the one
operation that would end the exposure has been disabled along with the
one that created it. So exit permission is not configurable here. There
is a function for it, and it returns True.

Fail closed one way, fail open the other
----------------------------------------
An unrecognised strategy gets NO entry permission -- "not yet decided"
must not read as "allowed". The same unknown gets FULL exit permission,
for the same reason inverted: a position whose owner cannot be
identified still has to be closable, and refusing that would strand it.
The two defaults point in opposite directions because the harm does.
"""

from typing import Dict

from config import strategy_registry

ENTRY_ENABLED = "ENABLED"
ENTRY_DISABLED = "DISABLED"

#: Slot -> entry permission. Keyed by SLOT rather than by any one of the
#: several names each strategy goes by, so `hma_early_trend`,
#: `S1_HMA_EARLY_TREND_V1` and the legacy signal id all resolve to one
#: decision instead of three that can disagree.
STRATEGY_ENTRY_POLICY: Dict[str, str] = {
    # S1 is stood down for NEW entries while its post-scanner-change
    # validation runs. Its scanner keeps running and keeps publishing,
    # and the held TX position keeps its full exit capability -- see
    # `exit_enabled`, which this table cannot reach.
    strategy_registry.SLOT_S1: ENTRY_DISABLED,
    # S2 has stood down; S6 replaced it as the fast-turnover validation
    # strategy.
    strategy_registry.SLOT_S2: ENTRY_DISABLED,
    # S6 is the strategy under limited-live validation.
    strategy_registry.SLOT_S6: ENTRY_ENABLED,
}


def entry_enabled(strategy) -> bool:
    """May this strategy OPEN a new position?

    Accepts any spelling the registry knows. An unrecognised strategy is
    refused: a cap or a permission that has not been agreed is not one
    that defaults to yes.
    """
    slot = strategy_registry.slot_for(strategy)
    if slot is None:
        return False
    return STRATEGY_ENTRY_POLICY.get(slot) == ENTRY_ENABLED


def exit_enabled(strategy) -> bool:  # noqa: ARG001 - the argument is the point
    """May this strategy CLOSE a position it holds? Always yes.

    Deliberately takes the strategy and ignores it. The signature says
    the question was asked per strategy; the body says the answer cannot
    be configured to no. Anything that wants to stop an exit has to
    delete this function and face every caller, rather than flipping a
    table entry.
    """
    return True


def entry_disabled_slots():
    """The slots currently barred from opening, for reports."""
    return tuple(s for s in strategy_registry.LIVE_SLOTS
                 if STRATEGY_ENTRY_POLICY.get(s) != ENTRY_ENABLED)
