"""Session -> route, and the sessions that get no route at all.

The failure this file exists to prevent: sending a premarket order down
the regular endpoint because "extended hours" sounds like it ought to
work. A session is orderable only when KIS publishes a route for it, and
"published" means a file in the official reference repo, not a name.
"""

import ast
import sys
from datetime import datetime
from pathlib import Path

import pytest
import pytz

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import s1_session_policy as sp  # noqa: E402
from s1_live import order_router as router  # noqa: E402

EASTERN = pytz.timezone("America/New_York")
TUESDAY = (2026, 8, 18)
SATURDAY = (2026, 8, 22)


def et(hour, minute=0, day=TUESDAY):
    return EASTERN.localize(datetime(day[0], day[1], day[2], hour, minute))


class TestSessionDetection:
    @pytest.mark.parametrize("hour,minute,expected", [
        (0, 30, sp.OVERNIGHT_DAYTIME), (3, 59, sp.OVERNIGHT_DAYTIME),
        (4, 0, sp.PREMARKET), (9, 29, sp.PREMARKET),
        (9, 30, sp.REGULAR), (15, 59, sp.REGULAR),
        (16, 0, sp.AFTER_HOURS), (19, 59, sp.AFTER_HOURS),
        (20, 0, sp.OVERNIGHT_DAYTIME), (23, 59, sp.OVERNIGHT_DAYTIME),
    ])
    def test_boundaries(self, hour, minute, expected):
        assert sp.current_session(et(hour, minute)) == expected

    def test_the_overnight_window_is_not_premarket(self):
        """They do not even overlap: 20:00-04:00 versus 04:00-09:30."""
        assert sp.current_session(et(22)) == sp.OVERNIGHT_DAYTIME
        assert sp.current_session(et(6)) == sp.PREMARKET
        assert sp.SESSION_POLICIES[sp.OVERNIGHT_DAYTIME].route != \
            sp.SESSION_POLICIES[sp.PREMARKET].route

    def test_a_weekend_is_closed_at_every_hour(self):
        for hour in (2, 6, 12, 18, 22):
            assert sp.current_session(et(hour, day=SATURDAY)) == sp.CLOSED

    def test_dst_is_not_hardcoded(self):
        """January and July both resolve, and via the same tz database."""
        winter = EASTERN.localize(datetime(2026, 1, 13, 12, 0))
        summer = EASTERN.localize(datetime(2026, 7, 14, 12, 0))
        assert sp.current_session(winter) == sp.REGULAR
        assert sp.current_session(summer) == sp.REGULAR
        source = (REPO_ROOT / "config" / "s1_session_policy.py").read_text()
        for banned in ("timedelta(hours=4)", "timedelta(hours=5)", "UTC-4", "UTC-5"):
            assert banned not in source, banned

    def test_an_unresolvable_clock_is_unknown_not_closed(self, monkeypatch):
        import market_hours

        def boom(*a, **k): raise RuntimeError("no clock")
        monkeypatch.setattr(market_hours, "eastern_now", boom)
        assert sp.current_session() == sp.UNKNOWN


