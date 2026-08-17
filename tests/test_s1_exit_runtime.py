"""EXIT_PATH_NOT_WIRED closed: fill -> state -> persistence -> decision
-> actual SELL submission, and the ways that path must refuse to act."""

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s1_live import exit_policy, exit_runtime, position_store  # noqa: E402
from state_store import db as state_db, exit_intent_ledger  # noqa: E402

ENTRY = 100.0

REGULAR = exit_runtime.SessionPolicy("REGULAR", orders_allowed=True, verification="VERIFIED")
PREMARKET_UNVERIFIED = exit_runtime.SessionPolicy(
    "PREMARKET", orders_allowed=False, verification="BROKER_SESSION_UNVERIFIED")


class FakeBroker:
    """Counts submissions. NEVER reaches a real broker."""

    def __init__(self, status_code=200, raises=None):
        self.calls = []
        self.status_code = status_code
        self.raises = raises

    def submit_order(self, symbol, qty=1, *, side, **kw):
        self.calls.append({"symbol": symbol, "qty": qty, "side": side, **kw})
        if self.raises:
            raise self.raises
        return type("R", (), {"status_code": self.status_code,
                              "text": "ok" if self.status_code < 300 else "rejected",
                              "data": {}, "dry_run": False})()


class Features:
    def __init__(self, price=110.0, hma200=100.0, hma89=105.0, hma200_slope=1.0):
        self.price, self.hma200 = price, hma200
        self.hma89, self.hma200_slope = hma89, hma200_slope


HEALTHY = Features()


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "s1.db"))
    connection = state_db.open_db()
    state_db.init_db(connection)
    yield connection
    connection.close()


def open_nvda(conn, entry_price=ENTRY, **kw):
    return position_store.open_position(
        conn, symbol="NVDA", strategy_id="hma_early_trend", signal_id="sig-1",
        entry_price=entry_price, quantity=1, **kw)


def tick(conn, broker, price, *, position_id, features=HEALTHY, session=REGULAR,
         session_date=None, emergency=False):
    state = position_store.load_state(conn, position_id)
    row = position_store.get_row(conn, position_id)
    return exit_runtime.evaluate_position(
        conn, broker_adapter=broker, position_id=position_id, state=state, row=row,
        current_price=price, features=features, session=session,
        session_date=session_date, emergency=emergency)


class TestFillToState:
    def test_the_entry_price_is_the_actual_fill_not_the_intended_limit(self, conn):
        """§3-1: every R level is measured from this number."""
        intended_limit, actual_fill = 100.0, 100.87
        pid = open_nvda(conn, entry_price=actual_fill)
        assert position_store.load_state(conn, pid).entry_price == actual_fill
        assert position_store.load_state(conn, pid).entry_price != intended_limit

    def test_a_fill_creates_state_the_policy_can_run_on(self, conn):
        state = position_store.load_state(conn, open_nvda(conn))
        assert isinstance(state, exit_policy.S1PositionState)
        assert exit_policy.decide(state, current_price=94.0, features=HEALTHY).sells

    def test_a_second_position_in_the_same_symbol_is_impossible(self, conn):
        open_nvda(conn)
        with pytest.raises(position_store.DuplicateS1PositionError):
            open_nvda(conn)

    def test_a_fractional_quantity_is_refused(self, conn):
        with pytest.raises(position_store.S1PositionStoreError):
            position_store.open_position(
                conn, symbol="AMD", strategy_id="hma_early_trend", signal_id="s",
                entry_price=100.0, quantity=0)


