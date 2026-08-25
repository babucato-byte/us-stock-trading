"""S6 breakout family: which session may scan, and which may order.

Two independent questions
-------------------------
Scanning and ordering are decided separately, and conflating them is the
mistake this module exists to prevent. S6 validates in EVERY session --
that is the point of the family -- while real orders are confined to the
sessions whose broker route has been verified AND whose rollout has
actually reached them. A session can be scannable, route-verified, and
still not enabled: verification is a precondition for trading a session,
not a decision to.

    SCAN / VALIDATE   = ALL SESSIONS
    ORDER             = VERIFIED AND ENABLED SESSIONS ONLY

The variants
------------
One ORB reused across the day would be wrong. Each session forms its own
range from its own opening, because a breakout of 09:30's range means
nothing at 20:00 -- the participants, the liquidity and the reference
price are all different. So the family is four variants, each with its
own range, and `REGULAR_ORB_MINUTES` is not silently copied into the
others: the other sessions carry it as a REFERENCE to be measured
against 5 and 30, not as a decided value.
"""

from typing import Dict, FrozenSet

STRATEGY_ID = "S6_ORB_BREAKOUT_V1"
SCANNER_NAME = "orb"

VARIANT_REGULAR = "S6-R"
VARIANT_OVERNIGHT = "S6-O"
VARIANT_PREMARKET = "S6-P"
VARIANT_AFTER_HOURS = "S6-A"

#: Session -> variant. Every session S6 scans has exactly one variant,
#: so a candidate can always say which range produced it.
VARIANT_BY_SESSION: Dict[str, str] = {
    "REGULAR": VARIANT_REGULAR,
    "OVERNIGHT_DAYTIME": VARIANT_OVERNIGHT,
    "PREMARKET": VARIANT_PREMARKET,
    "AFTER_HOURS": VARIANT_AFTER_HOURS,
}

#: Scanned and measured everywhere.
SCAN_SESSIONS: FrozenSet[str] = frozenset(VARIANT_BY_SESSION)

#: Sessions in which a real order may be attempted.
#:
#: REGULAR and OVERNIGHT_DAYTIME, because those are the two whose KIS
#: order route the official specification defines:
#:
#:   REGULAR            /trading/order          TTTT1002U / TTTT1006U
#:   OVERNIGHT_DAYTIME  /trading/daytime-order  TTTS6036U / TTTS6037U
#:
#: PREMARKET and AFTER_HOURS are absent because no US extended-hours
#: order endpoint exists in the overseas API -- the complete set of US
#: order TRs is those four plus the two cancels. ORD_DVSN carries
#: LOO/LOC/MOO/MOC, but those are at-the-open and at-the-close types
#: WITHIN the regular session, not extended-hours trading. They are not
#: waiting on a decision here; there is nothing to enable.
#:
#: Membership is not permission on its own. Each session still needs its
#: own wire values confirmed by a real response -- REGULAR's five are the
#: ARMED set, OVERNIGHT_DAYTIME's five are REQUIRED_FOR_DAYTIME -- and
#: they are deliberately disjoint so neither session's pending evidence
#: can block the other. The S6 family limit of one position spans all
#: four variants regardless.
#: NOT widened yet. Adding OVERNIGHT_DAYTIME here is a one-line change
#: and it fails fourteen guards that encode "REGULAR is the only
#: ordering session" -- test_a_verified_route_is_not_permission_to_trade,
#: test_scan_allowed_never_widens_what_may_be_ordered, and
#: test_only_regular_may_order among them. Those are the deliberate
#: safety surface, not stale fixtures, and each one has to be re-stated
#: for two ordering sessions rather than switched off.
#:
#: Everything else the daytime session needs is in place: the route
#: (TTTS6036U/6037U/6038U), its own disjoint wire-value set
#: (REQUIRED_FOR_DAYTIME), and a per-strategy session gate so enabling
#: it cannot widen S1's hours. This line is what remains.
LIVE_SESSIONS: FrozenSet[str] = frozenset({"REGULAR"})

MODE_LIMITED_LIVE = "LIMITED_LIVE"
MODE_REALTIME_SHADOW = "REALTIME_SHADOW"

#: The REGULAR opening range, unchanged from ORB v1.0. Referenced here
#: rather than redefined; the scanner's config remains the source.
REGULAR_ORB_MINUTES = 15

#: Candidate range windows to compare in SHADOW for the non-REGULAR
#: sessions. 15 leads because it is the measured REGULAR value and the
#: obvious reference -- but it is a REFERENCE, not a decision, and the
#: comparison is what turns it into one. Copying it in silently would
#: make every session inherit a number chosen for a different one.
SHADOW_RANGE_MINUTES = (5, 15, 30)


def variant_for(session) -> str:
    """The variant that owns this session, or "" if S6 does not scan it."""
    return VARIANT_BY_SESSION.get(str(session or "").strip().upper(), "")


def scans(session) -> bool:
    return variant_for(session) != ""


def orders_allowed(session) -> bool:
    """Whether a REAL order may be placed in this session.

    Fails closed: an unrecognised session is not enabled.
    """
    return str(session or "").strip().upper() in LIVE_SESSIONS


def mode_for(session) -> str:
    """What the monitor prints, and what the executor honours."""
    return (MODE_LIMITED_LIVE if orders_allowed(session)
            else MODE_REALTIME_SHADOW)
