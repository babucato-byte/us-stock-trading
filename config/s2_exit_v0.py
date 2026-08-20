"""S2_EXIT_V0 policy constants -- and the ones deliberately absent.

Why this is not S1_EXIT_V0 with different numbers
-------------------------------------------------
S1's exit is an R-multiple ladder: a −6% hard stop, a breakeven floor at
+1R, a +1R floor at +2R. Those levels were chosen against S1's measured
behaviour. S2 has roughly four trading days behind it, which is not
enough to choose a single one of them -- and a number chosen without
evidence does not become evidence by being written in a config file. It
becomes a constant nobody can argue with later, because its origin is
lost the moment it ships.

So S2_EXIT_V0 is STRUCTURAL. Every condition below is one of two things:

  * S2's own ENTRY condition, re-checked. If the reason the scanner
    flagged the symbol has stopped being true, the position no longer
    matches the thesis it was opened on. The thresholds come from
    `scanners/accumulation/config.json` and are not restated here --
    restating them would create a second copy that could drift.

  * A level with no free parameter. VWAP is where the day's volume
    actually traded; it is computed, not chosen. "Volume has decayed"
    is measured against the candidate's OWN signal-time multiple, so a
    6x candidate and a 1.6x candidate are judged on the same relative
    scale rather than a shared absolute one.

What is missing, and why that matters
-------------------------------------
There is NO PRICE STOP here. Not an oversight -- there is no honest way
to choose one yet, and the two dishonest ways are both worse than
absence:

  * borrowing S1's −6% would apply a level measured on a trend strategy
    to an accumulation strategy, and the number would look justified
    because it appears elsewhere in the repo;
  * picking a round number now would fix S2's risk profile by accident
    and make the month-1 review a formality.

`REQUIRES_STOP_BEFORE_LIVE` records this as a live-activation blocker
rather than leaving it to be noticed. S2 is DISCOVERY_ONLY today, so
nothing is exposed; the day someone proposes making it live, the stop is
a decision that has to be made explicitly by the operator, and it is a
real-money risk decision rather than a coding one.
"""

#: A position whose entry thesis has stopped being true is closed. The
#: thresholds live in the scanner's config and are read from there.
EXIT_ON_THESIS_INVALIDATION = True

#: A close below VWAP means the day's volume traded higher than the
#: current price. For a strategy whose premise is "volume arrives before
#: the move", that is the premise failing rather than the trade being
#: down.
EXIT_ON_VWAP_LOSS = True

#: The fraction of the signal's EXCESS volume over baseline that must
#: drain before the volume case is considered gone. Half is a
#: description of "the excess has halved", not a tuned level, and it is
#: recorded on every measured row so a later study can recompute with a
#: different definition instead of inheriting this one.
VOLUME_DECAY_FRACTION = 0.5
EXIT_ON_VOLUME_DECAY = True

#: No price stop. See the module docstring: this is a recorded gap, not
#: a setting. Live activation must not proceed while it is None.
HARD_STOP_PCT = None
REQUIRES_STOP_BEFORE_LIVE = True

#: Why the stop is absent, carried in-band so a reader of the decision
#: log does not have to find this file.
NO_STOP_REASON = (
    "S2_STOP_NOT_ESTABLISHED: four trading days is not enough to choose a "
    "level, and borrowing S1's would apply a trend strategy's measurement "
    "to an accumulation strategy")
