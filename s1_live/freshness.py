"""Signal freshness and extension, measured at order time.

Freshness
---------
PHASE 3 already refuses a candidate file whose manifest names a
different trading day. This adds the per-signal check: the signal's own
timestamp must belong to the trading day being traded.

The MAXIMUM AGE within a day is deliberately not set. PHASE 4A §13 ties
that number to the S1 exit horizon, which is itself unresolved -- the
only exit policy currently wired is a scalping one (60-minute time stop,
2R target on an 8% stop) whose holding period has nothing to do with a
daily-bar trend signal. Picking an age limit now would be picking half
of a decision whose other half does not exist. So `max_age_seconds`
defaults to None and, when unset, the age is measured and recorded but
not enforced.

Extension
---------
Same discipline. `extension_pct` -- how far the current price has moved
from the price the signal was generated at -- is COMPUTED and RECORDED,
and nothing is blocked on it.

The tempting shortcut would be to reuse
`candidate_decision.json`'s `max_extension_hma200_pct: 25.0`. That is a
different measurement: extension of price above its 200-period Hull
average, which describes where a stock sits in its own trend. This one
is drift between signal time and order time, which describes how much
the entry has already been given away. A 25% ceiling on the first says
nothing about the second, and borrowing the number because it is the
only one lying around is how an unjustified threshold ends up gating
real money. `candidate_decision` also stays disabled and is not
imported.
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

ALLOW = "ALLOW"
BLOCK = "BLOCK"

REASON_WRONG_TRADING_DAY = "SIGNAL_WRONG_TRADING_DAY"
REASON_NO_TIMESTAMP = "SIGNAL_TIMESTAMP_UNKNOWN"
REASON_FUTURE_SIGNAL = "SIGNAL_IN_THE_FUTURE"
REASON_TOO_OLD = "SIGNAL_TOO_OLD"

#: Recorded on every evaluation so the eventual threshold can be chosen
#: from observed data rather than guessed.
EXTENSION_UNENFORCED = "EXTENSION_RECORDED_NOT_ENFORCED"


@dataclass(frozen=True)
class FreshnessResult:
    verdict: str
    reason_code: Optional[str] = None
    detail: str = ""
    age_seconds: Optional[float] = None
    signal_trading_day: Optional[str] = None
    expected_trading_day: Optional[str] = None

    @property
    def allows_entry(self) -> bool:
        return self.verdict == ALLOW

    def as_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict, "reason_code": self.reason_code,
            "detail": self.detail, "age_seconds": self.age_seconds,
            "signal_trading_day": self.signal_trading_day,
            "expected_trading_day": self.expected_trading_day,
        }


def as_utc(value) -> Optional[datetime]:
    """A UTC-aware datetime from a datetime OR an ISO-8601 string, else None.

    Public and shared: `s1_live/reentry.py` uses the same function rather
    than carrying its own. Candidate rows come off a CSV, so their
    timestamps are strings, while callers holding a live clock pass
    datetimes -- and a guard that understood only one of those shapes
    would silently reject every candidate for "no usable timestamp".
    That is not hypothetical: the re-entry guard did exactly that until
    the two parsers were merged into this one.

    A naive datetime is treated as UTC rather than compared against an
    aware one, which would raise.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


#: Internal alias kept so this module reads as it did.
_aware = as_utc


def check(*, signal_timestamp, signal_trading_day, expected_trading_day,
          now=None, max_age_seconds: Optional[float] = None) -> FreshnessResult:
    """Is this signal still the right one to act on?"""
    current = _aware(now) or datetime.now(timezone.utc)
    signal_day = str(signal_trading_day) if signal_trading_day else None
    expected_day = str(expected_trading_day) if expected_trading_day else None

    if not signal_day or not expected_day or signal_day != expected_day:
        return FreshnessResult(
            BLOCK, REASON_WRONG_TRADING_DAY,
            f"signal trading day {signal_day!r} is not the day being traded "
            f"({expected_day!r})",
            signal_trading_day=signal_day, expected_trading_day=expected_day)

    stamp = _aware(signal_timestamp)
    if stamp is None:
        return FreshnessResult(BLOCK, REASON_NO_TIMESTAMP,
                               "the signal carries no usable timestamp",
                               signal_trading_day=signal_day,
                               expected_trading_day=expected_day)

    age = (current - stamp).total_seconds()
    if age < 0:
        # A signal stamped in the future is a clock or provenance
        # problem, not a very fresh signal.
        return FreshnessResult(BLOCK, REASON_FUTURE_SIGNAL,
                               f"the signal is stamped {abs(age):.0f}s in the future",
                               age_seconds=round(age, 3),
                               signal_trading_day=signal_day,
                               expected_trading_day=expected_day)

    if max_age_seconds is not None and age > float(max_age_seconds):
        return FreshnessResult(BLOCK, REASON_TOO_OLD,
                               f"the signal is {age:.0f}s old, beyond the configured "
                               f"{float(max_age_seconds):.0f}s",
                               age_seconds=round(age, 3),
                               signal_trading_day=signal_day,
                               expected_trading_day=expected_day)

    return FreshnessResult(ALLOW, age_seconds=round(age, 3),
                           signal_trading_day=signal_day,
                           expected_trading_day=expected_day)


def extension_pct(signal_price, current_price) -> Optional[float]:
    """How far price has drifted from the signal, in percent. None if
    either side is unusable -- never 0.0, which would read as "no drift"."""
    for value in (signal_price, current_price):
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(float(value)):
            return None
    signal = float(signal_price)
    if signal <= 0:
        return None
    return round((float(current_price) - signal) / signal * 100.0, 6)


def measure_extension(signal_price, current_price) -> Dict[str, Any]:
    """The recorded-not-enforced extension observation."""
    pct = extension_pct(signal_price, current_price)
    return {
        "signal_price": signal_price,
        "current_price": current_price,
        "extension_pct": pct,
        "extension_policy": EXTENSION_UNENFORCED,
        "extension_threshold_pct": None,
    }
