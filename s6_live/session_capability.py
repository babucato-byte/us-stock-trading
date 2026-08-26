"""Can an S6 order actually reach the broker in this session? Read-only.

Why this is derived and not declared
------------------------------------
§7 says: no assumption. A session whose route is unsupported or unclear
is BLOCKED_ORDER_ROUTE, and another session's endpoint is never reused
to fake one. So nothing here is a hand-written table of "which sessions
work". Every answer is read from the control that would ACTUALLY refuse
the order, and each one names the file and the refusal it read:

    scan_session.ORDER_VERIFIED_SESSIONS
        which routes have been verified against KIS at all.

    live_rollout_config.validate()
        raises on allow_extended_hours=True. PREMARKET and AFTER_HOURS
        orders are not merely unverified -- the rollout config REFUSES
        to validate with them enabled, and `run_live_buy_entry_cycle`
        raises `KISLiveTradingError` when validation fails, aborting the
        whole cycle. That refusal is a risk control, not a setting.

    rollout.regular_session_only -> order_gate.BuyGateContext
        `order_gate` blocks when `is_regular_session` is False, and the
        deployed env sets REGULAR_SESSION_ONLY=true.

    s6_sessions.LIVE_SESSIONS
        which sessions S6's ROLLOUT has reached. Separate from route
        verification on purpose: a verified route is a precondition for
        trading a session, not a decision to.

The distinction that matters
----------------------------
"Route not verified" and "route verified, rollout has not reached it"
are different facts with different fixes, and they are reported
separately rather than collapsed into one boolean. S2's entry policy
already keeps them apart for the same reason; this is that reasoning
applied per S6 variant.

Fill query is not assumed from BUY
----------------------------------
A session that accepts an order and a session whose fills can be read
back are not the same claim. `fill_query_verified` is answered from
whether the runtime has a working fill lookup wired for that session,
and today it is unverified everywhere -- `scripts/run_s6_runtime.py`
returns None from both fill lookups pending the live step. Reporting it
as verified because BUY works would be exactly the assumption §7
forbids.

It grants nothing
-----------------
Every function returns a verdict. Nothing here widens LIVE_SESSIONS,
edits a rollout flag, or places an order.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from config import s6_sessions

logger = logging.getLogger(__name__)

VERIFIED = "VERIFIED"
BLOCKED = "BLOCKED"
NOT_VERIFIED = "NOT_VERIFIED"

#: Why a route is unusable. Each maps to a different operator action.
REASON_ROUTE_UNVERIFIED = "ROUTE_NOT_VERIFIED_AGAINST_BROKER"
REASON_EXTENDED_HOURS_FORBIDDEN = "EXTENDED_HOURS_FORBIDDEN_BY_ROLLOUT"
REASON_REGULAR_ONLY = "ROLLOUT_IS_REGULAR_SESSION_ONLY"
REASON_ROLLOUT_NOT_REACHED = "ROUTE_VERIFIED_BUT_ROLLOUT_HAS_NOT_REACHED_IT"
REASON_FILL_QUERY_UNWIRED = "FILL_LOOKUP_NOT_WIRED"

#: Sessions the rollout config treats as extended hours. Named from the
#: refusal's own wording ("premarket/afterhours") rather than guessed.
EXTENDED_HOURS_SESSIONS = frozenset({"PREMARKET", "AFTER_HOURS"})


@dataclass(frozen=True)
class RouteVerdict:
    status: str
    reason: Optional[str] = None
    detail: str = ""

    @property
    def verified(self) -> bool:
        return self.status == VERIFIED

    def as_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "reason": self.reason,
                "detail": self.detail}


@dataclass
class SessionCapability:
    session: str
    variant: str
    buy: RouteVerdict
    sell: RouteVerdict
    fill_query: RouteVerdict

    @property
    def order_capable(self) -> bool:
        """A real order needs ALL THREE.

        A BUY that cannot be sold, or a fill that cannot be read back, is
        not a tradeable session -- it is a way to acquire a position
        nobody can account for.
        """
        return self.buy.verified and self.sell.verified and self.fill_query.verified

    def blocking_reasons(self) -> List[str]:
        return [v.reason for v in (self.buy, self.sell, self.fill_query)
                if v.reason]

    def as_dict(self) -> Dict[str, Any]:
        return {"session": self.session, "variant": self.variant,
                "buy_route": self.buy.as_dict(),
                "sell_route": self.sell.as_dict(),
                "fill_query": self.fill_query.as_dict(),
                "order_capable": self.order_capable,
                "blocking_reasons": self.blocking_reasons()}


def _extended_hours_forbidden(rollout=None) -> bool:
    """Does the rollout config REFUSE extended-hours orders?

    Asked by attempting the validation the buy cycle attempts, rather
    than by reading a flag: the refusal is what actually stops an order,
    and a flag read could drift from it.
    """
    from config.live_rollout_config import (LiveRolloutConfig,
                                            LiveRolloutConfigError)

    try:
        config = rollout or LiveRolloutConfig.from_env()
    except Exception:  # noqa: BLE001
        return True
    if getattr(config, "allow_extended_hours", False):
        return False
    # The flag is off. Confirm the config would REFUSE it if flipped,
    # so this reports the control rather than the current setting.
    try:
        import dataclasses

        dataclasses.replace(config, allow_extended_hours=True).validate()
    except LiveRolloutConfigError:
        return True
    except Exception:  # noqa: BLE001
        return True
    return False


def _regular_only(rollout=None) -> bool:
    from config.live_rollout_config import LiveRolloutConfig

    try:
        config = rollout or LiveRolloutConfig.from_env()
    except Exception:  # noqa: BLE001
        return True
    return bool(getattr(config, "regular_session_only", True))


def runtime_ready() -> RouteVerdict:
    """Is the fill inquiry IMPLEMENTED and callable? (§11)

    Separate from `actual_fill_observed`, and the split matters. This
    asks whether the code exists, imports, and is wired into the runtime
    -- a question with an answer today, before any S6 order has ever been
    placed. Whether a REAL S6 fill was read back is a production
    observation that only a real order can supply, and it stays
    NOT_MEASURED until one does.

    Answered by inspecting the runtime's own wiring rather than by a
    flag, so removing the wiring makes this go NOT_VERIFIED by itself.
    """
    try:
        import inspect

        from brokers import kis_fill_inquiry  # noqa: F401
        from scripts import run_s6_runtime

        source = (inspect.getsource(run_s6_runtime._fill_lookup)
                  + inspect.getsource(run_s6_runtime._order_id_for))
    except Exception as exc:  # noqa: BLE001
        return RouteVerdict(NOT_VERIFIED, REASON_FILL_QUERY_UNWIRED,
                            f"fill lookup could not be inspected: {exc}")

    if "kis_fill_inquiry.inquire" not in source:
        return RouteVerdict(
            NOT_VERIFIED, REASON_FILL_QUERY_UNWIRED,
            "the runtime's fill lookup does not call the KIS inquiry")
    return RouteVerdict(
        VERIFIED, None,
        "run_s6_runtime resolves the broker order id from the order "
        "ledgers and reads it through brokers.kis_fill_inquiry")


def _fill_query_verdict(session: str) -> RouteVerdict:
    """Can a fill be read back in this session?

    Session-independent: the inquiry is keyed on an order id, so a
    PREMARKET entry sold in REGULAR uses the same read. The verdict is
    therefore the runtime's, not the session's.
    """
    return runtime_ready()


def capability(session, *, rollout=None) -> SessionCapability:
    """What this session may actually do, with the reason for each answer.

    Delegated to `config.session_capability`, which is the same resolver
    the ORDER PATH uses. It used to decide here, and the two drifted:
    this module refused every non-REGULAR session on
    `rollout.regular_session_only`, a flag S6's order path does not
    consult -- `kis_live_trading._session_permitted` routes S6 through
    the shared resolver instead. So the report said BLOCKED for sessions
    S6 could actually trade, which is the more dangerous direction of
    wrong: an understated capability is invisible, and gets planned
    around rather than investigated.

    `static_capability` is asked rather than `capability_at` on purpose.
    This answers "what can this session do", not "what can it do at this
    instant" -- a report that used the clock would say no for the three
    sessions that happen to be shut while it runs.
    """
    from config import session_capability as authoritative
    from scanners.base import scan_session

    normalised = scan_session.normalize(session) or str(session or "").upper()
    variant = s6_sessions.variant_for(normalised)
    cap = authoritative.static_capability(normalised)
    fill = _fill_query_verdict(normalised)

    if cap.orders_allowed:
        ok = RouteVerdict(
            VERIFIED, None,
            f"{normalised} addresses the {cap.family} order family "
            f"({cap.order_route_buy[1]} buy / {cap.order_route_sell[1]} sell "
            f"/ {cap.cancel_route[1]} cancel) and is reached by the S6 rollout")
        return SessionCapability(normalised, variant, ok, ok, fill)

    # Refused. The reason is the resolver's, translated into this
    # module's vocabulary so operators reading a report see one story.
    reason, detail = {
        authoritative.STRATEGY_NOT_LIVE_IN_SESSION: (
            REASON_ROLLOUT_NOT_REACHED,
            f"a route exists; {normalised} is not in "
            f"s6_sessions.LIVE_SESSIONS {sorted(s6_sessions.LIVE_SESSIONS)}"),
        authoritative.ROUTE_FAMILY_UNVERIFIED: (
            REASON_ROUTE_UNVERIFIED,
            f"{normalised} is a window KIS runs but whose API support is "
            "not established -- the aftermarket extension is gated behind "
            "a per-customer application"),
    }.get(cap.entry_reason, (
        REASON_ROUTE_UNVERIFIED,
        f"{normalised} has no KIS order route ({cap.entry_reason})"))

    verdict = RouteVerdict(BLOCKED, reason, detail)
    return SessionCapability(normalised, variant, verdict, verdict, fill)


def all_sessions(*, rollout=None) -> Dict[str, SessionCapability]:
    """Every session S6 scans, in clock order."""
    from scanners.base import scan_session

    return {s: capability(s, rollout=rollout)
            for s in scan_session.SESSIONS
            if s6_sessions.scans(s)}