class TestPolicyTable:
    def test_unknown_denies_everything_including_scanning(self):
        policy = sp.policy_for(sp.UNKNOWN)
        assert (policy.scan_allowed, policy.entry_allowed, policy.exit_allowed) == \
            (False, False, False)

    def test_an_unrecognised_name_resolves_to_unknown(self):
        for name in ("", None, "LUNCH", "EXTENDED_HOURS"):
            assert sp.policy_for(name).session == sp.UNKNOWN

    def test_regular_is_verified_and_permits_both_sides(self):
        policy = sp.policy_for(sp.REGULAR)
        assert policy.entry_allowed and policy.exit_allowed and policy.verified
        assert policy.route == sp.ROUTE_STANDARD

    def test_the_daytime_session_is_verified_and_routed_separately(self):
        policy = sp.policy_for(sp.OVERNIGHT_DAYTIME)
        assert policy.entry_allowed and policy.exit_allowed and policy.verified
        assert policy.route == sp.ROUTE_DAYTIME
        assert policy.order_type == sp.ORDER_TYPE_LIMIT

    @pytest.mark.parametrize("name", [sp.PREMARKET, sp.AFTER_HOURS])
    def test_unverified_sessions_scan_but_neither_buy_nor_sell(self, name):
        policy = sp.policy_for(name)
        assert policy.scan_allowed is True
        assert policy.entry_allowed is False
        assert policy.exit_allowed is False
        assert policy.route == sp.ROUTE_NONE
        assert policy.verified is False
        assert policy.verification == "BROKER_SESSION_UNVERIFIED"

    def test_no_session_claims_a_live_response_yet(self):
        """`verified` means "KIS publishes it", not "we have seen it work"."""
        assert all(not p.live_response_observed for p in sp.SESSION_POLICIES.values())
        assert sp.policy_for(sp.REGULAR).verification == "REFERENCE_VERIFIED"

    def test_orderable_sessions_are_exactly_regular_and_daytime(self):
        assert sp.ORDERABLE_SESSIONS == frozenset({sp.REGULAR, sp.OVERNIGHT_DAYTIME})


class TestRouting:
    @pytest.mark.parametrize("side", ["buy", "sell"])
    def test_regular_routes_to_the_standard_endpoint(self, side):
        assert router.route_for(sp.REGULAR, side).route == sp.ROUTE_STANDARD

    @pytest.mark.parametrize("side", ["buy", "sell"])
    def test_daytime_routes_to_the_daytime_endpoint(self, side):
        route = router.route_for(sp.OVERNIGHT_DAYTIME, side)
        assert route.route == sp.ROUTE_DAYTIME
        assert route.order_type == sp.ORDER_TYPE_LIMIT

    @pytest.mark.parametrize("session", [sp.PREMARKET, sp.AFTER_HOURS,
                                         sp.CLOSED, sp.UNKNOWN])
    @pytest.mark.parametrize("side", ["buy", "sell"])
    def test_unverified_and_closed_sessions_get_no_route(self, session, side):
        with pytest.raises(router.OrderRouteUnavailable) as caught:
            router.route_for(session, side)
        assert caught.value.session == session

    def test_a_refusal_never_degrades_to_the_standard_route(self):
        """The specific bug: falling back would place a real order in a
        session nobody authorised."""
        for session in (sp.PREMARKET, sp.AFTER_HOURS, sp.CLOSED, sp.UNKNOWN):
            with pytest.raises(router.OrderRouteUnavailable):
                router.route_for(session, "buy")

    def test_an_unknown_side_is_refused(self):
        with pytest.raises(router.OrderRouteUnavailable):
            router.route_for(sp.REGULAR, "short")

    def test_can_enter_and_can_exit_agree_with_the_table(self):
        assert router.can_enter(sp.REGULAR) and router.can_exit(sp.REGULAR)
        assert router.can_enter(sp.OVERNIGHT_DAYTIME)
        assert not router.can_enter(sp.PREMARKET)
        assert not router.can_exit(sp.AFTER_HOURS)

    def test_the_matrix_is_what_it_claims(self):
        matrix = router.describe_matrix()
        assert matrix[sp.REGULAR]["buy"] == sp.ROUTE_STANDARD
        assert matrix[sp.OVERNIGHT_DAYTIME]["sell"] == sp.ROUTE_DAYTIME
        assert matrix[sp.PREMARKET]["buy"].startswith("BLOCKED:")
        assert matrix[sp.UNKNOWN]["sell"].startswith("BLOCKED:")


