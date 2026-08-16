"""CODEX-057 and CODEX-058.

CODEX-057 -- every durable READ is normalized, and a read FAILURE is
never reported as "no such order". order_repository.load() and
load_events() let raw sqlite3 errors escape, and submit_cancel() could
not tell "this order does not exist" (a policy block) apart from "the
database could not be read" (a system fault), so it ended the run as
SHADOW_BLOCKED either way.

CODEX-058 -- a connection whose rollback AND close both failed may still
hold SQLite's write lock. Python cannot conclude otherwise from a thrown
close(), so the process fail-stops and lets the OS reclaim the
descriptor.
"""
import os
import sqlite3
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

import shadow_audit
from domain.execution_event import ExecutionRecord
from domain.instrument import build_instrument
from domain.order_intent import OrderIntent
from domain.signal import build_signal
from execution import execution_engine, idempotency, order_gate, order_repository
from execution.execution_engine import ExecutionEngineError
from execution.order_repository import (
    FatalRepositoryConnectionError,
    OrderRepositoryConnectionInvalidatedError,
    OrderRepositoryReadError,
)
from state_store import db as state_db
import entry_limit_fixtures

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
ACCOUNT_ID = "12345678"
TERMINALS = ("SHADOW_COMPLETED", "SHADOW_BLOCKED", "SHADOW_ERROR")

FAKE_ACCOUNT = "70707070"
FAKE_SECRET = "PLANTEDAPPSECRET7777"
FAKE_TOKEN = "PLANTEDACCESSTOKEN7777"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setenv("OPERATIONS_HALT_STATE_FILE", str(tmp_path / "OPS_HALT.json"))
    monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path / "KILL_SWITCH.json"))
    monkeypatch.setenv("SHADOW_MODE_LOG_FILE", str(tmp_path / "SHADOW.jsonl"))
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "KIS_ORDER_IDEMPOTENCY.lock")
    state_db.open_db().close()
    yield


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    from operations import alerts as ops_alerts

    monkeypatch.setattr(ops_alerts, "send_alert", lambda message: sent.append(message) or True)
    return sent


def _instrument():
    return build_instrument("AAPL", exchange="NASDAQ")


def _order_intent(**overrides):
    kwargs = dict(
        internal_order_id="ord-1", signal_id="sig-1", strategy_id="strat-1", symbol="AAPL",
        exchange="NASDAQ", side="buy", quantity=1, order_type="limit", limit_price=100.0,
        stop_price=95.0, target_price=110.0, created_at=NOW,
    )
    kwargs.update(overrides)
    return OrderIntent(**kwargs)


def _buy_ctx_builder(order_intent):
    def _build(reconciliation):
        return order_gate.BuyGateContext(
            execution_broker="kis", live_order_enabled=True, entry_disabled=False,
            validated_commit="c1", deployed_commit="c1", kis_account_no=ACCOUNT_ID,
            allowed_account_no=ACCOUNT_ID, order_intent=order_intent, instrument=_instrument(),
            signal=build_signal(
                strategy_id="strat-1", strategy_version="v1", config_version="cfg-1",
                code_commit="abc", symbol="AAPL", exchange="NASDAQ", signal_price=100.0,
                score=90.0, entry_reason="breakout", valid_for_seconds=300, now=NOW,
            ),
            is_regular_session=True, kis_price_usd=100.1, max_price_deviation_percent=0.30,
            usd_orderable_cash=1000.0, has_open_order_for_symbol=False,
            has_order_for_signal_id=False, allowed_symbols=frozenset({"AAPL"}),
            reconciliation=reconciliation, entry_limits=entry_limit_fixtures.unlimited(), now=NOW,
        )
    return _build


