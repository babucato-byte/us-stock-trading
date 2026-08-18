"""Session in, route out. The strategy never learns a TR id.

S1 decides WHAT to trade. This decides WHERE that order goes, and it is
the only place in the S1 path that knows KIS has more than one order
endpoint. Keeping those apart is what stops "we opened a new session"
from turning into a change to the scanner, and what stops a strategy
change from silently altering an endpoint.

    S1 decision -> SessionPolicy -> OrderRouter -> KIS route -> adapter

Refusing is the common case
---------------------------
Most sessions have no route. `route_for()` raises rather than returning
a default, because the failure it prevents is specific: sending a
premarket order down the regular endpoint because "extended hours" sounds
like it should work. A session without published evidence gets no route
at all, and the caller stops.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from config import s1_session_policy as sp

logger = logging.getLogger(__name__)

REASON_NO_ROUTE = "SESSION_HAS_NO_ORDER_ROUTE"
REASON_UNVERIFIED = "BROKER_SESSION_UNVERIFIED"
REASON_ENTRY_NOT_ALLOWED = "SESSION_ENTRY_NOT_ALLOWED"
REASON_EXIT_NOT_ALLOWED = "SESSION_EXIT_NOT_ALLOWED"

SIDE_BUY = "buy"
SIDE_SELL = "sell"


class OrderRouteUnavailable(Exception):
    """No route for this session and side. Never a fallback -- the caller
    must not place the order somewhere else."""

    def __init__(self, message, *, reason_code, session):
        super().__init__(message)
        self.reason_code = reason_code
        self.session = session


@dataclass(frozen=True)
class OrderRoute:
    session: str
    side: str
    route: str
    order_type: str
    verified: bool
    live_response_observed: bool

    def as_dict(self):
        return dict(vars(self))


def route_for(session, side, *, policy=None) -> OrderRoute:
    """The route for `side` in `session`, or a refusal naming why."""
    resolved = policy or sp.policy_for(session)
    name = resolved.session
    side = str(side or "").lower()
    if side not in (SIDE_BUY, SIDE_SELL):
        raise OrderRouteUnavailable(f"unknown side {side!r}",
                                    reason_code="UNKNOWN_SIDE", session=name)

    permitted = resolved.entry_allowed if side == SIDE_BUY else resolved.exit_allowed
    if not permitted:
        code = REASON_ENTRY_NOT_ALLOWED if side == SIDE_BUY else REASON_EXIT_NOT_ALLOWED
        raise OrderRouteUnavailable(
            f"{name} does not permit {side}: {resolved.reason}",
            reason_code=code, session=name)
    if resolved.route == sp.ROUTE_NONE:
        raise OrderRouteUnavailable(
            f"{name} has no order route: {resolved.reason}",
            reason_code=REASON_NO_ROUTE, session=name)
    if not resolved.verified:
        raise OrderRouteUnavailable(
            f"{name} route is not verified: {resolved.reason}",
            reason_code=REASON_UNVERIFIED, session=name)

    return OrderRoute(session=name, side=side, route=resolved.route,
                      order_type=resolved.order_type or sp.ORDER_TYPE_LIMIT,
                      verified=resolved.verified,
                      live_response_observed=resolved.live_response_observed)


def can_enter(session, *, policy=None) -> bool:
    try:
        route_for(session, SIDE_BUY, policy=policy)
        return True
    except OrderRouteUnavailable:
        return False


def can_exit(session, *, policy=None) -> bool:
    try:
        route_for(session, SIDE_SELL, policy=policy)
        return True
    except OrderRouteUnavailable:
        return False


def describe_matrix():
    """Session -> what BUY and SELL may do, as data. Used by reports and
    by a test that asserts the table did not change unnoticed."""
    rows = {}
    for name in sorted(sp.SESSION_POLICIES):
        entry = {}
        for side in (SIDE_BUY, SIDE_SELL):
            try:
                entry[side] = route_for(name, side).route
            except OrderRouteUnavailable as exc:
                entry[side] = f"BLOCKED:{exc.reason_code}"
        policy = sp.policy_for(name)
        entry["scan_allowed"] = policy.scan_allowed
        entry["verification"] = policy.verification
        rows[name] = entry
    return rows


def broker_route_name(order_route: Optional[OrderRoute]) -> str:
    """What to hand `KISBroker.submit_order(route=...)`.

    None means the standard route, which keeps every existing caller --
    all of which predate sessions -- behaving exactly as before.

    The default is named from the session module, not imported from the
    broker: this package must stay unable to reach `brokers`, and a route
    name is a string both sides already agree on.
    """
    if order_route is None:
        return sp.ROUTE_STANDARD
    return order_route.route
