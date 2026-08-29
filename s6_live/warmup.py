"""Is there enough history behind this symbol to believe its indicators?

The question nothing was asking
------------------------------
A symbol subscribed to the realtime stream at 15:42 has one bar. Every
indicator can still be computed from it: an EMA of one value is that
value, a 20-bar volume baseline over one bar is that bar, a session VWAP
over one print is that print. Nothing raises, nothing returns None, and
every answer is meaningless -- an EMA9 equal to the last price makes a
symbol look like it is sitting exactly on its average, and a volume
baseline equal to the current bar makes expansion either impossible or
guaranteed depending on whether the first bar was quiet.

So a newly-subscribed symbol is WARMING_UP, not WATCHING, and it becomes
WATCHING only when the history behind it is both long enough and sound.

Failing is a real outcome
-------------------------
A warmup that cannot complete returns WARMUP_FAILED with the specific
reason, and the caller releases the slot back to Tier1. It does NOT
become READY with a note attached: a slot held by a symbol whose
indicators cannot be trusted is worse than an empty one, because it
looks occupied.

This module decides nothing about trading. It answers one question --
is the history sufficient and sound -- and every gate downstream still
applies.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from config import warmup_policy as policy

logger = logging.getLogger(__name__)


def completed_bars(bars, *, now):
    """Bars whose minute has finished.

    The bar for the minute in progress is still accumulating. Counting
    it toward "enough history" satisfies the requirement a minute early,
    on a bar whose volume is a fraction of what it will be.
    """
    if not bars:
        return []
    current_minute = now.replace(second=0, microsecond=0)
    return [b for b in bars if b.minute < current_minute]


def _monotonic(bars):
    minutes = [b.minute for b in bars]
    return minutes == sorted(minutes)


def _duplicates(bars):
    minutes = [b.minute for b in bars]
    return len(minutes) != len(set(minutes))


def _ohlc_sound(bar):
    """High is the highest and low the lowest, or the bar is not a bar."""
    try:
        values = (float(bar.open), float(bar.close))
        high, low = float(bar.high), float(bar.low)
    except (TypeError, ValueError):
        return False
    if high < low:
        return False
    return all(low <= v <= high for v in values)


def _missing_ratio(bars):
    """How much of the covered span has no bar.

    Measured against the span the bars themselves cover, not against a
    wall-clock window: a symbol that only traded for ten minutes has ten
    minutes of history, not fifty missing ones.
    """
    if len(bars) < 2:
        return 0.0
    span = (bars[-1].minute - bars[0].minute).total_seconds() / 60.0
    expected = int(span // policy.BAR_MINUTES) + 1
    if expected <= 0:
        return 0.0
    return max(0.0, (expected - len(bars)) / expected)


def integrity(bars, *, now, session_anchor=None) -> Dict[str, Any]:
    """Every structural check, run together, with each failure named.

    All of them run even after one fails. A history with three separate
    problems reported one per attempt takes three cycles to understand.
    """
    problems: List[str] = []
    if not _monotonic(bars):
        problems.append(policy.NON_MONOTONIC)
    if _duplicates(bars):
        problems.append(policy.DUPLICATE_TIMESTAMPS)
    if any(not _ohlc_sound(b) for b in bars):
        problems.append(policy.OHLC_INCONSISTENT)

    missing = _missing_ratio(bars)
    if missing > policy.MAX_MISSING_RATIO:
        problems.append(policy.GAP_IN_HISTORY)

    age = None
    if bars:
        age = (now - bars[-1].minute).total_seconds()
        if age > policy.MAX_LAST_BAR_AGE_SECONDS:
            problems.append(policy.STALE_LAST_BAR)

    # The session's own anchor, not whenever we started listening. A
    # symbol subscribed after the opening range closed has no ORB and
    # cannot acquire one later, so it is not merely short of history --
    # it is missing a feature it can never get today.
    if session_anchor is not None:
        if not bars or bars[0].minute > session_anchor:
            problems.append(policy.ANCHOR_NOT_COVERED)
            orb_end = session_anchor + timedelta(minutes=policy.ORB_MINUTES)
            if bars and bars[0].minute >= orb_end:
                problems.append(policy.ORB_WINDOW_MISSED)

    return {
        "bar_count": len(bars),
        "missing_ratio": missing,
        "last_bar_age_seconds": age,
        "problems": problems,
        "sound": not problems,
    }


def sufficiency(bars) -> Dict[str, Any]:
    """Which features have enough completed bars behind them."""
    have = len(bars)
    per_feature = {name: have >= need
                   for name, need in policy.REQUIRED_BARS.items()}
    needed = policy.longest_requirement()
    return {
        "bars": have,
        "required": needed,
        "short_by": max(0, needed - have),
        "per_feature": per_feature,
        "satisfied": all(per_feature.values()),
    }


def vwap_available(bars, *, session_anchor) -> bool:
    """Is there VWAP coverage from the session anchor onward?

    A VWAP over the most recent N bars is NOT a session VWAP, and
    substituting one would answer a different question than the strategy
    asked -- silently, and with a plausible number.
    """
    if not bars or session_anchor is None:
        return False
    return bars[0].minute <= session_anchor


def evaluate(symbol, *, bars, now, session_anchor=None) -> Dict[str, Any]:
    """WARMING_UP, WATCHING, or WARMUP_FAILED -- with the reason.

    Never raises. A symbol whose warmup cannot be judged is not promoted,
    which is the safe direction: it stays out of the ready set and says
    why.
    """
    moment = now or datetime.now(timezone.utc)
    try:
        usable = completed_bars(bars, now=moment)
        checks = integrity(usable, now=moment, session_anchor=session_anchor)
        counts = sufficiency(usable)
        reasons = list(checks["problems"])

        if session_anchor is not None and not vwap_available(
                usable, session_anchor=session_anchor):
            reasons.append(policy.VWAP_UNAVAILABLE)

        # A structural problem is a FAILURE; merely being short of bars
        # is not. One means the history is wrong and more waiting will
        # not fix it; the other means waiting is exactly the fix.
        structural = [r for r in reasons if r != policy.INSUFFICIENT_HISTORY]
        if structural:
            state = policy.STATE_WARMUP_FAILED
        elif not counts["satisfied"]:
            state = policy.STATE_WARMING_UP
            reasons.append(policy.INSUFFICIENT_HISTORY)
        else:
            state = policy.STATE_WATCHING

        return {
            "symbol": str(symbol or "").upper(),
            "state": state,
            "reasons": reasons,
            "evaluated_at": moment.isoformat(),
            "completed_bars": counts["bars"],
            "required_bars": counts["required"],
            "short_by": counts["short_by"],
            "per_feature": counts["per_feature"],
            "integrity": checks,
            #: True only when the slot should go back to Tier1. Waiting
            #: for bars keeps the slot; a broken history does not.
            "release_slot": state == policy.STATE_WARMUP_FAILED,
        }
    except Exception:  # noqa: BLE001 - an unjudgeable warmup is not a pass
        logger.warning("could not evaluate warmup for %s", symbol,
                       exc_info=True)
        return {
            "symbol": str(symbol or "").upper(),
            "state": policy.STATE_WARMUP_FAILED,
            "reasons": ["warmup could not be evaluated"],
            "release_slot": True,
        }


def may_watch(result) -> bool:
    """Only a completed, sound warmup opens the gate to WATCHING."""
    return bool(result) and result.get("state") == policy.STATE_WATCHING
