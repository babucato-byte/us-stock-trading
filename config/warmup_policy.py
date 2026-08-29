"""How much history a symbol needs before its signals mean anything.

Why a symbol cannot go straight to WATCHING
-------------------------------------------
A Tier2 symbol subscribed at 15:42 has one bar. Every indicator the
strategy consults can still be COMPUTED from it -- an EMA of one value
is that value, a volume "baseline" over one bar is that bar, a session
VWAP over one print is that print. None of them raises, none of them
returns None, and all of them are meaningless.

That is the failure this exists to prevent, and it is worse than a
missing value: an EMA9 that equals the last price makes every symbol
look like it is sitting exactly on its average, and a volume baseline
equal to the current bar makes every symbol look like it has no
expansion at all -- or, if the first bar is quiet, like everything after
it is a breakout.

Only completed bars count
-------------------------
The bar for the minute in progress is still accumulating. Counting it
toward "enough history" means the requirement is satisfied a minute
early, by a bar whose volume is a fraction of its final value.

The numbers
-----------
EMA21 needs the most and sets the floor. An EMA's weight decays
geometrically, so it never fully forgets its seed; the usual working
rule is ~5 spans before the seed's influence is negligible, which is
105 bars for a 21-span. That is a judgement, not a law, and it is
written here as one number to argue with rather than scattered through
the code.
"""

#: Bars are one minute.
BAR_MINUTES = 1

#: Completed bars each feature needs before it may be trusted.
#:
#: EMA21 dominates: ~5 spans before the seed stops mattering. EMA9 gets
#: the same treatment at its own span. The volume baseline is a 20-bar
#: average and needs its 20.
REQUIRED_BARS = {
    "ema9": 45,
    "ema21": 105,
    "volume_baseline": 20,
}

#: The opening range is measured from the session's own anchor, not from
#: whenever we happened to start listening. A symbol subscribed after
#: the range closed has no ORB and cannot acquire one later.
ORB_MINUTES = 15

#: The states a candidate moves through before it can be considered.
STATE_SCANNED = "SCANNED"
STATE_WARMING_UP = "WARMING_UP"
STATE_WATCHING = "WATCHING"
STATE_WARMUP_FAILED = "WARMUP_FAILED"

#: Reasons a warmup ends without reaching WATCHING. Each names what was
#: wrong, because "not ready" alone cannot be acted on.
INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
GAP_IN_HISTORY = "GAP_IN_HISTORY"
DUPLICATE_TIMESTAMPS = "DUPLICATE_TIMESTAMPS"
NON_MONOTONIC = "NON_MONOTONIC"
OHLC_INCONSISTENT = "OHLC_INCONSISTENT"
STALE_LAST_BAR = "STALE_LAST_BAR"
ANCHOR_NOT_COVERED = "ANCHOR_NOT_COVERED"
ORB_WINDOW_MISSED = "ORB_WINDOW_MISSED"
VWAP_UNAVAILABLE = "VWAP_UNAVAILABLE"

#: The most of a warmup window that may be missing and still be usable.
#: Above this the history has holes wide enough that an average over it
#: describes a different symbol than the one trading.
MAX_MISSING_RATIO = 0.10

#: How old the newest completed bar may be before the history is treated
#: as stale rather than merely short.
MAX_LAST_BAR_AGE_SECONDS = 180.0


def required_bars(feature) -> int:
    return REQUIRED_BARS.get(feature, 0)


def longest_requirement() -> int:
    """The binding constraint -- what a full warmup actually costs."""
    return max(REQUIRED_BARS.values())
