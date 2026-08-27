"""A buy may not be the order that proves a route works.

The gap this closes
-------------------
`route_awaiting_live_evidence` existed and was consulted by preflight and
by the one-shot bootstrap -- but not by the ordinary live order path. The
DAYTIME family's wire values were all LIVE_RESPONSE_PENDING, so a normal
S6 entry would have sent a real BUY on TTTS6036U: a first order proving a
route, placed by a strategy that did not know it was proving anything.

Buys only
---------
A sell is deliberately not gated. Refusing to exit on an unproven route
traps a position the account already holds, which is a larger risk than
the route being unproven -- and an exit that cannot be routed LATCHES and
retries rather than being abandoned. Reducing exposure on an imperfectly
evidenced route beats being unable to reduce it.

Evidence is per leg
-------------------
On 2026-08-27 S6 exited DT through /trading/daytime-order with TTTS6037U,
accepted as 0000001014 and filled 1 @ 51.61. That confirms the daytime
PATH and the daytime SELL TR, and nothing else: TTTS6036U has never
carried a buy and TTTS6038U has never carried a cancel.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from brokers import kis_broker as kb  # noqa: E402
from config import session_capability as sc  # noqa: E402
from execution.order_gate import (  # noqa: E402
    ROUTE_UNVERIFIED, OrderGateBlockedError, evaluate_buy_gate,
)

from tests import test_order_gate as fixtures  # noqa: E402


def _ctx(session="REGULAR", **overrides):
    kwargs = dict(order_intent=fixtures._order_intent(session=session))
    kwargs.update(overrides)
    return fixtures._buy_ctx(**kwargs)


def _blocked(ctx):
    with pytest.raises(OrderGateBlockedError) as excinfo:
        evaluate_buy_gate(ctx)
    return excinfo.value


class TestEvidenceIsRecordedPerLeg:
    def test_the_general_family_is_fully_verified(self):
        assert list(kb.pending_items_for(kb.REQUIRED_FOR_ARMED)) == []

    def test_the_daytime_sell_leg_now_has_live_evidence(self):
        pending = set(kb.pending_items_for(kb.REQUIRED_FOR_DAYTIME))
        assert "daytime_order_tr_id_live_sell" not in pending
        assert "daytime_order_path" not in pending

    def test_the_daytime_buy_and_cancel_legs_do_not(self):
        """Never inferred from the sell. Marking a wire value nothing has
        exercised is what this matrix exists to prevent."""
        pending = set(kb.pending_items_for(kb.REQUIRED_FOR_DAYTIME))
        assert "daytime_order_tr_id_live_buy" in pending
        assert "daytime_cancel_path" in pending
        assert "daytime_cancel_tr_id_live" in pending

    def test_the_evidence_names_the_order_that_produced_it(self):
        assert "0000001014" in kb.DAYTIME_SELL_EVIDENCE
        assert "TTTS6037U" in kb.DAYTIME_SELL_EVIDENCE


class TestAVerifiedRoutePasses:
    """Item 2: REGULAR data + route PASS."""

    @pytest.mark.parametrize("session", ["REGULAR", "PREMARKET", "AFTER_HOURS"])
    def test_the_general_sessions_are_not_route_blocked(self, session):
        assert sc.route_awaiting_live_evidence(session) is False
        assert evaluate_buy_gate(_ctx(session=session)) is True


class TestAnUnverifiedRouteBlocksTheBuy:
    """Items 5, 6, 9."""

    def test_a_daytime_buy_is_refused(self):
        assert sc.route_awaiting_live_evidence("OVERNIGHT_DAYTIME") is True
        blocked = _blocked(_ctx(session="OVERNIGHT_DAYTIME"))
        assert blocked.code == ROUTE_UNVERIFIED

    def test_the_refusal_names_the_unproven_wire_values(self):
        """'the route is unverified' is not actionable; which leg is."""
        message = str(_blocked(_ctx(session="OVERNIGHT_DAYTIME")))
        assert "daytime_order_tr_id_live_buy" in message
        assert "DAYTIME" in message

    def test_a_buy_without_a_session_is_judged_on_the_route_it_will_take(self):
        """KISBroker falls back to REGULAR when an intent names no
        session, so that is the route actually used -- and it is
        verified. Judging a different session than the wire will use
        would be judging nothing."""
        from execution import order_gate

        assert order_gate._BROKER_SESSION_FALLBACK == "REGULAR"
        assert evaluate_buy_gate(_ctx(session=None)) is True

    def test_the_fallback_matches_the_brokers_own(self):
        """If the broker's hint changes, the gate must follow or it
        judges a route the order does not take."""
        from brokers.kis_broker import KISBroker
        from execution import order_gate

        assert order_gate._BROKER_SESSION_FALLBACK == KISBroker._session_hint

    def test_it_runs_before_the_capacity_caps(self):
        """So an unproven route is reported as such rather than as a
        full account."""
        from execution import order_gate

        seq = order_gate.BUY_GATE_SEQUENCE
        assert seq.index(ROUTE_UNVERIFIED) < seq.index("MAX_OPEN_POSITIONS")


class TestSellsAreNotGatedHere:
    def test_a_sell_intent_is_not_route_blocked(self):
        """Trapping an exit is a larger risk than an unproven route, and
        the DT sell proves the daytime path carries one."""
        from execution import order_gate

        ctx = _ctx(session="OVERNIGHT_DAYTIME",
                   order_intent=fixtures._order_intent(
                       session="OVERNIGHT_DAYTIME", side="sell"))
        # The route check abstains; any refusal must come from elsewhere.
        try:
            order_gate._check_route_evidence(ctx)
        except OrderGateBlockedError as exc:
            pytest.fail(f"a sell was route-blocked: {exc}")

    def test_the_sell_gate_never_calls_it(self):
        import inspect

        from execution import order_gate

        source = inspect.getsource(order_gate.evaluate_sell_gate)
        assert "_check_route_evidence" not in source


class TestUnknownFailsClosed:
    def test_an_unresolvable_session_is_refused(self, monkeypatch):
        monkeypatch.setattr(
            sc, "route_awaiting_live_evidence",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no family")))
        blocked = _blocked(_ctx(session="REGULAR"))
        assert blocked.code == ROUTE_UNVERIFIED
