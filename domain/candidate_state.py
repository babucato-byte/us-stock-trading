"""One vocabulary for what a candidate is, from scan to position.

The confusion this replaces
---------------------------
The scanner, the trading runtime and Slack each had their own words --
"후보", "실거래 가능 후보", "연구용" -- and they did not mean the same
thing. A Slack line reporting "실거래 가능 0" was counting SCANNED rows
that no execution gate had ever been asked about, on a day a real BUY
went through. A count that can be zero while a fill happens is not a
count of anything.

READY_TO_BUY is not EXECUTABLE
------------------------------
This is the distinction the whole model exists for, and the one most
easily lost:

    READY_TO_BUY   the STRATEGY says yes. ORB, VWAP, EMA, volume,
                   freshness, extension -- facts about the market and
                   this symbol.

    EXECUTABLE     the ACCOUNT also says yes. Cash, orderability,
                   same-day re-entry, ownership, reconciliation,
                   duplicate protection, instrument eligibility, a
                   verified broker route -- facts about whether we may
                   act, which have nothing to do with whether we want
                   to.

A candidate can be READY all day and never be EXECUTABLE, and reporting
either as the other misleads in a different direction: calling READY
"executable" promises an order that cash will refuse, and calling
EXECUTABLE "ready" hides that the account was consulted at all.

BLOCKED is that gap, named. It means the strategy wanted this and the
account refused -- which is an operator's problem to look at, not a
strategy signal to tune.

No transition logic here
------------------------
Deliberately just the vocabulary and the ordering. The rules that MOVE a
candidate between states live where the decisions are made -- the watch,
the gate, the order path -- and duplicating them here would create a
second opinion about the same question.
"""

#: Scanner conditions matched. Nothing has been asked about the current
#: market or the account.
SCANNED = "SCANNED"

#: Being re-evaluated each minute against live data.
WATCHING = "WATCHING"

#: The strategy's entry conditions hold right now. NOT permission to buy.
READY_TO_BUY = "READY_TO_BUY"

#: READY, and every execution safety gate also passed.
EXECUTABLE = "EXECUTABLE"

#: An order has been sent to the broker.
BUY_SUBMITTED = "BUY_SUBMITTED"

#: A fill is confirmed and a position exists.
OPEN = "OPEN"

#: The strategy's own thesis broke. Not an account problem.
INVALIDATED = "INVALIDATED"

#: The strategy still wants it; the account will not allow it.
BLOCKED = "BLOCKED"

#: Reporting order, coarse to committed.
ORDER = (SCANNED, WATCHING, READY_TO_BUY, EXECUTABLE, BUY_SUBMITTED, OPEN)

ALL = ORDER + (INVALIDATED, BLOCKED)

#: States where no order exists and none has been attempted.
PRE_ORDER = frozenset({SCANNED, WATCHING, READY_TO_BUY, EXECUTABLE,
                       INVALIDATED, BLOCKED})

#: States where real money is committed or in flight.
COMMITTED = frozenset({BUY_SUBMITTED, OPEN})

#: The ONLY state that may be described as a tradeable candidate.
#:
#: §7: a scanner reporting on SCANNED rows has not consulted cash,
#: ownership, reconciliation or the broker route, and must not use the
#: words that imply it has.
TRADEABLE_STATES = frozenset({EXECUTABLE})

#: Phrases that assert an execution gate was passed. A report using one
#: of these about a candidate not in TRADEABLE_STATES is making a claim
#: nothing checked.
EXECUTION_CLAIM_PHRASES = (
    "실거래 가능",
    "tradeable",
    "executable",
)


def is_tradeable(state) -> bool:
    """May this candidate be described as tradeable?"""
    return state in TRADEABLE_STATES


def describes_execution(text) -> bool:
    """Does this wording claim an execution gate was passed?"""
    lowered = str(text or "").lower()
    return any(phrase.lower() in lowered for phrase in EXECUTION_CLAIM_PHRASES)


def rank_of(state) -> int:
    """Position in ORDER, or -1 for a terminal/off-path state.

    Lets a caller ask "did this advance?" without hard-coding the
    sequence in three places.
    """
    try:
        return ORDER.index(state)
    except ValueError:
        return -1


def advanced(previous, current) -> bool:
    """True when `current` is further along than `previous`."""
    return rank_of(current) > rank_of(previous)
