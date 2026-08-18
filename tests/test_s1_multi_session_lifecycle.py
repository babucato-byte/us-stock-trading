"""A position carried across every session, with time injected.

Walks one holding through OVERNIGHT -> PREMARKET -> REGULAR ->
AFTER_HOURS -> CLOSED and asserts what each session is allowed to do with
it. The interesting cases are the transitions: an exit that triggers in a
session with no route must survive into the next one that has a route,
and must then sell exactly once -- not once per session it waited
through, and not again after a restart.

No broker is contacted. The adapter counts calls; a real submit raises.
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest
import pytz

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import s1_session_policy as sp  # noqa: E402
from s1_live import exit_policy as ep, exit_runtime as er, position_store as ps  # noqa: E402
from s1_live import executor, order_router as router  # noqa: E402
from state_store import db as sdb  # noqa: E402
import config.s1_exit_v0 as pol  # noqa: E402

EASTERN = pytz.timezone("America/New_York")
ENTRY = 28.37

#: One trading day, in the order the sessions actually occur.
SESSION_WALK = [
    ("OVERNIGHT (pre-dawn)", 2, 30, sp.OVERNIGHT_DAYTIME),
    ("PREMARKET", 6, 0, sp.PREMARKET),
    ("REGULAR", 11, 0, sp.REGULAR),
    ("AFTER_HOURS", 17, 0, sp.AFTER_HOURS),
    ("OVERNIGHT (evening)", 21, 0, sp.OVERNIGHT_DAYTIME),
]


def et(hour, minute=0, day=(2026, 8, 18)):
    return EASTERN.localize(datetime(day[0], day[1], day[2], hour, minute))


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "s.db"))
    connection = sdb.open_db()
    sdb.init_db(connection)
    yield connection
    connection.close()


class Adapter:
    def __init__(self):
        self.calls = []

    def submit_order(self, symbol, qty=1, *, side, **kw):
        self.calls.append((symbol, qty, side))
        return type("R", (), {"status_code": 200, "text": "ok"})()


class Features:
    def __init__(self, price):
        self.price, self.hma200 = price, price * 0.85
        self.hma89, self.hma200_slope = price * 0.93, 1.0


def tick(conn, adapter, pid, price, session):
    return er.evaluate_position(
        conn, broker_adapter=adapter, position_id=pid,
        state=ps.load_state(conn, pid), row=ps.get_row(conn, pid),
        current_price=price, features=Features(price), session=session)


def hold(conn, symbol="WALKX"):
    return ps.open_position(conn, symbol=symbol, strategy_id=executor.STRATEGY_ID,
                            signal_id="sig-walk", entry_price=ENTRY, quantity=1)


class TestTheWalk:
    def test_every_session_is_detected_in_order(self):
        for label, hour, minute, expected in SESSION_WALK:
            assert sp.current_session(et(hour, minute)) == expected, label

    def test_each_session_selects_the_route_it_should(self):
        expected = {
            sp.OVERNIGHT_DAYTIME: sp.ROUTE_DAYTIME,
            sp.REGULAR: sp.ROUTE_STANDARD,
        }
        for label, hour, minute, session in SESSION_WALK:
            if session in expected:
                assert router.route_for(session, "sell").route == expected[session], label
            else:
                with pytest.raises(router.OrderRouteUnavailable):
                    router.route_for(session, "sell")

    def test_a_healthy_position_is_held_through_all_of_them(self, conn):
        pid, adapter = hold(conn), Adapter()
        for label, hour, minute, session in SESSION_WALK:
            out = tick(conn, adapter, pid, ENTRY * 1.01, sp.policy_for(session))
            assert out.action == er.ACTION_HELD, f"{label}: {out.action}"
        assert adapter.calls == []


class TestExitPendingAcrossSessions:
    def test_an_exit_triggered_in_premarket_waits_for_regular(self, conn):
        pid, adapter = hold(conn), Adapter()
        stop = ENTRY * (1 + pol.HARD_STOP_PCT) - 0.01

        out = tick(conn, adapter, pid, stop, sp.policy_for(sp.PREMARKET))
        assert out.action == er.ACTION_LATCHED
        assert adapter.calls == []
        row = ps.get_row(conn, pid)
        assert row["status"] == ps.STATUS_EXIT_PENDING
        latched = row["pending_exit_reason"]
        assert latched == ep.REASON_HARD_STOP

        out = tick(conn, adapter, pid, ENTRY * 1.2, sp.policy_for(sp.REGULAR))
        assert out.action == er.ACTION_PENDING_RESUBMITTED
        assert out.reason == latched, "the original reason must be what is sold on"
        assert len(adapter.calls) == 1

    def test_waiting_through_several_sessions_still_sells_exactly_once(self, conn):
        pid, adapter = hold(conn), Adapter()
        stop = ENTRY * (1 + pol.HARD_STOP_PCT) - 0.01

        # Triggers overnight, then sits through premarket unable to order.
        tick(conn, adapter, pid, stop, sp.policy_for(sp.AFTER_HOURS))
        tick(conn, adapter, pid, stop, sp.policy_for(sp.CLOSED))
        tick(conn, adapter, pid, stop, sp.policy_for(sp.PREMARKET))
        assert adapter.calls == [], "no route, no order"

        tick(conn, adapter, pid, stop, sp.policy_for(sp.REGULAR))
        assert len(adapter.calls) == 1

        # And every later session leaves it alone.
        for _, hour, minute, session in SESSION_WALK:
            tick(conn, adapter, pid, stop * 0.5, sp.policy_for(session))
        assert len(adapter.calls) == 1, "sold more than once"

    def test_the_latch_survives_a_restart_between_sessions(self, conn):
        pid, adapter = hold(conn), Adapter()
        stop = ENTRY * (1 + pol.HARD_STOP_PCT) - 0.01
        tick(conn, adapter, pid, stop, sp.policy_for(sp.PREMARKET))
        latched = ps.get_row(conn, pid)["pending_exit_reason"]
        conn.close()

        restarted = sdb.open_db()
        assert ps.get_row(restarted, pid)["pending_exit_reason"] == latched
        out = tick(restarted, adapter, pid, ENTRY, sp.policy_for(sp.REGULAR))
        assert out.action == er.ACTION_PENDING_RESUBMITTED
        assert len(adapter.calls) == 1
        restarted.close()

    def test_a_recovery_between_sessions_does_not_cancel_the_exit(self, conn):
        pid, adapter = hold(conn), Adapter()
        stop = ENTRY * (1 + pol.HARD_STOP_PCT) - 0.01
        tick(conn, adapter, pid, stop, sp.policy_for(sp.AFTER_HOURS))
        # Rallies hard overnight -- the trigger already fired.
        tick(conn, adapter, pid, ENTRY * 1.5, sp.policy_for(sp.OVERNIGHT_DAYTIME))
        assert len(adapter.calls) == 1, "the latched exit is what executes"

    def test_an_overnight_trigger_can_sell_in_the_overnight_session(self, conn):
        """It has a route, so there is nothing to wait for."""
        pid, adapter = hold(conn), Adapter()
        stop = ENTRY * (1 + pol.HARD_STOP_PCT) - 0.01
        out = tick(conn, adapter, pid, stop, sp.policy_for(sp.OVERNIGHT_DAYTIME))
        assert out.action == er.ACTION_SOLD
        assert adapter.calls == [("WALKX", 1, "sell")]


class TestNoDuplicateAcrossSessionBoundaries:
    def test_a_sold_position_is_not_re_sold_in_the_next_session(self, conn):
        pid, adapter = hold(conn), Adapter()
        stop = ENTRY * (1 + pol.HARD_STOP_PCT) - 0.01
        tick(conn, adapter, pid, stop, sp.policy_for(sp.REGULAR))
        assert len(adapter.calls) == 1
        for _, hour, minute, session in SESSION_WALK:
            tick(conn, adapter, pid, stop, sp.policy_for(session))
        assert len(adapter.calls) == 1

    def test_one_position_per_symbol_across_every_session(self, conn):
        hold(conn, "WALKX")
        for _ in SESSION_WALK:
            with pytest.raises(ps.DuplicateS1PositionError):
                hold(conn, "WALKX")
        assert ps.live_count(conn) == 1

    def test_the_daily_entry_cap_is_not_per_session(self, conn):
        """Stage 1 allows one entry a day in TOTAL. Opening a second
        orderable session must not open a second allowance, so the cap is
        counted per trading day and the session never appears in it."""
        from execution import entry_limits

        source = (REPO_ROOT / "execution" / "entry_limits.py").read_text()
        for session_word in ("session", "SESSION", "market_state", "PREMARKET",
                             "OVERNIGHT"):
            assert session_word not in source, (
                f"entry limits reference {session_word!r} -- the cap must be "
                "per trading day, not per session")
        assert hasattr(entry_limits, "__file__")
        assert len(sp.ORDERABLE_SESSIONS) == 2, "two sessions, still one entry a day"


class TestEntryAcrossSessions:
    def test_entry_is_refused_in_every_unrouted_session(self, conn):
        broker = type("B", (), {
            "get_positions": lambda self: [], "get_open_orders": lambda self: [],
            "get_account_cash_usd": lambda self: 100.0})()
        for session in (sp.PREMARKET, sp.AFTER_HOURS, sp.CLOSED, sp.UNKNOWN):
            status, detail, results = executor.run_entry_half(
                conn, broker=broker, session=sp.policy_for(session))
            assert status == executor.STATUS_SESSION_CLOSED, session
            assert results == {}

    def test_the_two_routed_sessions_are_not_refused_for_session_reasons(self, conn):
        """They may still be blocked by a gate -- but not by the session."""
        for session in (sp.REGULAR, sp.OVERNIGHT_DAYTIME):
            policy = sp.policy_for(session)
            assert policy.entry_allowed is True, session
            assert router.can_enter(session) is True, session


class TestSimulationPlacesNoRealOrder:
    def test_the_whole_walk_touches_only_the_fake_adapter(self, conn):
        pid, adapter = hold(conn), Adapter()
        stop = ENTRY * (1 + pol.HARD_STOP_PCT) - 0.01
        for _, hour, minute, session in SESSION_WALK:
            tick(conn, adapter, pid, stop, sp.policy_for(session))
        # Exactly one sell, and it went to the fake.
        assert len(adapter.calls) == 1
        assert all(call[2] == "sell" for call in adapter.calls)