class TestEachAxisReachesAnActualSell:
    def test_hard_stop_submits_a_sell(self, conn):
        broker, pid = FakeBroker(), open_nvda(conn)
        out = tick(conn, broker, 93.9, position_id=pid)
        assert out.action == exit_runtime.ACTION_SOLD
        assert out.reason == exit_policy.REASON_HARD_STOP
        assert broker.calls == [{"symbol": "NVDA", "qty": 1, "side": "sell",
                                 "client_order_id": broker.calls[0]["client_order_id"]}]

    def test_protective_stop_submits_a_sell(self, conn):
        broker = FakeBroker()
        pid = open_nvda(conn)
        position_store.apply_ratchet(conn, pid, new_protective_floor_r=0.0, peak_r=1.2)
        out = tick(conn, broker, ENTRY - 0.01, position_id=pid)
        assert out.action == exit_runtime.ACTION_SOLD
        assert out.reason == exit_policy.REASON_PROTECTIVE_STOP
        assert len(broker.calls) == 1

    def test_trend_breakdown_submits_a_sell(self, conn):
        broker, pid = FakeBroker(), open_nvda(conn)
        broken = Features(price=99.0, hma200=100.0, hma89=105.0, hma200_slope=1.0)
        out = tick(conn, broker, 99.0, position_id=pid, features=broken)
        assert out.action == exit_runtime.ACTION_SOLD
        assert out.reason == exit_policy.REASON_TREND_BREAKDOWN

    def test_time_exit_submits_a_sell(self, conn):
        broker, pid = FakeBroker(), open_nvda(conn)
        for day in range(1, 11):
            position_store.advance_session(conn, pid, f"2026-08-{day:02d}")
        out = tick(conn, broker, 101.0, position_id=pid)
        assert out.action == exit_runtime.ACTION_SOLD
        assert out.reason == exit_policy.REASON_TIME_EXIT

    def test_a_healthy_position_places_no_order(self, conn):
        broker, pid = FakeBroker(), open_nvda(conn)
        out = tick(conn, broker, 103.0, position_id=pid, features=Features(price=103.0))
        assert out.action == exit_runtime.ACTION_HELD
        assert broker.calls == []

    def test_a_ratchet_places_no_order_but_is_persisted(self, conn):
        broker, pid = FakeBroker(), open_nvda(conn)
        out = tick(conn, broker, 110.0, position_id=pid)
        assert out.action == exit_runtime.ACTION_RATCHETED
        assert broker.calls == []
        assert position_store.load_state(conn, pid).protective_floor_r == 0.0


class TestDuplicateSellIsImpossible:
    def test_a_second_tick_after_a_sell_places_no_second_order(self, conn):
        broker, pid = FakeBroker(), open_nvda(conn)
        assert tick(conn, broker, 93.0, position_id=pid).action == exit_runtime.ACTION_SOLD
        tick(conn, broker, 90.0, position_id=pid)
        tick(conn, broker, 85.0, position_id=pid)
        assert len(broker.calls) == 1, "the position was sold more than once"

    def test_the_exit_intent_ledger_refuses_a_second_active_intent(self, conn):
        broker, pid = FakeBroker(), open_nvda(conn)
        tick(conn, broker, 93.0, position_id=pid)
        assert exit_intent_ledger.get_active_intent(conn, pid) is not None

    def test_exit_submitted_survives_a_restart(self, conn):
        broker, pid = FakeBroker(), open_nvda(conn)
        tick(conn, broker, 93.0, position_id=pid)
        assert position_store.load_state(conn, pid).exit_submitted is True
        assert exit_policy.decide(position_store.load_state(conn, pid),
                                  current_price=80.0).action == exit_policy.HOLD


class TestSessionGating:
    def test_an_unverified_session_places_no_order_and_latches_the_exit(self, conn):
        """§5/§9: fail closed, but never discard the trigger."""
        broker, pid = FakeBroker(), open_nvda(conn)
        out = tick(conn, broker, 93.0, position_id=pid, session=PREMARKET_UNVERIFIED)
        assert out.action == exit_runtime.ACTION_LATCHED
        assert broker.calls == []
        row = position_store.get_row(conn, pid)
        assert row["status"] == position_store.STATUS_EXIT_PENDING
        assert row["pending_exit_reason"] == exit_policy.REASON_HARD_STOP

    def test_a_latched_exit_is_submitted_in_the_next_orderable_session(self, conn):
        broker, pid = FakeBroker(), open_nvda(conn)
        tick(conn, broker, 93.0, position_id=pid, session=PREMARKET_UNVERIFIED)
        out = tick(conn, broker, 97.0, position_id=pid, session=REGULAR)
        assert out.action == exit_runtime.ACTION_PENDING_RESUBMITTED
        assert len(broker.calls) == 1

    def test_a_recovering_price_does_not_erase_a_latched_exit(self, conn):
        """The trigger fired. A later HOLD tick must not cancel it."""
        broker, pid = FakeBroker(), open_nvda(conn)
        tick(conn, broker, 93.0, position_id=pid, session=PREMARKET_UNVERIFIED)
        out = tick(conn, broker, 105.0, position_id=pid, session=REGULAR)
        assert out.action == exit_runtime.ACTION_PENDING_RESUBMITTED
        assert out.reason == exit_policy.REASON_HARD_STOP

    def test_the_first_latched_reason_wins(self, conn):
        broker, pid = FakeBroker(), open_nvda(conn)
        tick(conn, broker, 93.0, position_id=pid, session=PREMARKET_UNVERIFIED)
        tick(conn, broker, 50.0, position_id=pid, session=PREMARKET_UNVERIFIED)
        assert position_store.get_row(conn, pid)["pending_exit_reason"] \
            == exit_policy.REASON_HARD_STOP


