"""Which US session is it, and what may S1 do in it.

Why this replaces two booleans
------------------------------
Session permission used to be `regular_session_only` and
`allow_extended_hours`. Two booleans can express "regular" and "not
regular" and nothing else, so every non-regular session collapsed into
one bucket -- which is why `allow_extended_hours=True` had to be refused
outright: turning it on would have opened premarket, after-hours AND the
overnight session together, on a single route, with only one of the three
actually verified.

This module says one thing per session instead. A session that KIS has a
published order route for can be enabled without implying anything about
the ones that do not.

The overnight session is not "extended hours"
---------------------------------------------
`market_hours` recognises PREMARKET (04:00), REGULAR (09:30), AFTERMARKET
(16:00-20:00) and calls everything else CLOSED. KIS's 미국주간거래 sits in
that "everything else": 20:00 ET through 04:00 ET, which is Korean
daytime, and it has its own endpoint and its own TR ids
(`/uapi/overseas-stock/v1/trading/daytime-order`, TTTS6036U/TTTS6037U).
Reading the name as "premarket" would send those orders to the wrong
route -- the two sessions do not even overlap.

verified vs live-response-observed
----------------------------------
Two different claims, kept apart because the codebase already
distinguishes them (`REFERENCE_VERIFIED` / `LIVE_RESPONSE_PENDING` in
brokers/kis_broker.py):

    reference_verified      KIS publishes this route and we have read its
                            schema from the official source.
    live_response_observed  a real order has actually come back from it.

A route may be implemented on the strength of the first. Whether it is
ENABLED for real money is a separate decision this module does not make.
"""

from dataclasses import dataclass
from datetime import time
from typing import Dict, Optional

# --- session names --------------------------------------------------------
CLOSED = "CLOSED"
OVERNIGHT_DAYTIME = "OVERNIGHT_DAYTIME"
PREMARKET = "PREMARKET"
REGULAR = "REGULAR"
AFTER_HOURS = "AFTER_HOURS"
UNKNOWN = "UNKNOWN"

# --- order routes ---------------------------------------------------------
ROUTE_NONE = "NONE"
ROUTE_STANDARD = "STANDARD_OVERSEAS_ORDER"
ROUTE_DAYTIME = "KIS_DAYTIME_ORDER"

# --- order types ----------------------------------------------------------
ORDER_TYPE_LIMIT = "LIMIT"

#: ET boundaries. The regular/premarket/after-hours values mirror
#: `market_hours` rather than restating them independently -- a second
#: opinion about when the session starts is a bug waiting for a DST
#: change. The overnight boundary is the one market_hours has no name for.
OVERNIGHT_START = time(20, 0)
OVERNIGHT_END = time(4, 0)


@dataclass(frozen=True)
class SessionPolicy:
    """What is permitted in one session. UNKNOWN denies everything."""

    session: str
    scan_allowed: bool
    entry_allowed: bool
    exit_allowed: bool
    route: str
    order_type: Optional[str]
    verified: bool
    #: True only once a real order has been observed on this route.
    live_response_observed: bool = False
    reason: str = ""

    @property
    def orderable(self) -> bool:
        """`s1_live/exit_runtime.SessionPolicy` compatibility: exits ask
        this one question, and it is about SELLING."""
        return self.exit_allowed

    @property
    def name(self) -> str:
        return self.session

    @property
    def orders_allowed(self) -> bool:
        return self.exit_allowed

    @property
    def sell_allowed(self) -> bool:
        """What `s1_live/exit_runtime.py` asks. Named separately from
        `entry_allowed` so a session that takes sells but not buys -- or
        the reverse -- cannot be collapsed into one answer by accident."""
        return self.exit_allowed

    @property
    def verification(self) -> str:
        if self.verified and self.live_response_observed:
            return "LIVE_RESPONSE_OBSERVED"
        if self.verified:
            return "REFERENCE_VERIFIED"
        return "BROKER_SESSION_UNVERIFIED"

    def as_dict(self) -> Dict[str, object]:
        return dict(vars(self), verification=self.verification,
                    orderable=self.orderable)


def _policy(session, *, scan, entry, exit_, route, order_type, verified,
            live_observed=False, reason=""):
    return SessionPolicy(
        session=session, scan_allowed=scan, entry_allowed=entry, exit_allowed=exit_,
        route=route, order_type=order_type, verified=verified,
        live_response_observed=live_observed, reason=reason)


