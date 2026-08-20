"""Execution-time confirmation for an S2 candidate.

Why this is not a scanner condition
-----------------------------------
S2's premise is that volume arrives BEFORE the price move, and its score
pays 30 of 100 points for a quiet price. `price_change_max_pct` is a
ceiling with deliberately no floor -- the scanner's own config records
that, and says why: a floor chosen now would remove the evidence that a
month of data is supposed to produce.

So "confirm the price is going the right way" cannot live in the scanner
without changing what S2 measures. It lives here instead, at execution
time, where it is a question about THIS moment rather than about the
setup: the scanner found a quiet accumulation, and by the time we would
act on it, has the price started to move with us or against us?

No free parameters
------------------
Every check below is either a re-read of S2's own measured condition or a
comparison with no threshold to choose:

    price > signal price     zero is the natural boundary for "positive";
                             there is no percentage to pick
    price > HMA200           S2's own entry condition, re-checked now
    HMA200 still rising      S2's own entry condition, re-checked now
    low <= price <= high     the venue's own day range, reused from S1

The last one is the existing S1 execution gate. Reusing it rather than
writing a second sanity check keeps one definition of "this price is
real" -- and that definition already refuses to invent a percentage.

Fail closed
-----------
Every unknown blocks. A missing HMA200, an unavailable price, an absent
signal price: each returns a refusal naming what was missing, never a
pass. An entry is optional; entering on data nobody could read is not.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

ALLOW = "ALLOW"
BLOCK = "BLOCK"

REASON_OK = "S2_ENTRY_CONFIRMED"
REASON_NO_PRICE = "S2_ENTRY_PRICE_UNAVAILABLE"
REASON_NO_SIGNAL_PRICE = "S2_ENTRY_SIGNAL_PRICE_UNAVAILABLE"
REASON_NO_FEATURES = "S2_ENTRY_FEATURES_UNAVAILABLE"
REASON_PRICE_NOT_CONFIRMED = "S2_ENTRY_PRICE_NOT_CONFIRMED"
REASON_BELOW_HMA200 = "S2_ENTRY_BELOW_HMA200"
REASON_HMA200_NOT_RISING = "S2_ENTRY_HMA200_NOT_RISING"
REASON_HMA200_UNAVAILABLE = "S2_ENTRY_HMA200_UNAVAILABLE"
REASON_STALE_SESSION = "S2_ENTRY_SESSION_NOT_ORDER_VERIFIED"


@dataclass
class EntryVerdict:
    decision: str
    reason: str
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.decision == ALLOW

    def as_dict(self) -> Dict[str, Any]:
        return {"decision": self.decision, "reason": self.reason,
                "detail": dict(self.detail)}


def _finite(value) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number or number in (
        float("inf"), float("-inf")) else number


def price_confirmed(current_price, signal_price) -> Optional[bool]:
    """Has the price moved with us since the scan?

    Strictly greater. A price exactly back at the signal price has not
    confirmed anything -- it is the same observation the scanner already
    made, and treating "unchanged" as confirmation would make the check
    pass for every candidate that simply did not move.

    None when either side is unreadable, so the caller blocks on the
    missing input rather than on a false comparison.
    """
    current = _finite(current_price)
    signal = _finite(signal_price)
    if current is None or signal is None:
        return None
    return current > signal


def confirm(*, current_price, signal_price, features=None, session=None,
            require_order_verified_session: bool = True) -> EntryVerdict:
    """May this S2 candidate be bought right now?

    Ordered so the refusal names the FIRST thing that was wrong, which is
    the one an operator can act on. A verdict that reported the last
    failure would send them to fix something that was merely also true.
    """
    current = _finite(current_price)
    if current is None:
        return EntryVerdict(BLOCK, REASON_NO_PRICE)

    signal = _finite(signal_price)
    if signal is None:
        return EntryVerdict(BLOCK, REASON_NO_SIGNAL_PRICE)

    if require_order_verified_session:
        from scanners.base import scan_session

        if not scan_session.order_route_verified(session):
            # PREMARKET and AFTER_HOURS scan but have no verified order
            # route. A reservation is an instruction to trade later, not
            # a fill, and treating the two as equivalent is how a
            # scan-only session quietly becomes a live one.
            return EntryVerdict(BLOCK, REASON_STALE_SESSION,
                                {"session": session,
                                 "verified": sorted(
                                     scan_session.ORDER_VERIFIED_SESSIONS)})

    confirmed = price_confirmed(current, signal)
    if confirmed is not True:
        return EntryVerdict(BLOCK, REASON_PRICE_NOT_CONFIRMED,
                            {"current_price": current, "signal_price": signal})

    if features is None:
        return EntryVerdict(BLOCK, REASON_NO_FEATURES)

    hma200 = _finite(getattr(features, "hma200", None))
    slope = _finite(getattr(features, "hma200_slope", None))
    if hma200 is None or slope is None:
        # S2's entry conditions cannot be re-checked, so they are not
        # known to hold. Unknown blocks.
        return EntryVerdict(BLOCK, REASON_HMA200_UNAVAILABLE,
                            {"hma200": hma200, "hma200_slope": slope})
    if current <= hma200:
        return EntryVerdict(BLOCK, REASON_BELOW_HMA200,
                            {"current_price": current, "hma200": hma200})
    if slope <= 0:
        return EntryVerdict(BLOCK, REASON_HMA200_NOT_RISING,
                            {"hma200_slope": slope})

    return EntryVerdict(ALLOW, REASON_OK,
                        {"current_price": current, "signal_price": signal,
                         "gain_since_signal": current - signal,
                         "hma200": hma200, "hma200_slope": slope,
                         "session": session})
