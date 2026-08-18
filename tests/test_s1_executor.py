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


class TestSessionGating:
    def test_only_the_regular_session_is_orderable(self, monkeypatch):
        import market_hours

        for state, expected in (("REGULAR", True), ("PREMARKET", False),
                                ("AFTERMARKET", False), ("CLOSED", False)):
            monkeypatch.setattr(market_hours, "get_market_state", lambda s=state: s)
            session = executor.resolve_session()
            assert session.orders_allowed is expected, state
            assert session.name == state

    def test_an_unreadable_market_state_is_not_orderable(self, monkeypatch):
        import market_hours

        def boom(*a, **k): raise RuntimeError("clock unavailable")
        monkeypatch.setattr(market_hours, "get_market_state", boom)
        session = executor.resolve_session()
        assert session.orders_allowed is False
        assert session.verification == "MARKET_STATE_UNAVAILABLE"

    def test_an_unknown_state_name_is_not_orderable(self, monkeypatch):
        import market_hours

        monkeypatch.setattr(market_hours, "get_market_state", lambda *a, **k: "SOMETHING_NEW")
        assert executor.resolve_session().orders_allowed is False

    def test_entry_refuses_outright_in_a_closed_session(self, conn):
        closed = er.SessionPolicy("CLOSED", orders_allowed=False)
        status, detail, results = executor.run_entry_half(
            conn, broker=FakeBroker(), session=closed)
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
        import market_hours

        monkeypatch.setattr(market_hours, "get_market_state", lambda *a, **k: "CLOSED")
        broker = FakeBroker()
        report = executor.run_cycle(broker=broker, broker_adapter=FakeAdapter(),
                                    conn=conn, now=NOW)
        assert report.market_state == "CLOSED"
        assert report.session_orderable is False
        assert report.entry_status == executor.STATUS_SESSION_CLOSED
        assert report.submitted == []
        assert broker.submits == [], "the real submit surface was entered"

    def test_the_report_serialises_every_field_the_spec_asks_for(self, conn, monkeypatch):
        import market_hours

        monkeypatch.setattr(market_hours, "get_market_state", lambda *a, **k: "CLOSED")
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


class TestPriceFnReachesKISCorrectly:
    """A regression that only a held position would otherwise reveal.

    `build_kis_instrument()` returns (instrument, exchange_record). The
    executor passed the tuple straight to `get_current_price`, which fails
    with "'tuple' object has no attribute 'exchange'" -- but only on an
    exit tick, which needs a position, which no test had. It reached
    production undetected for exactly that reason.
    """

    def test_the_instrument_is_unpacked_before_the_broker_sees_it(self):
        seen = {}

        class RecordingBroker:
            def get_current_price(self, instrument):
                seen["instrument"] = instrument
                return 28.37

        price = executor.make_price_fn(RecordingBroker())("AAPL")
        assert price == 28.37
        assert not isinstance(seen["instrument"], tuple), "tuple passed to broker"
        assert hasattr(seen["instrument"], "exchange"), "no .exchange on instrument"
        assert seen["instrument"].symbol == "AAPL"

    def test_every_call_site_unpacks_the_pair(self):
        """The other eight call sites already did; this keeps them aligned."""
        import re

        for path in (REPO_ROOT / "s1_live").rglob("*.py"):
            for line in path.read_text().splitlines():
                if "build_kis_instrument(" in line and "def " not in line:
                    assert re.search(r",\s*_?\w*\s*=\s*build_kis_instrument\(|"
                                     r"build_kis_instrument\([^)]*\)\[0\]", line), \
                        f"{path.name}: {line.strip()}"

    def test_an_unusable_price_raises_rather_than_being_used(self):
        for bad in (None, 0, -1.0):
            class Bad:
                def get_current_price(self, instrument):
                    return bad

            with pytest.raises(ValueError):
                executor.make_price_fn(Bad())("AAPL")
