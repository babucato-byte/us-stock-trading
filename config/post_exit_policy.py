"""How long an exit stays worth watching, and what is measured.

Why there is a limit at all
---------------------------
Post-exit tracking answers one question: was this exit any good? A price
two weeks after a sale answers a different question -- what the market
did over two weeks -- and reading it as a verdict on the exit blames the
rule for news it could not have known. Every window here is short on
purpose, and `MAX_TRACKING_DAYS` is the ceiling no strategy may exceed.

Why the window is per strategy
------------------------------
S6 is an intraday breakout: its exit is either right or wrong by the
next close, and a week later the position it left has nothing to do with
the range it broke out of. S1 holds a trend across sessions, so the same
question needs longer to answer. One shared window would be wrong for
both -- too long to judge S6, too short to judge S1.

Sample thresholds
-----------------
`interpretation_for` states what a given number of trades licenses.
Nothing here changes a rule; it decides only what a result is allowed to
be called. Twelve trades is an observation, not evidence.
"""

from config import strategy_registry

#: No strategy may track an exit longer than this, whatever it asks for.
MAX_TRACKING_DAYS = 5

#: Trading days after the exit, per strategy slot.
TRACKING_DAYS_BY_SLOT = {
    strategy_registry.SLOT_S6: 1,
    strategy_registry.SLOT_S1: 3,
    strategy_registry.SLOT_S2: 2,
}

#: Used when a strategy has no entry above. Deliberately the shortest
#: useful window rather than the longest: a strategy nobody has thought
#: about yet should collect the least speculative data, not the most.
DEFAULT_TRACKING_DAYS = 1

# Intraday horizons, in minutes after the exit fill.
HORIZON_M5 = "M5"
HORIZON_M15 = "M15"
HORIZON_M30 = "M30"
HORIZON_M60 = "M60"
INTRADAY_HORIZONS = ((HORIZON_M5, 5), (HORIZON_M15, 15),
                     (HORIZON_M30, 30), (HORIZON_M60, 60))

HORIZON_SAME_DAY_CLOSE = "SAME_DAY_CLOSE"
HORIZON_NEXT_DAY_OPEN = "NEXT_DAY_OPEN"
HORIZON_NEXT_DAY_HIGH = "NEXT_DAY_HIGH"
HORIZON_NEXT_DAY_LOW = "NEXT_DAY_LOW"
HORIZON_NEXT_DAY_CLOSE = "NEXT_DAY_CLOSE"

NEXT_DAY_HORIZONS = (HORIZON_NEXT_DAY_OPEN, HORIZON_NEXT_DAY_HIGH,
                     HORIZON_NEXT_DAY_LOW, HORIZON_NEXT_DAY_CLOSE)

ALL_HORIZONS = (tuple(name for name, _m in INTRADAY_HORIZONS)
                + (HORIZON_SAME_DAY_CLOSE,) + NEXT_DAY_HORIZONS)

STATUS_TRACKING = "TRACKING"
STATUS_COMPLETED = "COMPLETED"

OBSERVATION_OK = "OK"
OBSERVATION_UNAVAILABLE = "UNAVAILABLE"

#: Recorded on the one DT trade that predates the same-day re-entry
#: block, so the regression case is findable in the data rather than
#: only in a commit message.
NOTE_REENTRY_POLICY_MISSING = "SAME_DAY_REENTRY_POLICY_MISSING"


def tracking_days_for(strategy_id) -> int:
    """Trading days of tracking this strategy gets, capped."""
    slot = strategy_registry.slot_for(strategy_id)
    asked = TRACKING_DAYS_BY_SLOT.get(slot, DEFAULT_TRACKING_DAYS)
    return max(1, min(int(asked), MAX_TRACKING_DAYS))


# Sample-size bands. The point is to make "we have 12 trades" and "we
# have 200 trades" produce different words, so a small sample cannot be
# reported in language that invites a rule change.
OBSERVATION_ONLY = "OBSERVATION_ONLY"
TREND_INDICATION = "TREND_INDICATION"
MODIFICATION_CANDIDATE = "MODIFICATION_CANDIDATE"
STRATEGY_REVIEW_CANDIDATE = "STRATEGY_REVIEW_CANDIDATE"


def interpretation_for(sample_count: int) -> str:
    """What this many completed trades licenses -- never a rule change.

    Even STRATEGY_REVIEW_CANDIDATE is a label on a report. Changing a
    live exit rule requires a backtest, a regression and a shadow run;
    no code path anywhere reads this value and edits a threshold.
    """
    n = int(sample_count or 0)
    if n < 20:
        return OBSERVATION_ONLY
    if n < 50:
        return TREND_INDICATION
    if n < 100:
        return MODIFICATION_CANDIDATE
    return STRATEGY_REVIEW_CANDIDATE
