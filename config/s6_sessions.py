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

#: Sessions in which a real order may be ATTEMPTED -- all four.
#:
#: The overseas order API documents US orders in the premarket, the
#: regular session and the aftermarket, and they SHARE one endpoint and
#: one TR family:
#:
#:   PREMARKET / REGULAR / AFTER_HOURS
#:       /trading/order            TTTT1002U buy / TTTT1006U sell
#:       /trading/order-rvsecncl   TTTT1004U cancel
#:   OVERNIGHT_DAYTIME
#:       /trading/daytime-order            TTTS6036U / TTTS6037U
#:       /trading/daytime-order-rvsecncl   TTTS6038U
#:
#: Premarket and aftermarket were previously absent on the reasoning
#: that "no extended-hours TR exists, so the API cannot order there".
#: That inverted what the absence meant: having no session-specific TR
#: is what SHARING a route looks like. The mistake cost S6 half the
#: sessions it scans, in a family whose whole premise is that a breakout
#: is worth measuring in every session.
#:
#: The aftermarket EXTENSION is NOT included and is not a session here.
#: It is gated behind a per-customer application through HTS or the app,
#: so API support for it does not follow from the published schedule.
#: `config.kis_market_schedule` refuses it as UNVERIFIED rather than
#: assuming either way.
#:
#: Membership is CAPABILITY, not permission. Further gates stand between
#: this set and an order, and each is separately tested:
#:   * the ROUTE's wire values, confirmed by a real KIS response -- the
#:     GENERAL five and the DAYTIME five, disjoint so neither route's
#:     pending evidence blocks the other. They are per ROUTE, not per
#:     session: premarket, regular and aftermarket confirm one set
#:     together because they address one endpoint.
#:   * `strategy_entry_policy`, which can stand a strategy down for new
#:     entries without touching its exit
#:   * the S6 family limit of one position across all four variants
#:
#: WHEN each session is actually open is not decided here. KIS publishes
#: its windows in KST and they move against Eastern time with US DST;
#: `config.kis_market_schedule` derives them, and a session listed here
#: is still refused outside its window.
LIVE_SESSIONS: FrozenSet[str] = frozenset(
    {"PREMARKET", "REGULAR", "AFTER_HOURS", "OVERNIGHT_DAYTIME"})

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
