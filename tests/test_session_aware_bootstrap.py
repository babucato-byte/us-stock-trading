"""The bootstrap follows the session, and the order follows the route.

Three defects, one theme: the live order path assumed REGULAR in places
where the session was actually a variable.

1. `OrderIntent` had no `session`, and `KISBroker.submit_order` reads
   `order_intent.session or self._session_hint` where the hint is a
   CLASS attribute defaulting to "REGULAR". So an S6 order placed in
   OVERNIGHT_DAYTIME was addressed to the regular endpoint -- a
   different TR family, at an hour that endpoint does not run.
2. The bootstrap refused any session but REGULAR, so the daytime route
   could never be exercised by the one mechanism built to capture a live
   response for it.
3. `final_safety_recheck` refused whenever the deployment was ARMED,
   because `resolve_posture` returns ARMED for the three live flags and
   LIMITED_LIVE_BOOTSTRAP only in their absence. A route with no live
   evidence needs the bootstrap MORE on an armed deployment, not less.
"""

from datetime import datetime, timezone

import pytest

from domain.order_intent import OrderIntent, OrderIntentError
from live_pilot import bootstrap

NOW = datetime(2026, 8, 26, 1, 0, tzinfo=timezone.utc)


def _intent(**overrides):
    kwargs = dict(
        internal_order_id="ord-1", signal_id="sig-1",
        strategy_id="S6_ORB_BREAKOUT_V1", symbol="AAPL", exchange="NASDAQ",
        side="buy", quantity=1, order_type="limit", limit_price=100.0,
        stop_price=None, target_price=None, created_at=NOW)
    kwargs.update(overrides)
    return OrderIntent(**kwargs)


class TestTheOrderCarriesItsSession:
    def test_session_defaults_to_none_and_changes_nothing(self):
        """A caller that does not know its session keeps the previous
        behaviour exactly -- the broker falls back to its hint."""
        assert _intent().session is None

    def test_a_session_is_carried(self):
        assert _intent(session="OVERNIGHT_DAYTIME").session == "OVERNIGHT_DAYTIME"

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_session_is_a_fault_not_a_default(self, blank):
        """None means "I do not know"; blank means a caller COMPUTED one
        and got nothing. On an order path that must not resolve quietly
        to the regular route."""
        with pytest.raises(OrderIntentError) as excinfo:
            _intent(session=blank)
        assert "session" in str(excinfo.value)

    def test_the_broker_reads_the_intent_before_its_own_hint(self):
        """The route follows the order, not the process."""
        from brokers.kis_broker import KISBroker, order_route_for

        assert KISBroker._session_hint == "REGULAR"
        regular = order_route_for("REGULAR", "live", "buy")
        daytime = order_route_for("OVERNIGHT_DAYTIME", "live", "buy")
        # Different endpoint AND different TR -- which is why sending one
        # for the other is a real mis-routed order, not a cosmetic slip.
        assert regular[0] != daytime[0]
        assert regular[1] != daytime[1]


class TestTheOrderableSessionIsMeasured:
    def test_an_unrouted_session_yields_none(self, monkeypatch):
        from scanners.base import scan_session

        monkeypatch.setattr(scan_session, "session_at", lambda *a, **k: "PREMARKET")
        assert bootstrap._order_session() is None

    def test_a_routed_live_session_is_returned(self, monkeypatch):
        from scanners.base import scan_session

        for session in ("REGULAR", "OVERNIGHT_DAYTIME"):
            monkeypatch.setattr(scan_session, "session_at", lambda *a, _s=session, **k: _s)
            assert bootstrap._order_session() == session

    def test_a_session_s6_may_not_order_in_yields_none(self, monkeypatch):
        """Both conditions are required. AFTER_HOURS is a real session
        with real scanning and no order permission."""
        from scanners.base import scan_session

        monkeypatch.setattr(scan_session, "session_at", lambda *a, **k: "AFTER_HOURS")
        assert bootstrap._order_session() is None

    def test_it_never_falls_back_to_regular(self, monkeypatch):
        from scanners.base import scan_session

        monkeypatch.setattr(scan_session, "session_at", lambda *a, **k: None)
        assert bootstrap._order_session() is None


class TestEvidenceIsPerRoute:
    def test_the_daytime_and_regular_sets_are_separate(self):
        """Confirming one route says nothing about the other: different
        endpoints, different TR families, different hours."""
        from brokers import kis_broker as kb

        armed = set(kb.pending_items_for(kb.REQUIRED_FOR_ARMED))
        daytime = set(kb.pending_items_for(kb.REQUIRED_FOR_DAYTIME))
        assert not (armed & daytime)

    def test_the_session_decides_which_set_is_asked_about(self):
        assert bootstrap._route_awaiting_live_evidence("OVERNIGHT_DAYTIME") is True
        assert bootstrap._route_awaiting_live_evidence("REGULAR") is True
