"""What a session can actually do, asked in exactly one place.

The problem this exists for
---------------------------
Four modules each decided independently whether an order could be placed
"now", and they did not agree:

    live_pilot/bootstrap.py        `_order_session()`  -- routable session
    scripts/run_s6_runtime.py      `get_market_state() != "CLOSED"`
    s6_live/exit_runtime.py        the `orders_allowed` its caller passed
    scripts/final_pre_live_check.sh  `get_us_market_session() == "regular"`

`get_market_state()` models only the US venue's own sessions, so it
returns CLOSED for exactly 20:00->04:00 ET -- which is precisely the
OVERNIGHT_DAYTIME window. Gating orders on it therefore made daytime
trading structurally impossible: not "closed at the moment", but closed
for the entire session, every day, by construction. The entry path was
fixed first, which left the worse half in place -- a BUY that could be
placed into a session whose SELL was still latched.

So capability is decided here, once, and the four callers ask rather than
each deciding.

Two questions, not one
----------------------
ENTRY and EXIT are asked separately and are not the same question. A
strategy that has been stood down for new entries must still be able to
leave a position it already holds; the day that stops being true is the
day a stand-down becomes a way to trap capital. `entry_supported` may
therefore be False while `exit_supported` is True, and nothing in this
module ever returns the reverse for a routable session.

What it does NOT ask
--------------------
Whether the US primary market is open. KIS's 미국주간거래 executes while
that market is closed -- that is what it is for -- so "the US market is
closed" is not evidence that an order cannot be placed. It is evidence
about a different venue.

What it DOES still ask
----------------------
Whether the session falls on a real trading day. `scan_session.session_at`
is calendar-independent on purpose (a holiday still has a premarket
window on the clock), so the market-state check that was wrong about the
hours was nonetheless carrying the weekend and holiday guard. Removing it
without replacing that guard would have permitted a Saturday order. The
guard is restored here against the session's OWN trading day, which for
OVERNIGHT_DAYTIME is not the calendar date it starts on: the window that
opens 20:00 ET Friday belongs to Saturday and must be refused, while the
one that opens 20:00 ET Sunday belongs to Monday and must not be.

Fail closed
-----------
Every unknown -- an unrecognised session, a session with no KIS route, an
unreadable clock -- resolves to "no capability". A capability that cannot
be established is never assumed.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional, Tuple

from market_hours import EASTERN, MARKET_AFTERMARKET_END, is_market_day

#: Reason codes. Returned rather than raised: a caller deciding whether to
#: trade wants to say WHY it did not, and an exception at this layer would
#: be caught and flattened into "unavailable" by most of them.
CAPABLE = "CAPABLE"
NOT_A_SESSION = "NOT_A_SESSION"
NOT_A_TRADING_DAY = "NOT_A_TRADING_DAY"
NO_KIS_ROUTE = "NO_KIS_ROUTE"
STRATEGY_NOT_LIVE_IN_SESSION = "STRATEGY_NOT_LIVE_IN_SESSION"
ENTRY_DISABLED_FOR_STRATEGY = "ENTRY_DISABLED_FOR_STRATEGY"


@dataclass(frozen=True)
class SessionCapability:
    """One session, and everything a caller needs to decide about it.

    `entry_reason` / `exit_reason` are always populated -- CAPABLE when
    permitted -- so a refusal never has to be inferred from a bare False.
    """
    session: str
    trading_day: Optional[str]
    entry_supported: bool
    exit_supported: bool
    entry_reason: str
    exit_reason: str
    order_route_buy: Optional[Tuple[str, str]] = None
    order_route_sell: Optional[Tuple[str, str]] = None
    cancel_route: Optional[Tuple[str, str]] = None

    @property
    def orders_allowed(self) -> bool:
        """Back-compatible single answer: may ANY order be sent.

        Kept because several call sites genuinely mean "is this session
        transacting at all". A caller deciding about a specific side must
        ask `entry_supported` / `exit_supported` instead -- they differ.
        """
        return self.entry_supported or self.exit_supported


def current_session(now=None) -> str:
    """The session containing `now`, or "" if it cannot be determined."""
    try:
        from scanners.base import scan_session

        return str(scan_session.session_at(now) or "").strip().upper()
    except Exception:  # noqa: BLE001 - fail closed, never raise upward
        return ""


def trading_day_for(session, now=None):
    """The US trading day this session's window belongs to.

    For every session but OVERNIGHT_DAYTIME this is simply the Eastern
    calendar date. OVERNIGHT_DAYTIME straddles midnight, and the half
    before it belongs to the NEXT day: the window opening 20:00 ET on the
    25th is the overnight ahead of the 26th's session, and KIS trades it
    as the 26th. Dating it 25th would let a Friday-evening window inherit
    Friday's tradability and place an order into a Saturday.
    """
    name = str(session or "").strip().upper()
    try:
        from scanners.base.scan_session import EASTERN as _E  # noqa: F401
    except Exception:  # noqa: BLE001
        pass
    try:
        if now is None:
            from datetime import datetime

            moment = datetime.now(EASTERN)
        else:
            moment = now if now.tzinfo else now.replace(tzinfo=EASTERN)
        moment = moment.astimezone(EASTERN)
    except Exception:  # noqa: BLE001
        return None

    day = moment.date()
    if name == "OVERNIGHT_DAYTIME" and moment.time() >= MARKET_AFTERMARKET_END:
        day = day + timedelta(days=1)
    return day.isoformat()


def _routes(session):
    """(buy, sell, cancel) routes for the live environment, or None each.

    Resolved through the broker module so this file cannot drift from the
    endpoint table that the wire actually uses.
    """
    from brokers import kis_broker as kb

    def _one(fn, *args):
        try:
            return fn(*args)
        except Exception:  # noqa: BLE001
            return None

    return (_one(kb.order_route_for, session, "live", "buy"),
            _one(kb.order_route_for, session, "live", "sell"),
            _one(kb.cancel_route_for, session, "live"))


def capability_for(session, *, now=None, strategy_id=None) -> SessionCapability:
    """Everything decidable about `session`, as one value.

    `strategy_id` is optional: without it the answer is the SESSION's
    capability, which is what the runtime and the readiness checker want.
    With it, the strategy's own entry permission is applied on top -- a
    strategy stood down for new entries still keeps its exit.
    """
    name = str(session or "").strip().upper()
    day = trading_day_for(name, now)

    def _refuse(reason):
        return SessionCapability(
            session=name, trading_day=day,
            entry_supported=False, exit_supported=False,
            entry_reason=reason, exit_reason=reason)

    if not name:
        return _refuse(NOT_A_SESSION)

    from config import s6_sessions

    if not s6_sessions.scans(name):
        return _refuse(NOT_A_SESSION)

    # The strategy's session policy: which sessions may transact at all.
    if not s6_sessions.orders_allowed(name):
        return _refuse(STRATEGY_NOT_LIVE_IN_SESSION)

    # A route the wire actually defines. Asked of the broker, not assumed.
    from brokers import kis_broker as kb

    if name not in kb.ROUTED_SESSIONS:
        return _refuse(NO_KIS_ROUTE)

    buy, sell, cancel = _routes(name)
    if buy is None or sell is None:
        # A session that can be entered but not left is not a session we
        # trade. Both sides are required before either is offered.
        return _refuse(NO_KIS_ROUTE)

    # The weekend/holiday guard the market-state check used to carry.
    try:
        from datetime import date

        if day is None or not is_market_day(date.fromisoformat(day)):
            return _refuse(NOT_A_TRADING_DAY)
    except Exception:  # noqa: BLE001
        return _refuse(NOT_A_TRADING_DAY)

    entry_ok, entry_reason = True, CAPABLE
    if strategy_id is not None:
        from config import strategy_entry_policy

        if not strategy_entry_policy.entry_enabled(strategy_id):
            entry_ok, entry_reason = False, ENTRY_DISABLED_FOR_STRATEGY

    return SessionCapability(
        session=name, trading_day=day,
        entry_supported=entry_ok, exit_supported=True,
        entry_reason=entry_reason, exit_reason=CAPABLE,
        order_route_buy=buy, order_route_sell=sell, cancel_route=cancel)


def current_capability(*, now=None, strategy_id=None) -> SessionCapability:
    """`capability_for` the session we are actually in."""
    return capability_for(current_session(now), now=now, strategy_id=strategy_id)


def order_session(*, now=None, strategy_id=None) -> Optional[str]:
    """The session a real ENTRY may be routed into right now, or None.

    The bootstrap's original question, now answered from the shared
    resolver. None is a refusal, never a fallback to REGULAR: that
    fallback is exactly how an order reaches an endpoint that is not open
    at the hour it is sent.
    """
    cap = current_capability(now=now, strategy_id=strategy_id)
    return cap.session if cap.entry_supported else None


def route_awaiting_live_evidence(session) -> bool:
    """Does THIS session's KIS route still lack a live response?

    The regular five and the daytime five are separate sets against
    separate endpoints, so confirming one says nothing about the other --
    which is why the question is asked about a SESSION rather than in
    general.

    It lives here because two independent gates ask it: the safety
    re-check inside the order path, and the capability mint one step
    before the wire. They were written apart and only one of them was
    taught that ARMED is not a reason to refuse a bootstrap, which left
    the one-shot reachable through the first gate and refused by the
    second. Two copies of a rule are two chances to fix only one.
    """
    from brokers import kis_broker as kb

    posture = (kb.REQUIRED_FOR_DAYTIME
               if str(session or "").strip().upper() == "OVERNIGHT_DAYTIME"
               else kb.REQUIRED_FOR_ARMED)
    return bool(list(kb.pending_items_for(posture)))


def bootstrap_permitted_on_armed(*, now=None) -> bool:
    """May a one-shot bootstrap run on an ARMED deployment right now?

    `resolve_posture` returns ARMED whenever the three live flags are
    set, and LIMITED_LIVE_BOOTSTRAP only while they are not -- so on an
    armed deployment a posture-equality check makes the bootstrap
    unreachable. That is backwards for the case that matters: a route
    whose wire values have never been confirmed by a live response needs
    the bootstrap MORE, not less, and the general path must not be the
    thing that first touches it.

    Reaching LIMITED_LIVE_BOOTSTRAP instead by clearing the live flags is
    not an alternative: those flags are what `evaluate_sell_gate` reads,
    so turning them off to permit an entry would disable the EXIT of
    every position already held.
    """
    session = route_session(now=now)
    return session is not None and route_awaiting_live_evidence(session)


def route_session(*, now=None) -> Optional[str]:
    """The session an order should be ADDRESSED to right now, or None.

    Purely the envelope question: which endpoint family does this moment
    belong to. It takes no strategy on purpose.

    "Which endpoint does this session use" and "is this strategy allowed
    to open a position" are different questions. Folding the second into
    the first blocked every strategy that was stood down -- or merely not
    in the registry -- at the point where the caller was only trying to
    address the order correctly. Permission is decided by the gate; this
    decides where the message goes.
    """
    cap = current_capability(now=now)
    return cap.session if cap.orders_allowed else None


def exit_session(*, now=None) -> Optional[str]:
    """The session a real EXIT may be routed into right now, or None.

    Deliberately does not take a strategy: a held position's exit is not
    subject to that strategy's entry permission.
    """
    cap = current_capability(now=now)
    return cap.session if cap.exit_supported else None