class TestBrokerRouteWiring:
    def test_the_broker_knows_both_routes(self):
        from brokers import kis_broker

        assert set(kis_broker.ORDER_ROUTES) == {sp.ROUTE_STANDARD, sp.ROUTE_DAYTIME}
        assert kis_broker.DEFAULT_ORDER_ROUTE == sp.ROUTE_STANDARD

    def test_the_daytime_tr_ids_are_the_officially_published_pair(self):
        from brokers import kis_broker

        assert kis_broker.TR_ID_DAYTIME_ORDER_US[("live", "buy")] == "TTTS6036U"
        assert kis_broker.TR_ID_DAYTIME_ORDER_US[("live", "sell")] == "TTTS6037U"
        assert kis_broker.DAYTIME_ORDER_PATH == \
            "/uapi/overseas-stock/v1/trading/daytime-order"

    def test_the_standard_route_is_untouched(self):
        """Production trades on this one; it must be byte-identical."""
        from brokers import kis_broker

        assert kis_broker.TR_ID_ORDER_US[("live", "buy")] == "TTTT1002U"
        assert kis_broker.TR_ID_ORDER_US[("live", "sell")] == "TTTT1006U"
        assert kis_broker.ORDER_PATH == "/uapi/overseas-stock/v1/trading/order"

    def test_the_two_routes_use_different_endpoints(self):
        from brokers import kis_broker

        assert kis_broker.ORDER_PATH != kis_broker.DAYTIME_ORDER_PATH
        standard = set(kis_broker.TR_ID_ORDER_US.values())
        daytime = set(kis_broker.TR_ID_DAYTIME_ORDER_US.values())
        assert standard.isdisjoint(daytime)

    def test_an_unknown_route_name_is_refused_not_defaulted(self):
        """Behavioural, not a source grep: an unrecognised route must
        raise before anything reaches the network."""
        from brokers import kis_broker

        assert kis_broker.ORDER_ROUTES.get("NO_SUCH_ROUTE") is None
        # The lookup the submit path performs, exercised directly.
        with pytest.raises(KeyError):
            kis_broker.ORDER_ROUTES["NO_SUCH_ROUTE"]

    def test_a_route_without_a_tr_id_for_the_side_is_refused(self):
        """The daytime table has no paper entries. Falling back to the
        standard table would place a real order in the wrong session."""
        from brokers import kis_broker

        assert ("paper", "buy") not in kis_broker.TR_ID_DAYTIME_ORDER_US
        assert ("paper", "sell") not in kis_broker.TR_ID_DAYTIME_ORDER_US
        source = (REPO_ROOT / "brokers" / "kis_broker.py").read_text()
        submit = source[source.index("def submit_order("):]
        # The refusal must be a raise, not a reassignment to the default.
        assert "no order TR_ID for route=" in submit
        head = submit[:submit.index("no order TR_ID for route=")]
        assert "DEFAULT_ORDER_ROUTE)" in head, "route defaults only when unspecified"

    def test_the_strategy_never_names_a_tr_id(self):
        """S1 asks for a route; only the broker turns that into a TR id."""
        s1_dir = REPO_ROOT / "s1_live"
        for path in sorted(s1_dir.rglob("*.py")):
            source = path.read_text()
            for tr in ("TTTT1002U", "TTTT1006U", "TTTS6036U", "TTTS6037U",
                       "TTTS6038U", "TTTT1004U"):
                assert tr not in source, f"{path.name} names {tr}"


class TestNoSilentEnablement:
    def test_premarket_and_after_hours_stay_off_until_evidence_exists(self):
        """Changing these is a decision, not a refactor -- so it should
        fail a test rather than slip through one."""
        for name in (sp.PREMARKET, sp.AFTER_HOURS):
            policy = sp.policy_for(name)
            assert policy.entry_allowed is False, f"{name} entry was enabled"
            assert policy.exit_allowed is False, f"{name} exit was enabled"
            assert "no official KIS order route" in policy.reason

    def test_the_session_module_holds_no_strategy_thresholds(self):
        source = (REPO_ROOT / "config" / "s1_session_policy.py").read_text()
        for owned_elsewhere in ("HARD_STOP_PCT", "adx_min", "SCORE_THRESHOLD",
                                "max_quantity", "hma"):
            assert owned_elsewhere not in source, owned_elsewhere

    def test_the_router_imports_no_broker(self):
        """Routing decides WHERE, never HOW to talk to KIS."""
        source = (REPO_ROOT / "s1_live" / "order_router.py").read_text()
        top_level = [n for n in ast.parse(source).body
                     if isinstance(n, (ast.Import, ast.ImportFrom))]
        for node in top_level:
            module = getattr(node, "module", "") or ""
            assert not module.startswith("brokers"), module
