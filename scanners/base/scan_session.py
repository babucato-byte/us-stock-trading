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
`ORDER_VERIFIED_SESSIONS` records which sessions the specification
defines an order route for -- now all four, since premarket and
aftermarket share the general endpoint with the regular session. Nothing
in this module grants execution; it only records what a route exists for.

The boundaries below are for SCANNING and are fixed in Eastern time on
purpose: every scan must land in exactly one bucket, and buckets that
moved would make per-session comparison a sample rather than a partition.
They are NOT the hours orders may be placed in. KIS publishes its windows
in KST and they shift against Eastern time with US daylight saving --
`config.kis_market_schedule` derives those, and `session_capability`
refuses whenever the two disagree, which under DST they do for the hour
between the aftermarket extension and the daytime open.
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

#: Sessions for which the official specification defines an order route.
#:
#: All four. Premarket, regular and aftermarket share the general family
#: (/trading/order, TTTT1002U/TTTT1006U/TTTT1004U); daytime has its own
#: (/trading/daytime-order, TTTS6036U/TTTS6037U/TTTS6038U). Premarket and
#: aftermarket were previously excluded on the reasoning that no
#: extended-hours TR exists, which read a SHARED route as a missing one.
#:
#: "Route specified" is not "wire values confirmed by a live response",
#: and this set says only the first. The second lives in the broker's
#: verification matrix, per ROUTE rather than per session -- the three
#: general sessions confirm one set together because they address one
#: endpoint. Nor is either of them permission: `s6_sessions.LIVE_SESSIONS`
#: is the rollout, and `config.kis_market_schedule` still refuses any
#: hour outside a window KIS actually runs.
ORDER_VERIFIED_SESSIONS = frozenset(
    {PREMARKET, REGULAR, AFTER_HOURS, OVERNIGHT_DAYTIME})

#: What a session is allowed to imply, printed rather than inferred.
STATUS_ORDER_VERIFIED = "REFERENCE_VERIFIED"
STATUS_SCAN_ONLY = "SCAN_ONLY / LIVE UNVERIFIED"

#: The route is defined and reference-verified, but no live response has
#: confirmed it yet, so a new BUY there fails closed.
#:
#: Distinct from SCAN_ONLY, and the distinction matters to whoever reads
#: the message. SCAN_ONLY says "this session cannot be traded"; a reader
#: takes that as a fact about the market or the data. The daytime session
#: has live data, live volume and valid features -- what it lacks is one
#: confirmed BUY response, which is a fact about our evidence and nobody
#: else's. Printing the first when the second is true sends people
#: looking for a data problem that does not exist.
STATUS_ROUTE_AWAITING_EVIDENCE = "ROUTE_AWAITING_LIVE_EVIDENCE"


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
    """The line a monitor prints so nobody has to infer the answer.

    Three answers, not two. A route can be defined and still be waiting
    for its first confirmed live response, and that is a different thing
    from a session nobody can trade.
    """
    if not order_route_verified(session):
        return STATUS_SCAN_ONLY
    try:
        from config import session_capability

        if session_capability.route_awaiting_live_evidence(session):
            return STATUS_ROUTE_AWAITING_EVIDENCE
    except Exception:  # noqa: BLE001 - the evidence check is an extra
        # refinement; losing it must not downgrade a verified session to
        # SCAN_ONLY, which would understate what is actually usable.
        pass
    return STATUS_ORDER_VERIFIED