class _Broker:
    def __init__(self):
        self.open_orders = []
        self.cancel_calls = 0

    def get_positions(self):
        return []

    def get_open_orders(self):
        return self.open_orders

    def get_fills(self, *, start_date, end_date):
        return []

    def submit_order(self, order_intent, instrument, *, authorization=None):
        self.open_orders.append({"ODNO": "kis-1"})
        return ExecutionRecord(
            internal_order_id=order_intent.internal_order_id, broker="kis",
            broker_order_id="kis-1", requested_quantity=order_intent.quantity,
            requested_price=order_intent.limit_price, filled_quantity=0.0,
            average_fill_price=None, status="ACCEPTED", submitted_at=NOW, updated_at=NOW,
        )

    def cancel_order(self, order_intent, instrument, broker_order_id, *, authorization=None):
        self.cancel_calls += 1
        return ExecutionRecord(
            internal_order_id=order_intent.internal_order_id, broker="kis",
            broker_order_id=broker_order_id, requested_quantity=order_intent.quantity,
            requested_price=order_intent.limit_price, filled_quantity=0.0,
            average_fill_price=None, status="CANCELLED", submitted_at=NOW, updated_at=NOW,
        )


def _place_order(conn, broker):
    oi = _order_intent()
    execution_engine.submit_buy_order(
        order_intent=oi, buy_gate_context_builder=_buy_ctx_builder(oi), conn=conn,
        broker=broker, instrument=_instrument(), account_id=ACCOUNT_ID,
        audit_run_id=shadow_audit.new_run_id(), now=NOW,
    )
    return oi


def _events(run_id):
    return [row["event_type"] for row in shadow_audit.read_events(shadow_run_id=run_id)]


def _seed(conn, order_id="v"):
    idempotency.register(
        conn, internal_order_id=order_id, signal_id=f"sig-{order_id}", symbol="AAPL",
        side="buy", trading_date="2026-07-29", requested_quantity=1,
    )
    return order_repository.load(conn, order_id)


# The planted values go into the SQLite error text, which is exactly how a
# real driver error can carry the failing statement and its parameters.
LEAKY_MESSAGE = (
    "near \"SELECT * FROM kis_order_idempotency WHERE cano = '{account}' "
    "AND appsecret = '{secret}' AND token = '{token}'\": syntax error"
).format(account=FAKE_ACCOUNT, secret=FAKE_SECRET, token=FAKE_TOKEN)

SQLITE_FAILURES = [
    pytest.param(sqlite3.OperationalError, id="OperationalError"),
    pytest.param(sqlite3.IntegrityError, id="IntegrityError"),
    pytest.param(sqlite3.DatabaseError, id="DatabaseError"),
    pytest.param(sqlite3.Error, id="Error"),
]


class _ReadFails:
    """Fails one specific SELECT and lets everything else through."""

    def __init__(self, conn, fragment, failure):
        self._conn = conn
        self._fragment = fragment
        self._failure = failure

    def execute(self, sql, *args, **kwargs):
        if self._fragment in sql:
            raise self._failure
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


READ_APIS = [
    ("load", lambda conn: order_repository.load(conn, "v"),
     "SELECT internal_order_id, status"),
    ("load_events", lambda conn: order_repository.load_events(conn, "v"),
     "SELECT from_state"),
    ("find_existing", lambda conn: idempotency.find_existing(
        conn, internal_order_id="v", signal_id="sig-v", symbol="AAPL", side="buy",
        trading_date="2026-07-29"),
     "SELECT * FROM kis_order_idempotency"),
    ("has_unknown_order", lambda conn: idempotency.has_unknown_order(conn),
     "SELECT 1 FROM kis_order_idempotency"),
    ("list_unknown_orders", lambda conn: idempotency.list_unknown_orders(conn),
     "WHERE status = 'UNKNOWN'"),
    ("list_orders_by_status", lambda conn: idempotency.list_orders_by_status(conn, ("ACCEPTED",)),
     "WHERE status IN"),
    ("list_orders_with_broker_id", lambda conn: idempotency.list_orders_with_broker_id(conn),
     "WHERE broker_order_id IS NOT NULL"),
]


