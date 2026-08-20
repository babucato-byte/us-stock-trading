"""Which trading session a scan belongs to.

Why a scan needs a session at all
---------------------------------
A scanner answers "what qualifies right now". Run it four times a day and
you get four different answers from the same conditions, because the bars
underneath moved -- and until now every one of those answers was recorded
under the same `trading_day` key with no way to tell them apart. A
candidate found before the open and a candidate found at 15:45 are not the
same observation, and a study that merges them is measuring an average of
two different things.

So the session is a LABEL on a run, not a new condition. Nothing here
changes what any scanner looks for. S2's thresholds live in
`scanners/accumulation/config.json` and are untouched.

The four buckets, and why these boundaries
------------------------------------------
The clock boundaries are the ones `market_hours` already defines --
04:00, 09:30, 16:00, 20:00 ET. No new constant is introduced, because a
new number here would be a trading decision wearing a scheduling costume:

    PREMARKET          04:00 -> 09:30 ET
    REGULAR            09:30 -> 16:00 ET
    AFTER_HOURS        16:00 -> 20:00 ET
    OVERNIGHT_DAYTIME  20:00 -> 04:00 ET   (the remainder)

The four partition the day with no gap and no overlap, which is the
property that matters: every scan lands in exactly one bucket, so a
per-session comparison is a partition of the runs rather than a sample of
them.

OVERNIGHT_DAYTIME is one bucket, not two, because that is how the venue
treats it: KIS's 미국주간거래 window is the Korean daytime that overlaps the
US overnight. Splitting it here would invent a distinction the order path
does not make.

Scanning is not permission to trade
-----------------------------------
`ORDER_VERIFIED_SESSIONS` records which sessions have had their live
order route actually verified against the broker. PREMARKET and
AFTER_HOURS are scannable and are NOT verified: a reserved order is an
instruction to trade later, not an execution, and treating the two as the
same is how a scan-only session quietly becomes a live one. Nothing in
this module grants execution -- it only refuses to imply it.
"""

from datetime import datetime, time
from typing import Optional

from market_hours import (
    EASTERN,
    MARKET_AFTERMARKET_END,
    MARKET_PREMARKET_START,
    MARKET_REGULAR_END,
    MARKET_REGULAR_START,
)

PREMARKET = "PREMARKET"
REGULAR = "REGULAR"
AFTER_HOURS = "AFTER_HOURS"
OVERNIGHT_DAYTIME = "OVERNIGHT_DAYTIME"

#: Clock order, starting at the premarket boundary.
SESSIONS = (PREMARKET, REGULAR, AFTER_HOURS, OVERNIGHT_DAYTIME)

#: Sessions whose live order route has been verified against the broker.
#: See the module docstring: this is a record of what was checked, not a
#: policy that can be widened by editing a tuple. Widening it requires the
#: verification, and the verification is not in this file.
ORDER_VERIFIED_SESSIONS = frozenset({REGULAR, OVERNIGHT_DAYTIME})

#: What a session is allowed to imply, printed rather than inferred.
STATUS_ORDER_VERIFIED = "REFERENCE_VERIFIED"
STATUS_SCAN_ONLY = "SCAN_ONLY / LIVE UNVERIFIED"


def is_session(value) -> bool:
    return str(value) in SESSIONS


def normalize(value) -> Optional[str]:
    """A caller-supplied session name, or None if it is not one of the four.

    Deliberately not a best-effort match. A typo that silently became
    REGULAR would file a premarket scan under the one session that is
    allowed to trade.
    """
    if value is None:
        return None
    text = str(value).strip().upper().replace("-", "_").replace(" ", "_")
    return text if text in SESSIONS else None


def session_at(moment: Optional[datetime] = None) -> str:
    """The session containing `moment` (default: now), in Eastern time.

    Calendar-independent on purpose. A holiday still has a premarket
    window on the clock, and a scan that ran then ran in it; whether the
    market was OPEN is `market_hours.get_market_state()`'s question and it
    is asked separately. Conflating the two would relabel every holiday
    scan as OVERNIGHT_DAYTIME and quietly move it into the one off-hours
    bucket that is order-verified.
    """
    if moment is None:
        moment = datetime.now(EASTERN)
    elif moment.tzinfo is None:
        moment = moment.replace(tzinfo=EASTERN)
    clock: time = moment.astimezone(EASTERN).time()

    if MARKET_PREMARKET_START <= clock < MARKET_REGULAR_START:
        return PREMARKET
    if MARKET_REGULAR_START <= clock < MARKET_REGULAR_END:
        return REGULAR
    if MARKET_REGULAR_END <= clock < MARKET_AFTERMARKET_END:
        return AFTER_HOURS
    return OVERNIGHT_DAYTIME


def order_route_verified(session) -> bool:
    """Whether live orders in this session have a verified route.

    Fails closed: an unrecognised session is not verified.
    """
    return normalize(session) in ORDER_VERIFIED_SESSIONS


def execution_status(session) -> str:
    """The line a monitor prints so nobody has to infer the answer."""
    return (STATUS_ORDER_VERIFIED if order_route_verified(session)
            else STATUS_SCAN_ONLY)
