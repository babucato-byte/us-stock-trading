"""S6_EXIT_V0 policy -- and why it needs no invented percentage.

The stop is the setup's own geometry
------------------------------------
S1 uses −6% and S2 −3%, both chosen for those strategies. Copying either
here would apply a level measured on a different thesis, and picking a
third number from one observation (IEFA, MAE −0.155%) would encode a
single candidate into the risk policy.

Neither is necessary. S6's thesis IS the range: price broke out of it,
and price back BELOW THE RANGE LOW has comprehensively falsified that --
not "fallen a chosen percentage", but returned past the entire structure
the trade was taken from. That level is computed from the candidate's
own bars, has no free parameter, and differs per position exactly as the
setups differ.

So the hard risk level is `range_low`, and there is no percentage to
tune. What remains is a REFUSAL rather than a fallback: a position whose
range is unknown has no expressible stop, and `s6_live.qualification`
already refuses to qualify a candidate without one, so such a position
cannot be opened in the first place. If one is ever encountered -- a
corrupted row, a hand-inserted record -- the policy exits it rather than
holding something it cannot protect.

`MAX_LOSS_IS_MEASURED` is False and stays False: the range is a
structural level, not a measured risk tolerance. What a month of data
should decide is whether range_low is too FAR (giving back more than the
strategy earns) or too near (stopping out of noise), and that comparison
needs the shadow MAE distribution which does not exist yet.

No overnight carry
------------------
S6-R is validated inside one session. A breakout range formed at 09:30
says nothing about the next morning's open, and an intraday stop cannot
protect a gap. The position closes before the session boundary.
"""

#: The structural invalidation level, per position: the low of the range
#: the position broke out of. Not a percentage, not shared between
#: positions, and not tunable -- it is measured from the candidate's own
#: bars.
HARD_RISK_LEVEL = "RANGE_LOW"

#: There is no percentage cap. See the module docstring: the structural
#: level always exists for a qualified S6 candidate, and a position
#: without one is exited rather than held under a borrowed number.
CATASTROPHIC_CAP_PCT = None
MAX_LOSS_IS_MEASURED = False
MAX_LOSS_BASIS = (
    "S6_STRUCTURAL_STOP: the low of the breakout range, computed per "
    "position from its own bars. No percentage is chosen, so none is "
    "borrowed from S1 (-6%) or S2 (-3%) and none is inferred from the "
    "single IEFA observation")

#: What a month of data should settle -- recorded so the review has an
#: agenda rather than an intention.
REEVALUATION_QUESTIONS = (
    "is range_low too far, giving back more than the strategy earns",
    "is range_low too near, stopping out of ordinary noise",
    "does a cap tighter than range_low improve the MFE/MAE ratio",
    "is half the gain above the range the right give-back, and thirty "
    "minutes the right staleness, for the compound decay exit",
)

# --- structural exits -----------------------------------------------------

#: Price back inside the range it broke out of. THE S6 exit: the thesis
#: was the breakout, and this is the breakout undone.
EXIT_ON_RANGE_REENTRY = True

#: Below VWAP: the session's volume traded above the current price.
EXIT_ON_VWAP_FAILURE = True

#: EMA9 <= EMA21. The short-term structure that carried the breakout has
#: turned over.
EXIT_ON_EMA_STRUCTURE_FAILURE = True

#: Volume decay is never an exit ON ITS OWN -- a breakout continuing on
#: lighter participation is a normal winning shape, and cutting it would
#: systematically remove the trades that worked. Paired with price
#: weakness it is the compound exit.
EXIT_ON_VOLUME_DECAY_WITH_WEAKNESS = True
VOLUME_DECAY_FRACTION = 0.5

#: What "price weakness" means for that compound exit.
#:
#: It used to mean `price < peak_price`, and because the peak ratchets up
#: on every tick that was true on the first tick a position did not
#: print a new high -- so any winning breakout whose opening burst of
#: volume had halved (most of them) sold on its first ordinary pullback.
#:
#: Weakness now needs the position to have GIVEN BACK a fraction of the
#: gain the breakout made above the range high -- (peak - price) /
#: (peak - range_high) -- and for the peak to be STALE: no new high for
#: PEAK_STALE_MINUTES. A small pullback inside an advance surrenders a
#: small fraction; a dip minutes after a fresh high is a shake, not a
#: stall. The fraction is expressed in the setup's own geometry, like
#: the structural stop and VOLUME_DECAY_FRACTION, so it is not a
#: percentage of price and is not the same distance for every position.
#:
#: PROVISIONAL. Neither number is measured: 0.5 mirrors the decay
#: fraction's form, and thirty minutes is two executor ticks. The exit
#: diagnostics record the give-back fraction, the drawdown and the peak
#: age on every tick so the distribution can be read from real positions
#: before either value is treated as settled. See REEVALUATION_QUESTIONS.
PEAK_GIVEBACK_FRACTION = 0.5
PEAK_STALE_MINUTES = 30
PEAK_GIVEBACK_IS_MEASURED = False

#: Whether the give-back half of the compound exit may SELL.
#:
#: OFF until the two numbers above have been measured. While OFF the
#: rule is evaluated on every tick exactly as designed, and a tick on
#: which it would have sold is recorded -- `peak_exit_candidate` and
#: `would_sell_reason` in the exit diagnostics, with the raw peak,
#: give-back and age figures beside them -- but the decision is HOLD
#: unless a higher-priority structural exit independently sells. The
#: VWAP_BELOW half of the same rule is unaffected: it carries no new
#: threshold. Flipping this to True is the act of adopting the numbers,
#: and should follow the measurement, not precede it.
ENFORCE_PEAK_GIVEBACK_EXIT = False

# --- validation-phase constraints ----------------------------------------

ALLOW_OVERNIGHT_CARRY = False
EXIT_ON_SESSION_END = True

#: Raised one executor tick before the boundary, so a position cannot
#: reach it by being evaluated late. Not a tuned value: it is the width
#: of the tick itself.
SESSION_EXIT_LEAD_MINUTES = 15


def structural_stop(range_low):
    """The stop in force for a position, or None if it has no range."""
    try:
        low = float(range_low)
    except (TypeError, ValueError):
        return None
    return None if low != low or low <= 0 else low