class TestEveryReadIsNormalized:
    @pytest.mark.parametrize("name,call,fragment", READ_APIS,
                             ids=[api[0] for api in READ_APIS])
    @pytest.mark.parametrize("failure_cls", SQLITE_FAILURES)
    def test_raw_sqlite_never_escapes(self, name, call, fragment, failure_cls):
        conn = state_db.open_db()
        _seed(conn)
        failing = _ReadFails(conn, fragment, failure_cls(LEAKY_MESSAGE))

        with pytest.raises(OrderRepositoryReadError) as excinfo:
            call(failing)

        assert not isinstance(excinfo.value, sqlite3.Error)
        assert isinstance(excinfo.value.__cause__, sqlite3.Error)

    @pytest.mark.parametrize("name,call,fragment", READ_APIS,
                             ids=[api[0] for api in READ_APIS])
    def test_no_sql_or_secret_reaches_the_message(self, name, call, fragment):
        conn = state_db.open_db()
        _seed(conn)
        failing = _ReadFails(conn, fragment, sqlite3.OperationalError(LEAKY_MESSAGE))

        with pytest.raises(OrderRepositoryReadError) as excinfo:
            call(failing)

        message = str(excinfo.value)
        assert FAKE_ACCOUNT not in message
        assert FAKE_SECRET not in message
        assert FAKE_TOKEN not in message
        assert "SELECT" not in message
        assert "kis_order_idempotency" not in message

    def test_a_control_read_still_works(self):
        conn = state_db.open_db()
        _seed(conn)
        assert order_repository.load(conn, "v").state == "CREATED"
        assert [e["to_state"] for e in order_repository.load_events(conn, "v")] == ["CREATED"]

    def test_missing_order_is_none_not_an_error(self):
        conn = state_db.open_db()
        assert order_repository.load(conn, "does-not-exist") is None

    def test_empty_history_is_an_empty_list_not_an_error(self):
        conn = state_db.open_db()
        assert order_repository.load_events(conn, "does-not-exist") == []


class TestCancelReadFailureIsASystemFault:
    """A database fault must not be reported as a policy block."""

    def _run(self, failure):
        conn = state_db.open_db()
        broker = _Broker()
        _place_order(conn, broker)
        run_id = shadow_audit.new_run_id()
        failing = _ReadFails(conn, "SELECT internal_order_id, status", failure)
        with pytest.raises(ExecutionEngineError) as excinfo:
            execution_engine.submit_cancel(
                order_intent=_order_intent(), broker_order_id="kis-1",
                cancel_gate_context_builder=lambda: order_gate.CancelGateContext(
                    execution_broker="kis", broker_order_id="kis-1", is_actually_open=True,
                    kis_account_no=ACCOUNT_ID, allowed_account_no=ACCOUNT_ID, symbol="AAPL",
                    has_cancel_already_in_flight=False),
                conn=failing, broker=broker, instrument=_instrument(),
                audit_run_id=run_id, now=NOW,
            )
        return broker, run_id, excinfo.value

    def test_load_failure_ends_the_run_as_an_error_not_a_block(self, alerts):
        broker, run_id, exc = self._run(sqlite3.OperationalError(LEAKY_MESSAGE))

        assert broker.cancel_calls == 0, "the transport must not run"
        types = _events(run_id)
        assert "SHADOW_ERROR" in types
        assert "SHADOW_BLOCKED" not in types
        assert sum(1 for t in types if t in TERMINALS) == 1
        assert exc.reason_code == "STATE_READ_FAILURE"
        assert alerts, "no operator alert for a durable-read failure"

    def test_the_alert_says_this_is_a_database_fault(self, alerts):
        self._run(sqlite3.OperationalError("io"))
        joined = "\n".join(alerts).lower()
        assert "not a policy block" in joined
        assert "reconciliation" in joined

    def test_the_alert_carries_no_sql_or_secret(self, alerts):
        self._run(sqlite3.OperationalError(LEAKY_MESSAGE))
        joined = "\n".join(alerts)
        assert FAKE_ACCOUNT not in joined
        assert FAKE_SECRET not in joined
        assert FAKE_TOKEN not in joined
        assert "SELECT" not in joined

    def test_a_genuinely_missing_order_is_still_a_block(self, alerts):
        """The other half: not-found is a decision, and stays BLOCKED."""
        conn = state_db.open_db()
        broker = _Broker()
        run_id = shadow_audit.new_run_id()
        with pytest.raises(ExecutionEngineError, match="no durable order record"):
            execution_engine.submit_cancel(
                order_intent=_order_intent(), broker_order_id="kis-1",
                cancel_gate_context_builder=lambda: order_gate.CancelGateContext(
                    execution_broker="kis", broker_order_id="kis-1", is_actually_open=True,
                    kis_account_no=ACCOUNT_ID, allowed_account_no=ACCOUNT_ID, symbol="AAPL",
                    has_cancel_already_in_flight=False),
                conn=conn, broker=broker, instrument=_instrument(),
                audit_run_id=run_id, now=NOW,
            )
        types = _events(run_id)
        assert "SHADOW_BLOCKED" in types
        assert "SHADOW_ERROR" not in types