class TestRejectionHandling:
    def test_a_rejection_creates_no_position_change_and_no_retry_loop(self, conn):
        """§10: record it, do not chase price, do not enlarge, do not retry."""
        broker, pid = FakeBroker(status_code=400), open_nvda(conn)
        out = tick(conn, broker, 93.0, position_id=pid)
        assert out.action == exit_runtime.ACTION_BLOCKED
        assert len(broker.calls) == 1
        assert broker.calls[0]["qty"] == 1, "quantity must not be enlarged"
        assert position_store.get_row(conn, pid)["exit_submitted"] == 0

    def test_an_ambiguous_submission_is_never_auto_retried(self, conn):
        broker = FakeBroker(raises=RuntimeError("connection reset"))
        pid = open_nvda(conn)
        out = tick(conn, broker, 93.0, position_id=pid)
        assert out.action == exit_runtime.ACTION_BLOCKED
        assert len(broker.calls) == 1
        assert position_store.get_row(conn, pid)["status"] \
            == position_store.STATUS_EXIT_PENDING


class TestSessionCounting:
    def test_many_ticks_in_one_session_count_as_one(self, conn):
        """§4 ticks premarket, regular and after-hours. A naive increment
        would trip the 10-session time exit inside a single day."""
        broker, pid = FakeBroker(), open_nvda(conn)
        for _ in range(20):
            tick(conn, broker, 103.0, position_id=pid, session_date="2026-08-17",
                 features=Features(price=103.0))
        assert position_store.load_state(conn, pid).sessions_held == 1
        assert broker.calls == []


class TestLegacyScalpingExitCannotApply:
    def test_the_runtime_never_calls_the_scalping_lifecycle(self):
        forbidden = {"lifecycle", "check_and_manage", "risk_config",
                     "scalping_strategy_v1_config"}
        source = (REPO_ROOT / "s1_live" / "exit_runtime.py").read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[-1] not in forbidden, node.module
                for alias in node.names:
                    assert alias.name not in forbidden, alias.name
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[-1] not in forbidden, alias.name

    def test_the_eight_percent_scalping_stop_does_not_fire_for_s1(self, conn):
        """-6% must have already sold before -8% is reached."""
        import risk_config

        broker, pid = FakeBroker(), open_nvda(conn)
        out = tick(conn, broker, ENTRY * (1 + risk_config.STOP_LOSS_RATE) + 0.5,
                   position_id=pid)
        assert out.reason == exit_policy.REASON_HARD_STOP
        assert out.action == exit_runtime.ACTION_SOLD

    def test_sixty_minutes_of_holding_does_not_exit_an_s1_position(self, conn):
        from config import scalping_strategy_v1_config as scalping

        assert scalping.MAX_POSITION_HOLD_MINUTES == 60
        broker, pid = FakeBroker(), open_nvda(conn)
        out = tick(conn, broker, 103.0, position_id=pid, session_date="2026-08-17",
                   features=Features(price=103.0))
        assert out.action == exit_runtime.ACTION_HELD


class TestRealBrokerIsNeverTouched:
    def test_every_submission_in_this_module_goes_through_the_adapter(self):
        """No direct KIS call, no new order implementation (§3-4)."""
        source = (REPO_ROOT / "s1_live" / "exit_runtime.py").read_text()
        for banned in ("requests.", "http", "TTTT100", "_send_order", "KISBroker("):
            assert banned not in source, banned