#: The matrix. Every entry states its own evidence.
SESSION_POLICIES: Dict[str, SessionPolicy] = {
    # Reference-verified: examples_llm/overseas_stock/order/order.py,
    # TTTT1002U / TTTT1006U on /uapi/overseas-stock/v1/trading/order.
    # This is the route production has been trading on.
    REGULAR: _policy(
        REGULAR, scan=True, entry=True, exit_=True, route=ROUTE_STANDARD,
        order_type=ORDER_TYPE_LIMIT, verified=True,
        reason="standard overseas order route, in production use"),

    # Reference-verified: examples_llm/overseas_stock/daytime_order/
    # daytime_order.py -- /uapi/overseas-stock/v1/trading/daytime-order,
    # TTTS6036U (buy) / TTTS6037U (sell), NASD/NYSE/AMEX, ORD_DVSN="00"
    # only ("주간거래는 지정가만 가능"). No live response observed yet, so
    # enabling it for real money is a separate decision.
    OVERNIGHT_DAYTIME: _policy(
        OVERNIGHT_DAYTIME, scan=True, entry=True, exit_=True, route=ROUTE_DAYTIME,
        order_type=ORDER_TYPE_LIMIT, verified=True,
        reason="KIS daytime-order route, limit orders only"),

    # No published KIS order route has been found for the 04:00-09:30 ET
    # session. The standard route is NOT assumed to accept it: "extended
    # hours" is a name, not evidence. Scanning is still allowed, because
    # collecting candidates costs nothing and orders nothing.
    PREMARKET: _policy(
        PREMARKET, scan=True, entry=False, exit_=False, route=ROUTE_NONE,
        order_type=None, verified=False,
        reason="no official KIS order route identified for this session"),

    AFTER_HOURS: _policy(
        AFTER_HOURS, scan=True, entry=False, exit_=False, route=ROUTE_NONE,
        order_type=None, verified=False,
        reason="no official KIS order route identified for this session"),

    CLOSED: _policy(
        CLOSED, scan=True, entry=False, exit_=False, route=ROUTE_NONE,
        order_type=None, verified=False, reason="market closed"),

    # Not a session, a failure to determine one. Denies everything.
    UNKNOWN: _policy(
        UNKNOWN, scan=False, entry=False, exit_=False, route=ROUTE_NONE,
        order_type=None, verified=False,
        reason="session could not be determined -- fail closed"),
}

#: Sessions in which a real order may be routed at all.
ORDERABLE_SESSIONS = frozenset(
    name for name, policy in SESSION_POLICIES.items() if policy.route != ROUTE_NONE)


def policy_for(session: str) -> SessionPolicy:
    """The policy for a session name. Anything unrecognised is UNKNOWN,
    which denies everything -- a session we cannot name is not one we
    know a broker accepts."""
    return SESSION_POLICIES.get(str(session or "").upper(), SESSION_POLICIES[UNKNOWN])


def current_session(now=None) -> str:
    """The session name for `now`, in US/Eastern.

    Delegates the calendar and the clock to `market_hours` -- DST and
    holidays are its problem, and duplicating either here is how the two
    would eventually disagree. The one thing added is the overnight
    window, which `market_hours` folds into CLOSED because it predates
    KIS's daytime service.
    """
    import market_hours

    try:
        eastern = market_hours.eastern_now(now)
    except Exception:
        return UNKNOWN

    state = market_hours.get_market_state(eastern)
    if state == market_hours.PREMARKET:
        return PREMARKET
    if state == market_hours.REGULAR:
        return REGULAR
    if state == market_hours.AFTERMARKET:
        return AFTER_HOURS

    # market_hours says CLOSED. That covers both "no session at all" and
    # the overnight window, so the two are separated here.
    clock = eastern.time()
    overnight = clock >= OVERNIGHT_START or clock < OVERNIGHT_END
    if not overnight:
        return CLOSED
    # An overnight block belongs to the session of the day it ENDS on, so
    # 20:00 Monday runs into Tuesday's session. Both endpoints must be
    # trading days for it to be one.
    from datetime import timedelta

    session_day = eastern.date() if clock < OVERNIGHT_END \
        else (eastern + timedelta(days=1)).date()
    try:
        if not market_hours.is_market_day(session_day):
            return CLOSED
    except Exception:
        return UNKNOWN
    return OVERNIGHT_DAYTIME


def current_policy(now=None) -> SessionPolicy:
    return policy_for(current_session(now))


def matrix() -> Dict[str, Dict[str, object]]:
    """The whole table, for reports and for tests that assert it did not
    quietly change."""
    return {name: policy.as_dict() for name, policy in sorted(SESSION_POLICIES.items())}