# ---------------------------------------------------------------------------
# CODEX-058
# ---------------------------------------------------------------------------

class _CommitFails:
    def __init__(self, conn, fail_rollback=False, fail_close=False):
        self._conn = conn
        self._fail_rollback = fail_rollback
        self._fail_close = fail_close
        self.close_calls = 0

    def commit(self):
        raise sqlite3.OperationalError("simulated commit failure")

    def rollback(self):
        if self._fail_rollback:
            raise sqlite3.OperationalError("simulated rollback failure")
        return self._conn.rollback()

    def close(self):
        self.close_calls += 1
        if self._fail_close:
            raise sqlite3.OperationalError("simulated close failure")
        return self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TestFatalConnectionFault:
    def _poison(self, db_path):
        conn = state_db.open_db(db_path)
        record = _seed(conn)
        proxy = _CommitFails(conn, fail_rollback=True, fail_close=True)
        with pytest.raises(FatalRepositoryConnectionError) as excinfo:
            order_repository.advance(proxy, record, "VALIDATING", event_type="T", now=NOW)
        return conn, proxy, excinfo.value

    def test_rollback_and_close_both_failing_is_fatal(self, tmp_path, alerts):
        db_path = str(tmp_path / "FATAL.db")
        conn, proxy, exc = self._poison(db_path)

        assert not isinstance(exc, sqlite3.Error)
        assert proxy.close_calls == 1
        joined = "\n".join(alerts).lower()
        assert "critical" in joined
        assert "must restart" in joined
        conn.rollback()
        conn.close()

    def test_halt_is_set(self, tmp_path, alerts):
        from operations import kill_switch

        assert kill_switch.is_halted() is False
        conn, _proxy, _exc = self._poison(str(tmp_path / "HALT.db"))
        assert kill_switch.is_halted() is True, "HALT was not set on a fatal DB fault"
        conn.rollback()
        conn.close()

    @pytest.mark.parametrize("operation", [
        ("load", lambda conn: order_repository.load(conn, "v")),
        ("load_events", lambda conn: order_repository.load_events(conn, "v")),
        ("compare_and_set_state", lambda conn: order_repository.compare_and_set_state(
            conn, order_id="v", expected_state="CREATED", next_state="VALIDATING",
            event_type="T", expected_version=0)),
        ("find_existing", lambda conn: idempotency.find_existing(
            conn, internal_order_id="v", signal_id="sig-v", symbol="AAPL", side="buy",
            trading_date="2026-07-29")),
        ("list_unknown_orders", lambda conn: idempotency.list_unknown_orders(conn)),
    ], ids=lambda p: p[0] if isinstance(p, tuple) else str(p))
    def test_the_invalidated_connection_cannot_be_reused(self, tmp_path, alerts, operation):
        name, call = operation
        db_path = str(tmp_path / f"REUSE_{name}.db")
        conn, proxy, _exc = self._poison(db_path)

        # The PROXY is what was invalidated; every repository entry point
        # must refuse it before executing any SQL.
        with pytest.raises(OrderRepositoryConnectionInvalidatedError):
            call(proxy)
        conn.rollback()
        conn.close()

    def test_advance_also_refuses_the_invalidated_connection(self, tmp_path, alerts):
        db_path = str(tmp_path / "REUSE_ADVANCE.db")
        conn, proxy, _exc = self._poison(db_path)
        record = order_repository.load(conn, "v")
        with pytest.raises(OrderRepositoryConnectionInvalidatedError):
            order_repository.advance(proxy, record, "VALIDATING", event_type="T", now=NOW)
        conn.rollback()
        conn.close()

    def test_a_fresh_connection_still_works(self, tmp_path, alerts):
        db_path = str(tmp_path / "FRESH.db")
        conn, _proxy, _exc = self._poison(db_path)
        conn.rollback()
        conn.close()

        fresh = state_db.open_db(db_path)
        try:
            updated = order_repository.advance(
                fresh, order_repository.load(fresh, "v"), "VALIDATING", event_type="T", now=NOW,
            )
        finally:
            fresh.close()
        assert updated.state == "VALIDATING"


