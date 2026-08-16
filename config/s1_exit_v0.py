"""S1_EXIT_V0 -- the exit policy for the FIRST S1 live trades.

Not optimal, and not claimed to be
----------------------------------
Month 1 has produced no signals yet, so no number here is derived from S1
outcome data. `scanners/analytics/exit_research.py` exists to produce
that data, and V1 will be built from it. What V0 is for is narrower: to
make the first real trades survivable and legible, so that the research
has something to compare against.

Every value below is therefore derived from something that ALREADY
exists in this repository -- the account's own risk limits, the
allocator's concentration cap, or S1's own entry conditions -- rather
than chosen because it looked reasonable. Where a value is a convention
rather than a derivation, it says so.

HARD_STOP_PCT = -6%, and why not the -8% already in the codebase
----------------------------------------------------------------
`risk_config.STOP_LOSS_RATE` is -8% and is wired to the scalping path.
Copying it would be the mistake this phase was told to avoid, because it
was chosen for a holding period of minutes to one session, where a stop
is either hit inside the day or not at all. S1 holds across sessions, so
the same percentage permits a materially larger loss before it triggers.

-6% comes from the account limit instead. `s1_allocation.MAX_SINGLE_POSITION_PCT`
caps one name at 35% of the pool, and `risk_config.MAX_DAILY_LOSS_RATE`
caps the account's day at -2%:

    0.35 x 6% = 2.1% of the account

So one full stop-out costs approximately one full daily loss budget.
That is the property worth having: a single position being wrong ends the
day, and cannot quietly consume two or three days of budget. At -8% the
same stop would cost 2.8% -- 40% more than the budget it is supposed to
respect.

PROFIT_PROTECTION -- expressed in R, not in invented percentages
----------------------------------------------------------------
R is the entry-to-stop distance (6%). The ratchet is the classic one and
the R-multiple framing is already this codebase's convention
(`scalping_strategy_v1_config` uses TARGET_1_R_MULTIPLE / TARGET_2_R_MULTIPLE):

    +1R (+6%)  -> protective floor moves to breakeven
    +2R (+12%) -> protective floor moves to +1R (+6%)

The floor only ever rises. A trade that reached +6% cannot subsequently
become a full -6% loss, which is the specific failure this axis exists to
prevent. The TRIGGER LEVELS are a convention, not a measurement, and are
the first thing V1 should revisit.

TREND_EXIT -- S1's own thesis, asymmetrically
----------------------------------------------
S1 buys when: price > HMA200, HMA200 rising, HMA89 > HMA200, ADX > 20,
ADX rising. Exit does NOT require all five to keep holding, because
"re-pass the scanner every day or sell" would sell every ordinary
pullback, and the spec forbids exactly that.

The split is between STRUCTURAL and MOMENTUM conditions:

  structural -- price above HMA200, HMA89 above HMA200, HMA200 rising.
                These ARE the thesis. If price closes back below the long
                average, or the fast average crosses back under it, or the
                long trend itself rolls over, the reason for owning the
                position is gone.

  momentum   -- ADX above 20 and rising. These oscillate during a healthy
                consolidation. Requiring them to persist would exit good
                trends on quiet weeks, so they are deliberately NOT exit
                conditions.

No new indicator is introduced: the exit reads the same `hma200`,
`hma89`, `hma200_slope` and `price` that `SymbolFeatures` already
computes for the scanner.

TIME_EXIT -- capital release, not a time stop
---------------------------------------------
The scalping 60-MINUTE stop is not used and would be meaningless here: a
daily-bar trend signal has not had time to be right or wrong in an hour.

10 sessions is the longest window `exit_research` studies, and roughly
two calendar weeks. The condition is not "10 days, then sell" -- it is
"10 days AND the trade never worked": a position that has reached the
profit-protection trigger is left to the trend and protection axes, and
only one that has never got there has its capital released. That keeps a
working trend running while stopping a dead position from holding the
seed hostage, which is the point of a rotating small account.
"""

VERSION = "s1_exit_v0"

#: Not derived from S1 outcome data -- see the module docstring. Stated
#: as a flag so a report can say so without re-reading the prose.
DATA_DERIVED = False

# --- axis 1: hard stop ----------------------------------------------------
HARD_STOP_PCT = -0.06

#: The R unit every other level is expressed in.
R_PCT = abs(HARD_STOP_PCT)

# --- axis 2: profit protection -------------------------------------------
#: (trigger in R, protective floor in R). The floor only ever rises.
PROFIT_PROTECTION_STEPS = ((1.0, 0.0), (2.0, 1.0))

# --- axis 3: trend exit ---------------------------------------------------
#: Structural conditions whose failure ends the thesis. ADX is absent on
#: purpose -- see the docstring.
TREND_REQUIRE_PRICE_ABOVE_HMA200 = True
TREND_REQUIRE_HMA89_ABOVE_HMA200 = True
TREND_REQUIRE_HMA200_RISING = True

#: A single day's close is enough for price/HMA cross conditions because
#: the scanner itself judges on closes. The slope condition uses the same
#: `hma200_slope` the scanner uses, so "rolled over" means what it means
#: at entry.
TREND_SLOPE_EXIT_BELOW_PCT = 0.0

# --- axis 4: time exit ----------------------------------------------------
TIME_EXIT_SESSIONS = 10

#: A position that ever reached this many R is exempt from the time exit
#: and is managed by the trend and protection axes instead.
TIME_EXIT_EXEMPT_ABOVE_R = 1.0


def as_dict() -> dict:
    return {
        "version": VERSION,
        "data_derived": DATA_DERIVED,
        "hard_stop_pct": HARD_STOP_PCT,
        "r_pct": R_PCT,
        "profit_protection_steps_r": [list(step) for step in PROFIT_PROTECTION_STEPS],
        "trend_structural_conditions": [
            "price_above_hma200", "hma89_above_hma200", "hma200_rising"],
        "trend_momentum_conditions_deliberately_excluded": ["adx_min", "adx_rising"],
        "time_exit_sessions": TIME_EXIT_SESSIONS,
        "time_exit_exempt_above_r": TIME_EXIT_EXEMPT_ABOVE_R,
    }
