"""What state is each S6 variant in, and what is stopping the next one.

The four states (§17)
---------------------
    DISCOVERY_ONLY          scans and records; cannot order
    READY_FOR_LIMITED_LIVE  every precondition met; a human may promote
    LIMITED_LIVE            promoted, and may place a real order
    BLOCKED_ORDER_ROUTE     the route itself is refused; readiness is
                            not the question and cannot become it

BLOCKED is not "not ready yet"
------------------------------
READY_FOR_LIMITED_LIVE means the remaining step is a decision.
BLOCKED_ORDER_ROUTE means no amount of observation changes the answer,
because a control refuses the order: PREMARKET and AFTER_HOURS orders
make `live_rollout_config.validate()` raise, and that raise aborts the
whole buy cycle. Collapsing the two would let an operator wait for
evidence that can never arrive.

It computes; it does not promote
--------------------------------
`evaluate()` returns states. Moving a variant to LIMITED_LIVE is editing
`scanner_live_mode.SCANNER_LIVE_MODE` and `s6_sessions.LIVE_SESSIONS` --
two deliberate, reviewed edits, performed after reading this. Nothing
here writes either, and tests assert that against the import graph.

Every precondition is AND
-------------------------
§11 and §17 list them and every one is required: production market
observation, freshness, COMMON_STOCK dry run, BUY route, SELL route,
fill query. A BUY route without a verified SELL is how an account
acquires a position it cannot leave; a fill query that was never proved
is how it acquires one it cannot count.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config import s6_sessions

logger = logging.getLogger(__name__)

DISCOVERY_ONLY = "DISCOVERY_ONLY"
READY_FOR_LIMITED_LIVE = "READY_FOR_LIMITED_LIVE"
LIMITED_LIVE = "LIMITED_LIVE"
BLOCKED_ORDER_ROUTE = "BLOCKED_ORDER_ROUTE"

PASS = "PASS"
FAIL = "FAIL"
NOT_MEASURED = "NOT_MEASURED"

#: The observation checks each variant needs, in the order §10 names
#: them. The prefix is the variant's own so two variants can never share
#: an observation -- an OVERNIGHT tick is not evidence about REGULAR.
OBSERVATION_CHECKS = ("market_tick_verified",
                      "candidate_freshness_verified",
                      "common_stock_dry_run_verified")

ROUTE_CHECKS = ("buy_route_verified", "sell_route_verified",
                "fill_query_verified")

#: Variant -> the prefix its per-session checks carry.
PREFIX = {
    s6_sessions.VARIANT_REGULAR: "regular",
    s6_sessions.VARIANT_OVERNIGHT: "overnight",
    s6_sessions.VARIANT_PREMARKET: "premarket",
    s6_sessions.VARIANT_AFTER_HOURS: "afterhours",
}


@dataclass
class VariantState:
    variant: str
    session: str
    mode: str
    checks: Dict[str, str] = field(default_factory=dict)
    detail: Dict[str, str] = field(default_factory=dict)
    blocking: List[str] = field(default_factory=list)

    @property
    def may_order(self) -> bool:
        return self.mode == LIMITED_LIVE

    def as_dict(self) -> Dict[str, Any]:
        return {"variant": self.variant, "session": self.session,
                "mode": self.mode, "checks": dict(self.checks),
                "detail": dict(self.detail), "blocking": list(self.blocking)}


def _observation_status(value) -> str:
    if value is None:
        return NOT_MEASURED
    return PASS if value else FAIL


def evaluate_variant(session, *, observations=None, rollout=None,
                     modes=None) -> VariantState:
    """One variant's state. Never raises, never promotes.

    `observations` carries the production facts, keyed by the variant's
    own prefixed names (e.g. `regular_market_tick_verified`). Anything
    absent is NOT_MEASURED -- never assumed, and a closed market
    therefore produces no state change at all.
    """
    from s6_live import session_capability

    variant = s6_sessions.variant_for(session)
    prefix = PREFIX.get(variant, "unknown")
    seen = observations or {}
    checks: Dict[str, str] = {}
    detail: Dict[str, str] = {}

    try:
        capability = session_capability.capability(session, rollout=rollout)
    except Exception as exc:  # noqa: BLE001 - an unanswerable capability
        # question is not a licence to trade.
        logger.warning("S6 capability probe failed for %s", session,
                       exc_info=True)
        return VariantState(
            variant, str(session), BLOCKED_ORDER_ROUTE,
            {f"{prefix}_{c}": NOT_MEASURED for c in ROUTE_CHECKS},
            {"capability": f"probe failed: {exc}"},
            [f"capability probe failed: {exc}"])

    for name, verdict in (("buy_route_verified", capability.buy),
                          ("sell_route_verified", capability.sell),
                          ("fill_query_verified", capability.fill_query)):
        key = f"{prefix}_{name}"
        checks[key] = PASS if verdict.verified else NOT_MEASURED
        if verdict.reason:
            detail[key] = f"{verdict.reason}: {verdict.detail}"

    for name in OBSERVATION_CHECKS:
        key = f"{prefix}_{name}"
        checks[key] = _observation_status(seen.get(key))

    blocking = [k for k, v in checks.items() if v != PASS]

    # A refused route is its own state. Reporting it as DISCOVERY_ONLY
    # would invite waiting for observations that cannot change it.
    route_blocked = any(v.status == session_capability.BLOCKED
                        for v in (capability.buy, capability.sell))
    if route_blocked:
        return VariantState(variant, str(session), BLOCKED_ORDER_ROUTE,
                            checks, detail, blocking)

    live_now = _is_live(modes) and s6_sessions.orders_allowed(session)
    if live_now:
        return VariantState(variant, str(session), LIMITED_LIVE, checks,
                            detail, blocking)

    if not blocking:
        return VariantState(variant, str(session), READY_FOR_LIMITED_LIVE,
                            checks, detail, blocking)

    return VariantState(variant, str(session), DISCOVERY_ONLY, checks,
                        detail, blocking)


def _is_live(modes=None) -> bool:
    from config import scanner_live_mode

    return scanner_live_mode.is_limited_live(s6_sessions.SCANNER_NAME, modes)


def evaluate(*, observations=None, rollout=None, modes=None
             ) -> Dict[str, VariantState]:
    """Every S6 variant, keyed by variant id."""
    from scanners.base import scan_session

    out: Dict[str, VariantState] = {}
    for session in scan_session.SESSIONS:
        if not s6_sessions.scans(session):
            continue
        state = evaluate_variant(session, observations=observations,
                                 rollout=rollout, modes=modes)
        out[state.variant] = state
    return out


def format_table(states: Dict[str, VariantState]) -> str:
    """The §21 table: one row per variant, states not adjectives."""
    order = (s6_sessions.VARIANT_OVERNIGHT, s6_sessions.VARIANT_PREMARKET,
             s6_sessions.VARIANT_REGULAR, s6_sessions.VARIANT_AFTER_HOURS)
    head = (f"{'VARIANT':<7} {'SESSION':<18} {'TICK':<13} {'FRESH':<13} "
            f"{'COMMON':<13} {'BUY':<13} {'SELL':<13} {'FILL':<13} MODE")
    lines = [head, "-" * len(head)]
    for variant in order:
        state = states.get(variant)
        if state is None:
            continue
        p = PREFIX.get(variant, "unknown")
        c = state.checks
        lines.append(
            f"{variant:<7} {state.session:<18} "
            f"{c.get(p + '_market_tick_verified', '-'):<13} "
            f"{c.get(p + '_candidate_freshness_verified', '-'):<13} "
            f"{c.get(p + '_common_stock_dry_run_verified', '-'):<13} "
            f"{c.get(p + '_buy_route_verified', '-'):<13} "
            f"{c.get(p + '_sell_route_verified', '-'):<13} "
            f"{c.get(p + '_fill_query_verified', '-'):<13} "
            f"{state.mode}")
    return "\n".join(lines)
