"""Manual Watchlist policy, versioned and separate from scanner config.

Nothing here can change a scanner. `manual_watch_score` is computed
AFTER the scanners have already decided PASS/REJECT, from the fields
they stored -- it reorders a list, it never adds or removes a signal.
Month 1's rule that no scanner threshold moves is untouched by anything
in this file, and `tests/test_manual_watchlist.py` asserts that the
scanner configs are not read here.

Every weight is named and version-stamped so a reordering that happened
last month is reproducible this month. When the weights change,
`MANUAL_WATCH_VERSION` changes with them -- a watchlist file records the
version it was built under, so two files built a week apart are never
silently compared under the assumption they used the same formula.
"""

import os

#: Bump whenever any weight, penalty or the score formula changes.
#: Stamped into every watchlist file.
MANUAL_WATCH_VERSION = "manual_watch_v1"

# --- which scanners feed which stage (spec track C-2) -------------------
#
# S1/S2/S3 are the daily, end-of-session scanners: they are what the
# evening pass has to work with. S4 runs premarket and is therefore a
# CONFIRMATION of last night's list rather than a source of new names --
# a symbol S4 flags that nothing flagged yesterday has no overnight
# thesis behind it, only this morning's gap.
#
# S5/S6 are intraday. They are recorded for behaviour observation and
# performance analysis, and deliberately contribute NOTHING to the
# ranking: by the time they fire, the reading list has already been
# read.
DAILY_SOURCE_SCANNERS = ("hma_early_trend", "accumulation", "breakout_ready")
PREMARKET_CONFIRM_SCANNER = "premarket_momentum"
INTRADAY_OBSERVE_SCANNERS = ("gap_pullback", "orb")

# --- score weights ------------------------------------------------------
#
# The formula is a weighted sum of components each normalised to 0-100,
# minus an overextension penalty. It is deliberately boring: anything
# cleverer would be a Candidate Decision Layer wearing a different name,
# and this layer is explicitly not allowed to be one.
WEIGHT_INTERSECTION = 30.0   # how many DAILY scanners agreed
WEIGHT_MAX_SCORE = 25.0      # the best scanner_score the symbol earned
WEIGHT_EARLY_TREND = 15.0    # S1 present
WEIGHT_ACCUMULATION = 10.0   # S2 present
WEIGHT_BREAKOUT_READY = 10.0  # S3 present
WEIGHT_PREMARKET_CONFIRM = 10.0  # S4 confirmed it this morning

#: Subtracted from the weighted sum. Not a filter: an overextended name
#: still appears, ranked lower and flagged, because "it ran already" is
#: something the reader should see rather than something this code
#: should decide for them.
PENALTY_OVEREXTENDED = 20.0

# --- overextension thresholds (spec track C-5) --------------------------
#
# These mark a name as "already moved", for DISPLAY. They are not
# scanner thresholds and are never consulted by a scanner: a symbol that
# trips every one of them is still a PASS if its scanner said PASS.
#
# The HMA200 figure mirrors the extension ceiling documented for the
# (disabled) candidate decision policy, so the two do not drift apart.
# It is restated here rather than imported, because importing that
# module from this package is exactly what the isolation test forbids.
OVEREXTENDED_HMA200_PCT = 25.0
OVEREXTENDED_HMA89_PCT = 15.0
OVEREXTENDED_DAY_CHANGE_PCT = 10.0
#: Distance to the 52-week high, in percent. A name AT its 52w high is
#: not overextended by itself -- that is what a breakout looks like --
#: so this only fires together with one of the extension measures.
NEAR_52W_HIGH_PCT = 1.0

# --- output sizes (spec track C-4) --------------------------------------
FILE_TOP_N = 20      # the file may carry more context than the message
SLACK_TOP_N = 5      # default in Slack
SLACK_TOP_N_MAX = 10  # hard ceiling for --top

#: How many scored symbols the STORED file keeps, before display
#: truncation. Larger than FILE_TOP_N because the morning pass re-ranks
#: the evening's list: a symbol that sat at 24th last night can move up
#: once the premarket scanner confirms it, and it can only do that if it
#: was written down. Bounded so a broad day cannot produce a file with
#: thousands of entries -- what is dropped is recorded as
#: `truncated_from` rather than silently lost.
STORE_TOP_N = 200

# --- storage ------------------------------------------------------------
#: A single location, chosen from the two the spec allowed. `logs/watchlist`
#: sits BESIDE `logs/scanners` rather than inside it, so that a rule like
#: "the analytics store is append-only research data" keeps meaning
#: exactly what it says -- the watchlist is neither append-only nor
#: research data, and burying it under the same root would blur that.
WATCHLIST_DIR_ENV = "MANUAL_WATCHLIST_DIR"
WATCHLIST_SUBDIR = ("logs", "watchlist")

STAGE_TOMORROW = "tomorrow"
STAGE_TODAY = "today"


def slack_top_n(requested=None) -> int:
    """The requested Slack size, clamped to the documented ceiling."""
    if requested is None:
        return SLACK_TOP_N
    try:
        value = int(requested)
    except (TypeError, ValueError):
        return SLACK_TOP_N
    return max(1, min(value, SLACK_TOP_N_MAX))


def is_enabled(env=None) -> bool:
    """Same off switch as the scanner notifications, read the same way."""
    mapping = os.environ if env is None else env
    raw = mapping.get("SCANNER_SLACK_ENABLED")
    if raw is None:
        return True
    return str(raw).strip().lower() not in {"0", "false", "no", "n", "off"}
