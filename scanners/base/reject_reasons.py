"""Turn a rejection sentence into a stable code, an observed value and
a threshold.

Why classify instead of tagging at the source
---------------------------------------------
`require()` takes a message, not a code, and that is deliberate: the
condition and its explanation sit on one line so they cannot drift
apart. Adding a code argument to every `require()` call in every scanner
would put a second thing next to each condition that CAN drift, and the
first month a code said VWAP while the sentence said EMA, the
calibration built on it would be wrong in a way nobody could see.

So the sentence stays the source of truth and is classified here. The
patterns are anchored on the wording the scanners actually emit, and
`UNCLASSIFIED` is a real answer -- a reason this table does not
recognise is reported as unrecognised rather than silently bucketed into
the nearest match, because a wrong bucket is worse than a missing one
when the whole point is counting.

What is stored, and what is not
-------------------------------
One row per rejected symbol: timestamp, session, symbol, scanner, code,
and -- where the sentence carried them -- the observed value and the
threshold it missed. Not the bar frame, not the indicator series, not
the reason text of every gate that would have run afterwards. A 202
symbol scan every 15 minutes writes 202 short rows, which is a file an
operator can read and a month of which is still small; dumping the
intermediate calculations would be several megabytes an hour and would
answer no question the code and the two numbers do not.
"""

import re
from typing import Optional, Tuple

#: Codes. Named for the GATE, not for the sentence, so a reworded
#: message keeps its bucket.
CLOSE_BREAKOUT = "CLOSE_BREAKOUT"
CURRENT_ABOVE_RANGE = "CURRENT_ABOVE_RANGE"
EXTENSION = "EXTENSION"
VWAP = "VWAP"
EMA_STRUCTURE = "EMA_STRUCTURE"
VOLUME_EXPANSION = "VOLUME_EXPANSION"
POST_RANGE_BARS = "POST_RANGE_BARS"
OPENING_RANGE = "OPENING_RANGE"
DATA_ERROR = "DATA_ERROR"
SECURITY_TYPE = "SECURITY_TYPE"
PRICE_INVALID = "PRICE_INVALID"
UNCLASSIFIED = "UNCLASSIFIED"

_NUM = r"(-?\d+(?:\.\d+)?)"

#: (code, pattern, observed group, threshold group). Order matters: the
#: first match wins, so the more specific sentence is listed first.
_PATTERNS = (
    # "broke the opening range high 25.90 but has fallen back inside the
    # range (now 24.68)" -- a DIFFERENT finding from never having closed
    # above it, and the two must not share a bucket: one is a failed
    # breakout, the other is a faded one.
    (CURRENT_ABOVE_RANGE,
     rf"fallen back inside the range \(now {_NUM}\)", 1, None),
    (CURRENT_ABOVE_RANGE, rf"broke the opening range high {_NUM} but", None, 1),
    (CLOSE_BREAKOUT, rf"no bar has CLOSED above the opening range high {_NUM}",
     None, 1),
    (CLOSE_BREAKOUT, rf"opening range high {_NUM} has not been touched",
     None, 1),
    (VOLUME_EXPANSION, rf"volume expansion {_NUM}x below {_NUM}x", 1, 2),
    (VOLUME_EXPANSION, r"volume expansion not computable", None, None),
    (EXTENSION, rf"already {_NUM}% above the opening range high, past the {_NUM}%",
     1, 2),
    (VWAP, rf"price {_NUM} at/below VWAP {_NUM}", 1, 2),
    (VWAP, r"session VWAP not computable", None, None),
    (EMA_STRUCTURE, rf"EMA9 {_NUM} at/below EMA21 {_NUM}", 1, 2),
    (EMA_STRUCTURE, r"session EMA9/EMA21 not computable", None, None),
    (POST_RANGE_BARS, rf"{_NUM} bars since the \d+m opening range, need {_NUM}",
     1, 2),
    (OPENING_RANGE, r"opening range \(\d+m\) not computable", None, None),
    (OPENING_RANGE, r"opening range \(\d+m\)? ?not computable", None, None),
    (OPENING_RANGE, r"no regular-session bars today", None, None),
    (OPENING_RANGE, r"no .* bars for this session", None, None),
    (DATA_ERROR, r"insufficient_or_stale_data", None, None),
    (DATA_ERROR, r"no usable current price|session bars have no usable closes",
     None, None),
    (SECURITY_TYPE, r"security type|not a common stock|ETP|WARRANT", None, None),
    (PRICE_INVALID, r"price .* invalid|no usable .* price", None, None),
)

_COMPILED = tuple((code, re.compile(pattern, re.IGNORECASE), obs, thr)
                  for code, pattern, obs, thr in _PATTERNS)


