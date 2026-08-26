"""What a session can actually do, asked in exactly one place.

The problem this exists for
---------------------------
Four modules each decided independently whether an order could be placed
"now", and they did not agree. `get_market_state()` models the US venue's
own sessions and returns CLOSED for exactly the daytime window, so gating
on it made daytime orders structurally impossible. Capability is decided
here, once, and the callers ask.

Capability is time-driven, not name-driven
------------------------------------------
The authoritative input is a MOMENT, resolved against
`config.kis_market_schedule` -- KIS's own published windows, expressed in
KST because that is where KIS expresses them. Session NAMES are a label
on the answer, never the input to it.

That distinction is the fix for a real defect. `scan_session` partitions
the day by fixed Eastern hours, which is right for scanning: every scan
lands in exactly one bucket and the buckets never move. It is wrong for
ORDERING, because KIS's windows are fixed in KST and the KST->ET offset
moves an hour with US daylight saving. The fixed-ET daytime boundary was
correct in December and an hour early in August, so every DST day had a
window -- 20:00-21:00 ET -- in which the system believed it could place a
daytime order while KIS had no session open at all.

Two families, not four sessions
-------------------------------
What an order needs to know is which API family to address:

    GENERAL  프리마켓 / 정규장 / 애프터마켓
             TTTT1002U buy, TTTT1006U sell, TTTT1004U cancel
    DAYTIME  미국주간거래
             TTTS6036U buy, TTTS6037U sell, TTTS6038U cancel

The general family covers premarket and aftermarket as well as the
regular session: the overseas order API documents US orders in all three.
Reading "there is no premarket-specific TR" as "the API cannot order in
premarket" was wrong, and it cost S6 two of the four sessions it scans.

One strategy, every session it can reach
----------------------------------------
S6 is one strategy with one execution path. Sessions differ in which
route family they address and nothing else -- there is no per-session
strategy, and the position records `entry_session` / `exit_session`
rather than branching on them.

Two questions, not one
----------------------
ENTRY and EXIT are asked separately. A strategy stood down for new
entries must still be able to leave what it holds; the day that stops
being true is the day a stand-down becomes a way to trap capital.

Fail closed
-----------
Every unknown -- a window with no established family, an unreadable
clock, a non-trading day -- resolves to no capability. The aftermarket
EXTENSION is the deliberate example: it is gated behind a per-customer
application, so whether an API order is accepted in it does not follow
from the schedule, and it is refused rather than assumed.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

from config import kis_market_schedule as schedule

#: Reason codes. Returned rather than raised: a caller deciding whether
#: to trade wants to say WHY it did not.
CAPABLE = "CAPABLE"
NOT_A_SESSION = "NOT_A_SESSION"
NOT_A_TRADING_DAY = "NOT_A_TRADING_DAY"
NO_KIS_ROUTE = "NO_KIS_ROUTE"
MARKET_CLOSED = "MARKET_CLOSED"
ROUTE_FAMILY_UNVERIFIED = "ROUTE_FAMILY_UNVERIFIED"
STRATEGY_NOT_LIVE_IN_SESSION = "STRATEGY_NOT_LIVE_IN_SESSION"
ENTRY_DISABLED_FOR_STRATEGY = "ENTRY_DISABLED_FOR_STRATEGY"

FAMILY_GENERAL = schedule.FAMILY_GENERAL
FAMILY_DAYTIME = schedule.FAMILY_DAYTIME

#: KIS window -> the name the rest of the system uses for that session.
#: The concepts are identical; only the vocabulary differs, and the
#: scanner's vocabulary is what candidates and variants are keyed by.
SESSION_BY_WINDOW = {
    schedule.WINDOW_PREMARKET: "PREMARKET",
    schedule.WINDOW_REGULAR: "REGULAR",
    schedule.WINDOW_AFTERMARKET: "AFTER_HOURS",
    schedule.WINDOW_DAYTIME: "OVERNIGHT_DAYTIME",
}
WINDOW_BY_SESSION = {v: k for k, v in SESSION_BY_WINDOW.items()}


@dataclass(frozen=True)
class SessionCapability:
    """One moment, and everything a caller needs to decide about it."""
    session: str
    window: str
    family: Optional[str]
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
        """May ANY order be sent. A caller deciding about a specific side
        must ask `entry_supported` / `exit_supported` -- they differ."""
        return self.entry_supported or self.exit_supported


def _now(moment=None):
    if moment is not None:
        return moment
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _routes(family):
    """(buy, sell, cancel) for a family, or None each.

    Resolved through the broker so this file cannot drift from the
    endpoint table the wire actually uses.
    """
    from brokers import kis_broker as kb

    def _one(fn, *args):
        try:
            return fn(*args)
        except Exception:  # noqa: BLE001
            return None

    return (_one(kb.order_route_for_family, family, "live", "buy"),
            _one(kb.order_route_for_family, family, "live", "sell"),
            _one(kb.cancel_route_for_family, family, "live"))


def capability_at(moment=None, *, strategy_id=None) -> SessionCapability:
    """Everything decidable about `moment`, as one value.

    `strategy_id` is optional: without it the answer is the SESSION's
    capability, which is what the runtime and readiness checker want.
    With it, the strategy's own entry permission is applied on top -- a
    strategy stood down for new entries still keeps its exit.
    """
    moment = _now(moment)
    window = schedule.window_at(moment)
    session = SESSION_BY_WINDOW.get(window, "")
    day = schedule.trading_day_for(moment, window)

    def _refuse(reason):
        return SessionCapability(
            session=session, window=window, family=None, trading_day=day,
            entry_supported=False, exit_supported=False,
            entry_reason=reason, exit_reason=reason)

    if window == schedule.WINDOW_CLOSED:
        return _refuse(MARKET_CLOSED)

    family = schedule.family_for_window(window)
    if family is None:
        # A window KIS runs but whose API support is not established --
        # the aftermarket extension, gated behind a separate application.
        return _refuse(ROUTE_FAMILY_UNVERIFIED)

    if not session:
        return _refuse(NOT_A_SESSION)

    # The strategy's session policy: which sessions it has been rolled
    # out to. Separate from whether a route exists.
    from config import s6_sessions

    if not s6_sessions.orders_allowed(session):
        return _refuse(STRATEGY_NOT_LIVE_IN_SESSION)

    buy, sell, cancel = _routes(family)
    if buy is None or sell is None:
        # A session that can be entered but not left is not one we trade.
        return _refuse(NO_KIS_ROUTE)

    if day is None or not schedule.is_trading_day(day):
        return _refuse(NOT_A_TRADING_DAY)

    entry_ok, entry_reason = True, CAPABLE
    if strategy_id is not None:
        from config import strategy_entry_policy

        if not strategy_entry_policy.entry_enabled(strategy_id):
            entry_ok, entry_reason = False, ENTRY_DISABLED_FOR_STRATEGY

    return SessionCapability(
        session=session, window=window, family=family, trading_day=day,
        entry_supported=entry_ok, exit_supported=True,
        entry_reason=entry_reason, exit_reason=CAPABLE,
        order_route_buy=buy, order_route_sell=sell, cancel_route=cancel)


def static_capability(session, *, strategy_id=None) -> SessionCapability:
    """Can this session EVER place an order? Asked without a clock.

    Reports need this. "What can PREMARKET do" is a question about the
    route family and the rollout, and answering it with the CURRENT
    moment would make every report say "no" for the three sessions that
    happen not to be open while it runs.

    `capability_at` is this plus the clock: same family and routes, with
    the window and calendar applied on top. A report that used this and a
    gate that used `capability_at` cannot disagree about capability --
    only about whether it is available right now, which is the honest
    difference between them.
    """
    name = str(session or "").strip().upper()
    window = WINDOW_BY_SESSION.get(name)

    def _refuse(reason):
        return SessionCapability(
            session=name, window=window or "", family=None, trading_day=None,
            entry_supported=False, exit_supported=False,
            entry_reason=reason, exit_reason=reason)

    if window is None:
        return _refuse(NOT_A_SESSION)

    family = schedule.family_for_window(window)
    if family is None:
        return _refuse(ROUTE_FAMILY_UNVERIFIED)

    from config import s6_sessions

    if not s6_sessions.orders_allowed(name):
        return _refuse(STRATEGY_NOT_LIVE_IN_SESSION)

    buy, sell, cancel = _routes(family)
    if buy is None or sell is None:
        return _refuse(NO_KIS_ROUTE)

    entry_ok, entry_reason = True, CAPABLE
    if strategy_id is not None:
        from config import strategy_entry_policy

        if not strategy_entry_policy.entry_enabled(strategy_id):
            entry_ok, entry_reason = False, ENTRY_DISABLED_FOR_STRATEGY

    return SessionCapability(
        session=name, window=window, family=family, trading_day=None,
        entry_supported=entry_ok, exit_supported=True,
        entry_reason=entry_reason, exit_reason=CAPABLE,
        order_route_buy=buy, order_route_sell=sell, cancel_route=cancel)


def current_capability(*, now=None, strategy_id=None) -> SessionCapability:
    """`capability_at` for the moment we are actually in."""
    return capability_at(now, strategy_id=strategy_id)


def capability_for(session, *, now=None, strategy_id=None) -> SessionCapability:
    """Capability at `now`, refused when it is not `session`'s window.

    Kept for callers that already know which session they believe they
    are in -- the runtime passes the scanner's answer. The MOMENT still
    decides; naming a session only adds the requirement that the two
    agree. They can disagree: `scan_session` partitions by fixed Eastern
    hours while KIS's windows move with DST, and during the hour where
    they differ the honest answer is no capability rather than whichever
    source was asked first.
    """
    cap = capability_at(now, strategy_id=strategy_id)
    name = str(session or "").strip().upper()
    if not name or name != cap.session:
        return SessionCapability(
            session=cap.session, window=cap.window, family=None,
            trading_day=cap.trading_day,
            entry_supported=False, exit_supported=False,
            entry_reason=NOT_A_SESSION, exit_reason=NOT_A_SESSION)
    return cap


def current_session(now=None) -> str:
    """The session name for `now`, or "" when KIS runs no window then."""
    return capability_at(now).session


def current_window(now=None) -> str:
    """The KIS window for `now`, including CLOSED and the extension."""
    return schedule.window_at(_now(now))


def route_family(now=None) -> Optional[str]:
    """GENERAL, DAYTIME, or None -- which API family `now` addresses."""
    return capability_at(now).family


def route_session(*, now=None) -> Optional[str]:
    """The session an order should be ADDRESSED to, or None.

    Purely the envelope question. It takes no strategy on purpose:
    "which endpoint does this moment use" and "may this strategy open a
    position" are different questions, and folding the second into the
    first blocks strategies at the point where the caller was only trying
    to address the order correctly.
    """
    cap = capability_at(now)
    return cap.session if cap.orders_allowed else None


def order_session(*, now=None, strategy_id=None) -> Optional[str]:
    """The session a real ENTRY may be routed into, or None.

    None is a refusal, never a fallback to REGULAR: that fallback is
    exactly how an order reaches an endpoint that is not open.
    """
    cap = capability_at(now, strategy_id=strategy_id)
    return cap.session if cap.entry_supported else None


def exit_session(*, now=None) -> Optional[str]:
    """The session a real EXIT may be routed into, or None.

    Deliberately takes no strategy: a held position's exit is not subject
    to that strategy's entry permission.
    """
    cap = capability_at(now)
    return cap.session if cap.exit_supported else None


def evidence_posture_for_family(family):
    """Which wire-value set a family's live evidence belongs to.

    The two sets are per ROUTE, not per session. "The ARMED five" is a
    name from when the regular session was the only one considered; they
    are the GENERAL route's five, and premarket, regular and aftermarket
    all confirm them together because they all address that route. The
    daytime five are separate because the endpoint and TR family are.
    """
    from brokers import kis_broker as kb

    return (kb.REQUIRED_FOR_DAYTIME if family == FAMILY_DAYTIME
            else kb.REQUIRED_FOR_ARMED)


def route_awaiting_live_evidence(session_or_family) -> bool:
    """Does this route still lack a live response?

    Accepts a family or a session name. It lives here because two
    independent gates ask it -- the safety re-check inside the order
    path, and the capability mint one step before the wire -- and when
    they were written apart only one of them was taught that ARMED is not
    a reason to refuse a bootstrap.
    """
    from brokers import kis_broker as kb

    name = str(session_or_family or "").strip().upper()
    family = name if name in (FAMILY_GENERAL, FAMILY_DAYTIME) else (
        FAMILY_DAYTIME if name == "OVERNIGHT_DAYTIME" else FAMILY_GENERAL)
    return bool(list(kb.pending_items_for(evidence_posture_for_family(family))))


def bootstrap_permitted_on_armed(*, now=None) -> bool:
    """May a one-shot bootstrap run on an ARMED deployment right now?

    `resolve_posture` returns ARMED whenever the three live flags are set,
    so a posture-equality check makes the bootstrap unreachable exactly
    where it is most needed: a route whose wire values have never been
    confirmed. Reaching LIMITED_LIVE_BOOTSTRAP instead by clearing those
    flags is not an alternative -- they are what `evaluate_sell_gate`
    reads, so turning them off to permit an entry would disable the EXIT
    of every position already held.
    """
    cap = capability_at(now)
    if not cap.orders_allowed or cap.family is None:
        return False
    return route_awaiting_live_evidence(cap.family)
