"""S2_EXIT_V0: the trade ends when the reason for it does.

The centre of this policy is not a stop
---------------------------------------
S2 enters because volume arrived and the price confirmed it. So the exit
that matters is the same statement running backwards: the volume
momentum that justified the position has drained AND the price has
stopped being carried by it. That is one condition with two halves, and
neither half alone is the signal.

Decay alone is not an exit. Volume fading while the price keeps making
progress above VWAP is a normal, profitable shape -- the move continuing
on lighter participation -- and selling it would systematically cut the
trades that worked. So a decayed position that is still strong is given
a short confirmation window and only exits if the decay persists while
the price does nothing.

Price weakness alone is not a volume exit either. It is handled on its
own terms: a VWAP failure is a VWAP failure and a break of HMA200 is a
structure failure, and both are reported as themselves.

The exit does not look at PnL
-----------------------------
Nothing in this module reads unrealised profit, and no threshold is
expressed in R or in percent-from-entry except the catastrophic cap. When
the volume case dies the position closes; whether that books a profit or
a loss is an outcome, not an input. A rule that exited differently above
and below water would be two strategies sharing a name, and the one that
runs in a drawdown is always the untested one.

The hard cap is not the strategy
--------------------------------
`S2_LIMITED_LIVE_MAX_LOSS_PCT` is catastrophic protection. It exists so a
gap or a broken data feed cannot turn one position into the worst trade
of the month while the structural exits wait for an observation that
never arrives. A normal S2 exit should reach the structural conditions
first, and how often the cap fires instead is itself a measurement worth
having -- if it is the usual reason, the structural exits are too slow
and that is a finding, not a tuning problem.

It is named for its purpose rather than as a strategy parameter. A
constant called `STOP_LOSS_PCT` acquires authority it never earned, and
six weeks on nobody remembers whether 3.0 was measured or chosen. It was
chosen: S2 has roughly four trading days behind it, which is not enough
to measure a stop. S1's −6% is deliberately not reused -- that level was
measured on a trend strategy built to sit through noise, and borrowing it
would have looked justified precisely because it already exists here.
"""

# --- the volume-momentum core --------------------------------------------

#: How much of the signal's EXCESS volume over baseline must drain before
#: the volume case counts as decayed. Half is a description -- "the
#: excess has halved" -- not a tuned level, and the raw ratio is stored
#: on every row so a later study recomputes rather than inherits it.
#:
#: Measured against the PEAK the position reached, not against the entry
#: multiple. Momentum that built to 8x and fell to 4x has decayed even
#: though 4x is still far above the 1.5x that triggered the scan, and
#: measuring from entry would call that position untouched.
VOLUME_DECAY_FRACTION = 0.5
EXIT_ON_VOLUME_DECAY = True

#: Decay while the price is still working does not exit immediately. The
#: window is two ticks of the executor's own 15-minute cadence: one
#: observation can be a thin print or a single quiet bar, and two
#: consecutive is the least that separates a reading from a condition.
#: It is a debounce, not a tuned holding period.
VOLUME_DECAY_CONFIRMATION_MINUTES = 30

# --- price weakness, on its own terms ------------------------------------

#: A close below VWAP means the day's volume traded above the current
#: price. Paired with decay it is the compound exit; alone it is still an
#: exit, reported as itself.
EXIT_ON_VWAP_FAILURE = True

#: S2's own entry conditions, re-checked. The thresholds live in
#: `scanners/accumulation/config.json` and are read from there; a copy
#: here could drift from the one the scanner actually applied.
EXIT_ON_STRUCTURE_FAILURE = True

#: Price back below where it stood when volume peaked: the move the
#: volume produced has been given back. This is a comparison against the
#: position's own history, with no level to choose.
EXIT_ON_MOMENTUM_REVERSAL = True

# --- catastrophic protection ---------------------------------------------

#: Maximum accepted loss per S2 position during limited-live validation,
#: in percent of entry price. Fallback protection, not the exit strategy.
S2_LIMITED_LIVE_MAX_LOSS_PCT = 3.0

MAX_LOSS_IS_MEASURED = False
MAX_LOSS_BASIS = (
    "S2_LIMITED_LIVE_CATASTROPHIC_CAP: operator-agreed maximum loss for the "
    "validation phase, not an optimised level and not S1's. Normal S2 exits "
    "should be reached by volume/price structure first; the cap firing as "
    "the usual reason is a finding about the structural exits, not a "
    "tuning problem")

#: What must be compared once a sample exists. Listed in code so the
#: review has an agenda rather than an intention.
REEVALUATION_CANDIDATES = ("-2.0%", "-2.5%", "-3.0%", "ATR_VOLATILITY_BASED",
                           "VWAP_STRUCTURE_BASED")

# --- validation-phase constraints ----------------------------------------

#: No position survives the end of its session while limited-live is
#: being validated. A gap through a session boundary is exposed to
#: nothing an intraday rule can protect against.
ALLOW_OVERNIGHT_CARRY = False
EXIT_ON_SESSION_END = True

#: How long before the session ends the exit is raised: the width of the
#: executor's own tick, so a position cannot reach the boundary by being
#: evaluated one tick too late.
SESSION_EXIT_LEAD_MINUTES = 15


def max_loss_stop_price(entry_price):
    """The catastrophic cap as a price, or None if entry is unreadable."""
    try:
        entry = float(entry_price)
    except (TypeError, ValueError):
        return None
    if entry != entry or entry <= 0:
        return None
    return entry * (1.0 - S2_LIMITED_LIVE_MAX_LOSS_PCT / 100.0)