# The child process holds a REAL write transaction it cannot roll back or
# close, then exits non-zero -- the fail-stop this finding requires.
_CHILD = textwrap.dedent(
    """
    import os, sqlite3, sys
    sys.path.insert(0, sys.argv[2])
    os.environ["STATE_STORE_DB_FILE"] = sys.argv[1]
    os.environ["OPERATIONS_HALT_STATE_FILE"] = sys.argv[3]
    os.environ["KILL_SWITCH_STATE_FILE"] = sys.argv[4]
    # A fatal repository fault sets HALT, which now emits a Slack
    # notification, and notification_health persists every send outcome.
    # monkeypatch cannot reach a child process, so without these the
    # child drops NOTIFICATION_HEALTH_STATE.json / notification_health.log
    # into the repository root.
    os.environ["NOTIFICATION_HEALTH_STATE_FILE"] = sys.argv[3] + ".nh.json"
    os.environ["NOTIFICATION_HEALTH_LOG_FILE"] = sys.argv[3] + ".nh.log"
    from execution import idempotency, order_repository
    from state_store import db as state_db

    idempotency._LOCK_FILE = __import__("pathlib").Path(sys.argv[3]).parent / "lock"
    conn = state_db.open_db(sys.argv[1])
    idempotency.register(conn, internal_order_id="p", signal_id="sp", symbol="AAPL",
                         side="buy", trading_date="2026-07-29", requested_quantity=1)

    class Poison:
        def __init__(self, c):
            self._c = c
        def commit(self):
            raise sqlite3.OperationalError("commit failed")
        def rollback(self):
            raise sqlite3.OperationalError("rollback failed")
        def close(self):
            raise sqlite3.OperationalError("close failed")
        def __getattr__(self, n):
            return getattr(self._c, n)

    try:
        order_repository.advance(Poison(conn), order_repository.load(conn, "p"),
                                 "VALIDATING", event_type="T")
    except order_repository.FatalRepositoryConnectionError:
        # The service entrypoint's contract: exit non-zero so systemd
        # restarts the unit and the OS releases the SQLite lock.
        sys.stdout.write("FATAL\\n")
        sys.stdout.flush()
        os._exit(4)
    sys.stdout.write("NO_FATAL\\n")
    os._exit(0)
    """
)