def classify(message) -> Tuple[str, Optional[float], Optional[float]]:
    """`(code, observed, threshold)`. Never raises, never guesses.

    An unrecognised sentence yields `UNCLASSIFIED` with no numbers,
    which shows up in the summary as its own bucket -- the signal that
    this table needs a new pattern, rather than a quietly wrong count.
    """
    text = "" if message is None else str(message)
    for code, pattern, obs_group, thr_group in _COMPILED:
        found = pattern.search(text)
        if not found:
            continue
        return code, _group(found, obs_group), _group(found, thr_group)
    return UNCLASSIFIED, None, None


def _group(match, index) -> Optional[float]:
    if index is None:
        return None
    try:
        return float(match.group(index))
    except (IndexError, TypeError, ValueError):
        return None


# -- DATA_ERROR diagnostics -------------------------------------------
#
# `DATA_ERROR` is one bucket in the scan summary, and on 2026-09-02 it was
# 592 of 593 symbols for six consecutive overnight scans with `rejected=0`
# -- the strategy was never consulted at all. The aggregate could not say
# whether that was thin overnight liquidity doing exactly what it always
# does, or a fetch path that had stopped working, and those two call for
# opposite responses.
#
# So each DATA_ERROR is also filed under one diagnostic category. This is
# OBSERVATIONAL ONLY: nothing branches on it, no symbol's fate changes,
# and the existing `DATA_ERROR` count is untouched.

INSUFFICIENT_POST_RANGE_BARS = "INSUFFICIENT_POST_RANGE_BARS"
MISSING_VWAP = "MISSING_VWAP"
MISSING_EMA = "MISSING_EMA"
STALE_DATA = "STALE_DATA"
NO_EXECUTABLE_PRICE = "NO_EXECUTABLE_PRICE"
KIS_FETCH_ERROR = "KIS_FETCH_ERROR"
RATE_LIMIT = "RATE_LIMIT"
OTHER = "OTHER"

DATA_ERROR_CATEGORIES = (
    INSUFFICIENT_POST_RANGE_BARS, MISSING_VWAP, MISSING_EMA, STALE_DATA,
    NO_EXECUTABLE_PRICE, KIS_FETCH_ERROR, RATE_LIMIT, OTHER,
)

#: Categories where the data never arrived, as opposed to arriving and
#: being insufficient. The distinction is the whole point: one is an
#: infrastructure fault, the other is a quiet market.
ACQUISITION_FAILURES = frozenset({KIS_FETCH_ERROR, RATE_LIMIT})

#: Most specific first; first match wins, so exactly one category is
#: assigned per symbol and the counts sum to the DATA_ERROR total.
_DATA_ERROR_PATTERNS = (
    # Acquisition. Checked before the "insufficient" families because a
    # fetch that failed produces no bars, and "no bars" would otherwise
    # read as thin liquidity.
    (RATE_LIMIT, r"rate.?limit|EGW00201|too many requests|throttl"),
    (KIS_FETCH_ERROR,
     r"KIS|minute chart unavailable|fetch failed|no intraday bars|"
     r"MarketDataUnavailable|returned no minute bars|HTTP \d{3}|timeout"),
    # Arrived, but not enough to compute the strategy's inputs.
    (INSUFFICIENT_POST_RANGE_BARS,
     r"bars since the \d+m opening range|post.?range|opening range "
     r"\(\d+m\)? ?not computable|no regular-session bars|"
     r"no .* bars for this session"),
    (MISSING_VWAP, r"VWAP not computable|no VWAP|vwap"),
    (MISSING_EMA, r"EMA9?/?EMA21 not computable|EMA not computable|ema"),
    (STALE_DATA, r"stale|insufficient_or_stale_data|too old|age"),
    (NO_EXECUTABLE_PRICE,
     r"no usable current price|no usable closes|price .* invalid|"
     r"no executable price|no usable .* price"),
)

_DATA_ERROR_COMPILED = tuple(
    (code, re.compile(pattern, re.IGNORECASE))
    for code, pattern in _DATA_ERROR_PATTERNS)


def classify_data_error(message) -> str:
    """One diagnostic category for a `ScannerDataError`. Never raises.

    An unrecognised message yields OTHER rather than a guess, and a
    growing OTHER is the signal that this table needs a new pattern --
    the same discipline `classify` uses for UNCLASSIFIED.
    """
    text = "" if message is None else str(message)
    for code, pattern in _DATA_ERROR_COMPILED:
        if pattern.search(text):
            return code
    return OTHER


def is_acquisition_failure(category) -> bool:
    """True when the data never arrived, false when it arrived thin."""
    return category in ACQUISITION_FAILURES
