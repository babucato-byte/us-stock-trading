"""The unattended S1 cycle: exits before entry, and no order without a fill.

The two properties worth breaking a build over:

  an entry block must never become an exit block -- otherwise the account
  is trapped in the position the block exists to escape;

  a submitted order must never become a local position -- otherwise a
  rejected or unfilled order leaves state claiming a holding that does
  not exist, and the exit policy starts measuring R from a price nobody
  paid.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s1_live import executor, exit_policy as ep, exit_runtime as er, position_store as ps  # noqa: E402
from state_store import db as sdb  # noqa: E402
import config.s1_exit_v0 as pol  # noqa: E402

NOW = datetime(2026, 8, 18, 14, 0, tzinfo=timezone.utc)
ENTRY = 28.37


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "s.db"))
    connection = sdb.open_db()
    sdb.init_db(connection)
    yield connection
    connection.close()


class Pos:
    def __init__(self, symbol, quantity, average_fill_price):
        self.symbol, self.quantity = symbol, quantity
        self.average_fill_price = average_fill_price


class FakeBroker:
    def __init__(self, positions=None, cash=30.99, price=ENTRY):
        self._positions = positions or []
        self._cash, self._price = cash, price
        self.submits = []

    def get_positions(self): return list(self._positions)
    def get_open_orders(self): return []
    def get_account_cash_usd(self): return self._cash
    def get_current_price(self, instrument): return self._price
    def submit_order(self, *a, **k):
        self.submits.append((a, k))
        raise AssertionError("real broker submit attempted")


class FakeAdapter:
    def __init__(self): self.calls = []
    def submit_order(self, symbol, qty=1, *, side, **kw):
        self.calls.append((symbol, qty, side))
        return type("R", (), {"status_code": 200, "text": "ok"})()


class Features:
    def __init__(self, price):
        self.price, self.hma200 = price, price * 0.85
        self.hma89, self.hma200_slope = price * 0.93, 1.0


def open_position(conn, symbol="TESTX", entry=ENTRY):
    return ps.open_position(conn, symbol=symbol, strategy_id=executor.STRATEGY_ID,
                            signal_id="sig", entry_price=entry, quantity=1)


def at_et(year, month, day, hour, minute=0):
    """A timezone-aware Eastern instant, so session detection is tested
    against the real clock rather than a patched state function."""
    import pytz

    return pytz.timezone("America/New_York").localize(
        datetime(year, month, day, hour, minute))


class TestSessionGating:
    """Tuesday 2026-08-18 is an ordinary trading day; Saturday the 22nd
    is not."""

    @pytest.mark.parametrize("hour,minute,expected", [
        (3, 0, "OVERNIGHT_DAYTIME"), (4, 0, "PREMARKET"), (9, 0, "PREMARKET"),
        (9, 30, "REGULAR"), (12, 0, "REGULAR"), (16, 0, "AFTER_HOURS"),
        (19, 0, "AFTER_HOURS"), (20, 0, "OVERNIGHT_DAYTIME"), (23, 0, "OVERNIGHT_DAYTIME"),
    ])
    def test_the_session_is_detected_from_eastern_time(self, hour, minute, expected):
        assert executor.resolve_session(at_et(2026, 8, 18, hour, minute)).name == expected

    def test_a_weekend_is_closed(self):
        assert executor.resolve_session(at_et(2026, 8, 22, 12, 0)).name == "CLOSED"

    def test_regular_still_permits_both_sides(self):
        """The behaviour production already has must not change."""
        session = executor.resolve_session(at_et(2026, 8, 18, 12, 0))
        assert session.entry_allowed is True
        assert session.exit_allowed is True
        assert session.route == "STANDARD_OVERSEAS_ORDER"
        assert session.verified is True

    @pytest.mark.parametrize("hour,name", [(6, "PREMARKET"), (17, "AFTER_HOURS")])
    def test_unverified_sessions_scan_but_never_order(self, hour, name):
        session = executor.resolve_session(at_et(2026, 8, 18, hour, 0))
        assert session.name == name
        assert session.scan_allowed is True
        assert session.entry_allowed is False
        assert session.exit_allowed is False
        assert session.verified is False

    def test_an_unreadable_clock_fails_closed(self, monkeypatch):
        import market_hours

        def boom(*a, **k): raise RuntimeError("clock unavailable")
        monkeypatch.setattr(market_hours, "eastern_now", boom)
        session = executor.resolve_session()
        assert session.name == "UNKNOWN"
        assert session.entry_allowed is False
        assert session.exit_allowed is False
        assert session.scan_allowed is False

    def test_entry_refuses_outright_in_a_closed_session(self, conn):
        from config import s1_session_policy as sp

        status, detail, results = executor.run_entry_half(
            conn, broker=FakeBroker(), session=sp.policy_for(sp.CLOSED))
        assert status == executor.STATUS_SESSION_CLOSED
        assert results == {}

    def test_entry_asks_about_entry_not_about_orders_generally(self, conn):
        """A session that allowed exits but not entries must not buy."""
        from config import s1_session_policy as sp

        exit_only = sp.SessionPolicy(
            session="EXIT_ONLY", scan_allowed=True, entry_allowed=False,
            exit_allowed=True, route=sp.ROUTE_STANDARD,
            order_type=sp.ORDER_TYPE_LIMIT, verified=True)
        status, _, results = executor.run_entry_half(
            conn, broker=FakeBroker(), session=exit_only)
        assert status == executor.STATUS_SESSION_CLOSED
        assert results == {}


class TestOrderAcceptedIsNotFilled:
    def test_no_broker_position_means_no_local_position(self, conn):
        recorded = executor.sync_fills(conn, FakeBroker(positions=[]),
                                       trading_day="2026-08-18")
        assert recorded == []
        assert ps.live_count(conn) == 0

    def test_a_confirmed_fill_uses_the_brokers_average_price(self, conn):
        broker = FakeBroker(positions=[Pos("NVDA", 1, 28.37)])
        recorded = executor.sync_fills(conn, broker, trading_day="2026-08-18")
        assert len(recorded) == 1
        assert recorded[0]["entry_price"] == 28.37
        assert recorded[0]["source"] == "BROKER_CONFIRMED_FILL"
        assert ps.load_state(conn, recorded[0]["position_id"]).entry_price == 28.37

    def test_a_position_without_a_usable_price_is_refused(self, conn):
        broker = FakeBroker(positions=[Pos("NVDA", 1, 0.0)])
        assert executor.sync_fills(conn, broker, trading_day="2026-08-18") == []
        assert ps.live_count(conn) == 0

    def test_syncing_twice_creates_one_position(self, conn):
        broker = FakeBroker(positions=[Pos("NVDA", 1, 28.37)])
        executor.sync_fills(conn, broker, trading_day="2026-08-18")
        again = executor.sync_fills(conn, broker, trading_day="2026-08-18")
        assert again == []
        assert ps.live_count(conn) == 1

    def test_a_broker_read_failure_records_nothing(self, conn):
        class Broken(FakeBroker):
            def get_positions(self): raise RuntimeError("read failed")

        assert executor.sync_fills(conn, Broken(), trading_day="2026-08-18") == []
        assert ps.live_count(conn) == 0


class TestExitsAreIndependentOfEntry:
    def test_the_exit_half_runs_without_consulting_any_entry_gate(self):
        import ast

        source = (REPO_ROOT / "s1_live" / "executor.py").read_text()
        tree = ast.parse(source)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "run_exit_half")
        body = ast.dump(fn)
        for token in ("run_live_buy_entry_cycle", "ENTRY_DISABLED", "entry_limits",
                      "risk_guards", "orders_allowed"):
            assert token not in body, token

    def test_a_stop_still_sells_when_the_session_blocks_entry(self, conn):
        """The session cannot take a BUY, but a triggered exit must still
        be latched rather than lost."""
        pid = open_position(conn)
        adapter = FakeAdapter()
        closed = er.SessionPolicy("AFTERMARKET", orders_allowed=False)
        stop_px = ENTRY * (1 + pol.HARD_STOP_PCT) - 0.01
        out = er.evaluate_position(
            conn, broker_adapter=adapter, position_id=pid,
            state=ps.load_state(conn, pid), row=ps.get_row(conn, pid),
            current_price=stop_px, features=Features(stop_px), session=closed)
        assert out.action == er.ACTION_LATCHED
        assert adapter.calls == []
        assert ps.get_row(conn, pid)["status"] == ps.STATUS_EXIT_PENDING

    def test_the_latched_exit_sells_once_in_the_next_orderable_session(self, conn):
        pid = open_position(conn)
        adapter = FakeAdapter()
        stop_px = ENTRY * (1 + pol.HARD_STOP_PCT) - 0.01
        er.evaluate_position(conn, broker_adapter=adapter, position_id=pid,
                             state=ps.load_state(conn, pid), row=ps.get_row(conn, pid),
                             current_price=stop_px, features=Features(stop_px),
                             session=er.SessionPolicy("AFTERMARKET", False))
        out = er.evaluate_position(conn, broker_adapter=adapter, position_id=pid,
                                   state=ps.load_state(conn, pid), row=ps.get_row(conn, pid),
                                   current_price=ENTRY * 1.1, features=Features(ENTRY * 1.1),
                                   session=er.SessionPolicy("REGULAR", True))
        assert out.action == er.ACTION_PENDING_RESUBMITTED
        assert len(adapter.calls) == 1

    def test_no_positions_means_no_exit_work_and_no_broker_calls(self, conn):
        adapter = FakeAdapter()
        outcomes = executor.run_exit_half(
            conn, broker=FakeBroker(), broker_adapter=adapter,
            session=er.SessionPolicy("REGULAR", True), trading_day="2026-08-18")
        assert outcomes == []
        assert adapter.calls == []


class TestRestartRecovery:
    def test_protective_floor_and_sessions_survive_a_restart(self, conn, tmp_path):
        pid = open_position(conn)
        ps.apply_ratchet(conn, pid, new_protective_floor_r=0.0, peak_r=1.2)
        ps.advance_session(conn, pid, "2026-08-18")
        conn.close()

        restarted = sdb.open_db()
        state = ps.load_state(restarted, pid)
        assert state.protective_floor_r == 0.0
        assert state.peak_r == pytest.approx(1.2)
        assert state.sessions_held == 1
        restarted.close()

    def test_a_sold_position_is_not_re_sold_after_a_restart(self, conn):
        pid = open_position(conn)
        adapter = FakeAdapter()
        stop_px = ENTRY * (1 + pol.HARD_STOP_PCT) - 0.01
        er.evaluate_position(conn, broker_adapter=adapter, position_id=pid,
                             state=ps.load_state(conn, pid), row=ps.get_row(conn, pid),
                             current_price=stop_px, features=Features(stop_px),
                             session=er.SessionPolicy("REGULAR", True))
        assert len(adapter.calls) == 1
        conn.close()

        restarted = sdb.open_db()
        er.evaluate_position(restarted, broker_adapter=adapter, position_id=pid,
                             state=ps.load_state(restarted, pid),
                             row=ps.get_row(restarted, pid),
                             current_price=stop_px * 0.5, features=Features(stop_px * 0.5),
                             session=er.SessionPolicy("REGULAR", True))
        assert len(adapter.calls) == 1, "sold twice across a restart"
        restarted.close()

    def test_a_synced_position_is_not_duplicated_after_a_restart(self, conn):
        broker = FakeBroker(positions=[Pos("NVDA", 1, 28.37)])
        executor.sync_fills(conn, broker, trading_day="2026-08-18")
        conn.close()

        restarted = sdb.open_db()
        assert executor.sync_fills(restarted, broker, trading_day="2026-08-18") == []
        assert ps.live_count(restarted) == 1
        restarted.close()


class TestCycleReporting:
    def test_a_cycle_reports_the_session_and_never_touches_the_real_broker(
            self, conn, monkeypatch):
        from config import s1_session_policy as sp

        monkeypatch.setattr(sp, "current_policy", lambda *a, **k: sp.policy_for(sp.CLOSED))
        broker = FakeBroker()
        report = executor.run_cycle(broker=broker, broker_adapter=FakeAdapter(),
                                    conn=conn, now=NOW)
        assert report.market_state == "CLOSED"
        assert report.session_orderable is False
        assert report.entry_status == executor.STATUS_SESSION_CLOSED
        assert report.submitted == []
        assert broker.submits == [], "the real submit surface was entered"

    def test_the_report_serialises_every_field_the_spec_asks_for(self, conn, monkeypatch):
        from config import s1_session_policy as sp

        monkeypatch.setattr(sp, "current_policy", lambda *a, **k: sp.policy_for(sp.CLOSED))
        report = executor.run_cycle(broker=FakeBroker(), broker_adapter=FakeAdapter(),
                                    conn=conn, now=NOW)
        payload = report.as_dict()
        for field in ("started_at", "trading_day", "market_state", "session_orderable",
                      "exits", "entry_status", "submitted", "blocked", "skipped",
                      "positions_synced", "account"):
            assert field in payload, field

    def test_the_distinct_no_trade_statuses_are_distinguishable(self):
        assert len({executor.STATUS_NO_CANDIDATE, executor.STATUS_NO_AFFORDABLE,
                    executor.STATUS_SESSION_CLOSED, executor.STATUS_ENTRY_BLOCKED,
                    executor.STATUS_SUBMITTED}) == 5


class TestStrategyIsolation:
    def test_the_executor_never_calls_the_scalping_manager(self):
        import ast

        source = (REPO_ROOT / "s1_live" / "executor.py").read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [getattr(node, "module", "") or ""] + [a.name for a in node.names]
                for name in names:
                    assert "kis_position_manager" not in str(name)
                    assert "lifecycle" not in str(name).split(".")[-1]

    def test_only_s1_is_named(self):
        assert executor.STRATEGY_ID == "hma_early_trend"
        source = (REPO_ROOT / "s1_live" / "executor.py").read_text()
        for other in ("accumulation", "breakout_ready", "gap_pullback",
                      "premarket_momentum"):
            assert other not in source, other

    def test_the_features_used_by_the_exit_stop_at_the_previous_session(self):
        """The exit's trend axis must not flicker intraday any more than
        the entry signal does."""
        import ast

        source = (REPO_ROOT / "s1_live" / "executor.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(source))
                  if isinstance(n, ast.FunctionDef) and n.name == "make_features_fn")
        body = ast.dump(fn)
        assert "_truncated_bundle" in body
        assert "signal_day" in body
        assert "want_premarket" in body