class TestRealProcessFailStopReleasesTheLock:
    """A mock connection cannot prove a real lock was released. This runs
    a genuine second process against a genuine SQLite file."""

    def test_lock_is_held_while_the_child_lives_and_freed_when_it_exits(self, tmp_path):
        db_path = str(tmp_path / "PROC.db")
        state_db.open_db(db_path).close()

        result = subprocess.run(
            [sys.executable, "-c", _CHILD, db_path, str(REPO_ROOT),
             str(tmp_path / "HALT.json"), str(tmp_path / "KS.json")],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=60,
        )
        assert "FATAL" in result.stdout, result.stderr
        assert result.returncode != 0, "the child did not fail-stop"

        # The child is gone, so the OS released its descriptor and a new
        # writer must succeed.
        writer = state_db.connect(db_path, busy_timeout_ms=2000)
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                "UPDATE kis_order_idempotency SET updated_at = ? WHERE internal_order_id = ?",
                ("after-restart", "p"),
            )
            writer.commit()
        finally:
            writer.close()

        verify = state_db.connect(db_path)
        try:
            row = verify.execute(
                "SELECT status, updated_at FROM kis_order_idempotency WHERE internal_order_id = 'p'"
            ).fetchone()
        finally:
            verify.close()
        assert row["updated_at"] == "after-restart"
        # The uncommitted transition never became durable.
        assert row["status"] == "CREATED"

    def test_a_live_holder_does_block_a_writer(self, tmp_path):
        """Control: proves the lock test above is meaningful -- while a
        writer really holds BEGIN IMMEDIATE, another one IS blocked."""
        db_path = str(tmp_path / "HOLD.db")
        holder = state_db.open_db(db_path)
        _seed(holder)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute(
            "UPDATE kis_order_idempotency SET updated_at='x' WHERE internal_order_id='v'")

        other = state_db.connect(db_path, busy_timeout_ms=300)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
                other.execute(
                    "UPDATE kis_order_idempotency SET updated_at='y' WHERE internal_order_id='v'")
                other.commit()
        finally:
            other.close()
            holder.rollback()
            holder.close()


class TestEntrypointsFailStop:
    """FatalRepositoryConnectionError must reach a non-zero exit code, so
    systemd's Restart=on-failure actually restarts the unit."""

    ENTRYPOINTS = [
        "run_reconciliation.py",
        "run_shadow_mode.py",
        "run_shadow_exit_evaluation.py",
        "run_live_buy_entry.py",
        "run_health_report.py",
    ]

    @pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
    def test_entrypoint_handles_the_fatal_error(self, entrypoint):
        source = (REPO_ROOT / "scripts" / entrypoint).read_text(encoding="utf-8")
        assert "FatalRepositoryConnectionError" in source
        assert "EXIT_FATAL_DB" in source
        assert "_fail_stop(" in source

    @pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
    def test_exit_code_is_non_zero(self, entrypoint, tmp_path, monkeypatch):
        if entrypoint == "run_live_buy_entry.py":
            # This one refuses to run at all under the read-only posture,
            # which is correct but would short-circuit the fatal path.
            monkeypatch.setenv("KIS_LIVE_ORDER_ENABLED", "true")
            monkeypatch.setenv("LIVE_ROLLOUT_ENABLED", "true")
            monkeypatch.setenv("ENTRY_DISABLED", "false")
        if entrypoint == "run_shadow_mode.py":
            # Shadow refuses to evaluate against a stale/absent
            # reconciliation snapshot, which would also short-circuit the
            # fatal path this test is about. Give it a usable one.
            import json
            from datetime import datetime, timezone

            snapshot = tmp_path / "RECONCILIATION.json"
            snapshot.write_text(json.dumps({
                "schema_version": 1, "clean": True, "mismatch_count": 0,
                "unknown_count": 0, "halt": False,
                "checked_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
            monkeypatch.setenv("RECONCILIATION_STATE_FILE", str(snapshot))
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        try:
            import importlib

            module = importlib.import_module(entrypoint[:-3])
            importlib.reload(module)
            # Each entrypoint's own "do the work" call.
            target = "collect" if entrypoint == "run_health_report.py" else "run_once"

            def _fatal(*args, **kwargs):
                raise FatalRepositoryConnectionError("rollback and close both failed")

            monkeypatch.setattr(module, target, _fatal)
            exit_code = module.main([])
        finally:
            sys.path.remove(str(REPO_ROOT / "scripts"))
        assert exit_code != 0
        assert exit_code == module.EXIT_FATAL_DB
