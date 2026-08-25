"""CODEX-055 and CODEX-056: durable-state failures are normalized, and a
connection that cannot be rolled back is discarded.

CODEX-055 -- the UNKNOWN fallback caught only OrderStateTransitionError
and OrderRepositoryError, so a raw sqlite3.Error raised while writing
UNKNOWN escaped the repository boundary and skipped reason-code
normalization, the operator alert, the SHADOW_ERROR terminal and the
manual-reconciliation notice.

CODEX-056 -- compare_and_set_state() rolled back after a failed COMMIT,
but when the ROLLBACK failed too the connection kept its write
transaction open. On this codebase's single state file that locks out
every other writer, including the Shadow audit trail this very failure
needs to record itself.
"""
import sqlite3
from datetime import datetime, timezone

import pytest

import shadow_audit
from domain.execution_event import ExecutionRecord
from domain.instrument import build_instrument
from domain.order_intent import OrderIntent
from domain.signal import build_signal
from execution import execution_engine, idempotency, order_gate, order_repository
from execution.execution_engine import ExecutionEngineError
from execution.order_repository import (
    OrderRepositoryPersistenceError,
    OrderRepositoryRollbackError,
    OrderRepositoryTransactionError,
)
from state_store import db as state_db
import entry_limit_fixtures

NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
ACCOUNT_ID = "12345678"
TERMINALS = ("SHADOW_COMPLETED", "SHADOW_BLOCKED", "SHADOW_ERROR")

# Planted values that must never reach an exception message or an alert.
FAKE_ACCOUNT = "70707070"
FAKE_SECRET = "PLANTEDAPPSECRET55555"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setenv("OPERATIONS_HALT_STATE_FILE", str(tmp_path / "OPS_HALT.json"))
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
        internal_order_id="ord-1", signal_id="sig-1", strategy_id="S1_HMA_EARLY_TREND_V1", symbol="AAPL",
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
                strategy_id="S1_HMA_EARLY_TREND_V1", strategy_version="v1", config_version="cfg-1",
                code_commit="abc", symbol="AAPL", exchange="NASDAQ", signal_price=100.0,
                score=90.0, entry_reason="breakout", valid_for_seconds=300, now=NOW,
            ),
            is_regular_session=True, kis_price_usd=100.1, max_price_deviation_percent=0.30,
            usd_orderable_cash=1000.0, has_open_order_for_symbol=False,
            has_order_for_signal_id=False, allowed_symbols=frozenset({"AAPL"}),
            reconciliation=reconciliation, entry_limits=entry_limit_fixtures.unlimited(), now=NOW,
        )
    return _build


def _cancel_ctx_builder():
    def _build():
        return order_gate.CancelGateContext(
            execution_broker="kis", broker_order_id="kis-1", is_actually_open=True,
            kis_account_no=ACCOUNT_ID, allowed_account_no=ACCOUNT_ID, symbol="AAPL",
            has_cancel_already_in_flight=False,
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


def _cancel(conn, broker, run_id):
    return execution_engine.submit_cancel(
        order_intent=_order_intent(), broker_order_id="kis-1",
        cancel_gate_context_builder=_cancel_ctx_builder(), conn=conn, broker=broker,
        instrument=_instrument(), audit_run_id=run_id, now=NOW,
    )


def _events(run_id):
    return [row["event_type"] for row in shadow_audit.read_events(shadow_run_id=run_id)]


def _terminal_count(run_id):
    return sum(1 for event in _events(run_id) if event in TERMINALS)


# ---------------------------------------------------------------------------
# CODEX-055
# ---------------------------------------------------------------------------

class TestUnknownFallbackNormalization:
    """A raw sqlite3 exception from the UNKNOWN fallback must not escape."""

    def _run_with_unknown_failure(self, monkeypatch, failure):
        conn = state_db.open_db()
        broker = _Broker()
        _place_order(conn, broker)
        run_id = shadow_audit.new_run_id()

        real_advance = order_repository.advance

        def _fail(conn_, record, next_state, **kwargs):
            if next_state == "CANCELLED":
                raise order_repository.OrderRepositoryPersistenceError("final write failed")
            if next_state == "UNKNOWN":
                raise failure
            return real_advance(conn_, record, next_state, **kwargs)

        monkeypatch.setattr(execution_engine.order_repository, "advance", _fail)
        with pytest.raises(ExecutionEngineError) as excinfo:
            _cancel(conn, broker, run_id)
        return conn, broker, run_id, excinfo.value

    def test_control_a_healthy_cancel_reaches_the_transport_and_completes(self):
        """Proves the harness itself does not block, so the failures below
        are genuinely caused by the injected fault."""
        conn = state_db.open_db()
        broker = _Broker()
        _place_order(conn, broker)
        run_id = shadow_audit.new_run_id()
        result = _cancel(conn, broker, run_id)
        assert result.status == "CANCELLED"
        assert broker.cancel_calls == 1
        assert _events(run_id)[-1] == "SHADOW_COMPLETED"

    @pytest.mark.parametrize("failure", [
        sqlite3.OperationalError("disk I/O error"),
        sqlite3.IntegrityError("constraint failed"),
        sqlite3.DatabaseError("database disk image is malformed"),
        sqlite3.Error("generic sqlite failure"),
    ])
    def test_raw_sqlite_errors_are_normalized(self, monkeypatch, alerts, failure):
        conn, broker, run_id, exc = self._run_with_unknown_failure(monkeypatch, failure)

        assert not isinstance(exc, sqlite3.Error), "a raw sqlite3 error escaped"
        assert exc.reason_code == "STATE_PERSISTENCE"
        assert broker.cancel_calls == 1, "the transport DID run"
        types = _events(run_id)
        assert "SHADOW_ERROR" in types
        assert "SHADOW_BLOCKED" not in types
        assert "SHADOW_COMPLETED" not in types
        assert _terminal_count(run_id) == 1
        assert alerts, "no operator alert for an unrecoverable persistence failure"

    def test_alert_says_manual_reconciliation_is_required(self, monkeypatch, alerts):
        self._run_with_unknown_failure(monkeypatch, sqlite3.OperationalError("io"))
        joined = "\n".join(alerts).lower()
        assert "reconciliation" in joined
        assert "no automatic re-cancel" in joined

    def test_no_automatic_re_cancel(self, monkeypatch, alerts):
        _conn, broker, _run_id, _exc = self._run_with_unknown_failure(
            monkeypatch, sqlite3.OperationalError("io"),
        )
        assert broker.cancel_calls == 1

    def test_error_message_carries_no_sql_or_parameters(self, monkeypatch, alerts):
        failure = sqlite3.OperationalError(
            f"near \"UPDATE kis_order_idempotency SET status='CANCELLED' "
            f"WHERE cano='{FAKE_ACCOUNT}' AND secret='{FAKE_SECRET}'\""
        )
        _conn, _broker, _run_id, exc = self._run_with_unknown_failure(monkeypatch, failure)
        message = str(exc)
        assert FAKE_ACCOUNT not in message
        assert FAKE_SECRET not in message
        assert "UPDATE kis_order_idempotency" not in message
        # ...and neither does the operator alert.
        joined = "\n".join(alerts)
        assert FAKE_ACCOUNT not in joined
        assert FAKE_SECRET not in joined
        assert "UPDATE kis_order_idempotency" not in joined

    def test_the_original_exception_is_preserved_by_chaining(self, monkeypatch, alerts):
        failure = sqlite3.OperationalError("disk I/O error")
        _conn, _broker, _run_id, exc = self._run_with_unknown_failure(monkeypatch, failure)
        assert exc.__cause__ is failure


class TestNormalizePersistenceError:
    def test_every_sqlite_class_maps_to_one_reason_code(self):
        for failure in (sqlite3.OperationalError("a"), sqlite3.IntegrityError("b"),
                        sqlite3.DatabaseError("c"), sqlite3.Error("d"),
                        OrderRepositoryPersistenceError("e")):
            normalized = execution_engine.normalize_persistence_error(
                failure, stage="test", post_transport=True,
            )
            assert normalized.reason_code == "STATE_PERSISTENCE"
            assert normalized.__cause__ is failure
            assert "manual reconciliation required" in str(normalized)

    def test_connection_invalidation_is_reported_in_the_message(self):
        normalized = execution_engine.normalize_persistence_error(
            sqlite3.OperationalError("x"), stage="test", post_transport=True,
            connection_invalidated=True,
        )
        assert "connection was invalidated" in str(normalized)
        assert normalized.connection_invalidated is True


# ---------------------------------------------------------------------------
# CODEX-056
# ---------------------------------------------------------------------------

class _CommitFails:
    def __init__(self, conn, fail_rollback=False, fail_close=False):
        self._conn = conn
        self._fail_rollback = fail_rollback
        self._fail_close = fail_close
        self.rollback_calls = 0
        self.close_calls = 0

    def commit(self):
        raise sqlite3.OperationalError("simulated commit failure")

    def rollback(self):
        self.rollback_calls += 1
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


def _seed(conn, order_id="v"):
    idempotency.register(
        conn, internal_order_id=order_id, signal_id=f"sig-{order_id}", symbol="AAPL",
        side="buy", trading_date="2026-07-29", requested_quantity=1,
    )
    return order_repository.load(conn, order_id)


def _writer_succeeds(db_path, order_id="v"):
    """A fresh connection must be able to take the write lock."""
    conn = state_db.connect(db_path, busy_timeout_ms=800)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE kis_order_idempotency SET updated_at = ? WHERE internal_order_id = ?",
            ("later", order_id),
        )
        conn.commit()
        return True, None
    except sqlite3.OperationalError as exc:
        return False, str(exc)
    finally:
        conn.close()


class TestCommitFailureRollbackSucceeds:
    def test_normalized_error_and_the_connection_stays_usable(self, tmp_path):
        db_path = str(tmp_path / "ROLLBACK_OK.db")
        conn = state_db.open_db(db_path)
        record = _seed(conn)

        with pytest.raises(OrderRepositoryTransactionError) as excinfo:
            order_repository.advance(_CommitFails(conn), record, "VALIDATING",
                                     event_type="T", now=NOW)
        assert not isinstance(excinfo.value, sqlite3.Error)
        assert not isinstance(excinfo.value, OrderRepositoryRollbackError)
        assert conn.in_transaction is False

        # The same connection is still usable, and so is a new one.
        assert order_repository.load(conn, "v").state == "CREATED"
        conn.close()
        ok, error = _writer_succeeds(db_path)
        assert ok, error


class TestCommitAndRollbackBothFail:
    def test_connection_is_invalidated_and_the_lock_is_released(self, tmp_path, alerts):
        db_path = str(tmp_path / "POISONED.db")
        conn = state_db.open_db(db_path)
        record = _seed(conn)
        proxy = _CommitFails(conn, fail_rollback=True)

        with pytest.raises(OrderRepositoryRollbackError) as excinfo:
            order_repository.advance(proxy, record, "VALIDATING", event_type="T", now=NOW)

        assert not isinstance(excinfo.value, sqlite3.Error)
        assert proxy.close_calls == 1, "the poisoned connection was not closed"
        assert alerts, "no operator alert for an invalidated connection"

        # The poisoned connection is unusable...
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
        # ...and a new writer is NOT locked out.
        ok, error = _writer_succeeds(db_path)
        assert ok, f"a later writer was blocked: {error}"

    def test_reusing_the_invalidated_connection_is_refused(self, tmp_path, alerts):
        db_path = str(tmp_path / "REUSE.db")
        conn = state_db.open_db(db_path)
        record = _seed(conn)
        with pytest.raises(OrderRepositoryRollbackError):
            order_repository.advance(_CommitFails(conn, fail_rollback=True), record,
                                     "VALIDATING", event_type="T", now=NOW)

        with pytest.raises(OrderRepositoryPersistenceError, match="unusable"):
            order_repository.advance(conn, record, "VALIDATING", event_type="T", now=NOW)

    def test_close_failing_too_escalates_to_a_fatal_error(self, tmp_path, alerts):
        """CODEX-058: rollback AND close both failed, so the write lock may
        still be held. Python cannot conclude otherwise from a thrown
        close(), so this escalates to a process-level fault."""
        db_path = str(tmp_path / "CLOSE_FAILS.db")
        conn = state_db.open_db(db_path)
        record = _seed(conn)
        proxy = _CommitFails(conn, fail_rollback=True, fail_close=True)

        with pytest.raises(order_repository.FatalRepositoryConnectionError) as excinfo:
            order_repository.advance(proxy, record, "VALIDATING", event_type="T", now=NOW)

        assert isinstance(excinfo.value.__cause__, sqlite3.Error)
        assert proxy.close_calls == 1
        joined = "\n".join(alerts).lower()
        assert "could not be closed" in joined
        assert "must restart" in joined
        conn.rollback()
        conn.close()

    def test_partial_write_is_never_reported_as_durable(self, tmp_path, alerts):
        """The state UPDATE and its event INSERT both ran, then COMMIT and
        ROLLBACK failed. Nothing may be treated as persisted."""
        db_path = str(tmp_path / "PARTIAL.db")
        conn = state_db.open_db(db_path)
        record = _seed(conn)
        with pytest.raises(OrderRepositoryRollbackError):
            order_repository.advance(_CommitFails(conn, fail_rollback=True), record,
                                     "VALIDATING", event_type="T", now=NOW)

        fresh = state_db.connect(db_path)
        try:
            state = order_repository.load(fresh, "v").state
            events = [e["to_state"] for e in order_repository.load_events(fresh, "v")]
        finally:
            fresh.close()
        assert state == "CREATED", "an uncommitted transition was visible"
        assert events == ["CREATED"]

    def test_a_second_writer_can_proceed_after_the_poisoned_one(self, tmp_path, alerts):
        db_path = str(tmp_path / "CONCURRENT.db")
        writer_a = state_db.open_db(db_path)
        record = _seed(writer_a)
        with pytest.raises(OrderRepositoryRollbackError):
            order_repository.advance(_CommitFails(writer_a, fail_rollback=True), record,
                                     "VALIDATING", event_type="T", now=NOW)

        writer_b = state_db.open_db(db_path)
        try:
            updated = order_repository.advance(
                writer_b, order_repository.load(writer_b, "v"), "VALIDATING",
                event_type="T", now=NOW,
            )
        finally:
            writer_b.close()
        assert updated.state == "VALIDATING"


class TestAuditSurvivesAPoisonedOrderConnection:
    """CODEX-056 §6: the Shadow audit trail uses its OWN connection, so a
    poisoned order connection must not take it down with it -- which is
    only true once the poisoned connection is actually closed."""

    def test_audit_and_alert_still_work(self, tmp_path, alerts):
        db_path = str(tmp_path / "SHARED.db")
        order_conn = state_db.open_db(db_path)
        record = _seed(order_conn)
        with pytest.raises(OrderRepositoryRollbackError):
            order_repository.advance(_CommitFails(order_conn, fail_rollback=True), record,
                                     "VALIDATING", event_type="T", now=NOW)

        # A separate audit connection on the SAME file still writes.
        audit_conn = state_db.open_db(db_path)
        try:
            shadow_audit.finalize_audit_run(
                audit_run_id="poisoned-run", terminal_event=shadow_audit.SHADOW_ERROR,
                action="cancel", reason_code="STATE_PERSISTENCE", conn=audit_conn, now=NOW,
            )
            rows = shadow_audit.read_events(shadow_run_id="poisoned-run", conn=audit_conn)
        finally:
            audit_conn.close()
        assert [r["event_type"] for r in rows] == ["SHADOW_ERROR"]
        assert alerts


class TestNoRawSqliteEscapesTheRepository:
    def test_repository_functions_normalize_every_failure(self, tmp_path):
        db_path = str(tmp_path / "NORMALIZE.db")
        conn = state_db.open_db(db_path)
        record = _seed(conn)

        class _ExplodeOnUpdate:
            def __init__(self, c):
                self._c = c

            def execute(self, sql, *args, **kwargs):
                if "SET status = ?" in sql:
                    raise sqlite3.DatabaseError("database disk image is malformed")
                return self._c.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._c, name)

        with pytest.raises(OrderRepositoryPersistenceError) as excinfo:
            order_repository.advance(_ExplodeOnUpdate(conn), record, "VALIDATING",
                                     event_type="T", now=NOW)
        assert not isinstance(excinfo.value, sqlite3.Error)
        assert isinstance(excinfo.value.__cause__, sqlite3.Error)
        conn.close()

    def test_begin_failure_is_normalized(self, tmp_path):
        db_path = str(tmp_path / "BEGIN.db")
        conn = state_db.open_db(db_path)
        record = _seed(conn)

        class _ExplodeOnBegin:
            def __init__(self, c):
                self._c = c

            def execute(self, sql, *args, **kwargs):
                if sql.strip().upper().startswith("BEGIN"):
                    raise sqlite3.OperationalError("cannot start a transaction")
                return self._c.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._c, name)

        with pytest.raises(OrderRepositoryPersistenceError):
            order_repository.advance(_ExplodeOnBegin(conn), record, "VALIDATING",
                                     event_type="T", now=NOW)
        conn.close()

    def test_a_closed_connection_is_reported_clearly(self, tmp_path):
        db_path = str(tmp_path / "CLOSED.db")
        conn = state_db.open_db(db_path)
        record = _seed(conn)
        conn.close()
        with pytest.raises(OrderRepositoryPersistenceError, match="unusable"):
            order_repository.advance(conn, record, "VALIDATING", event_type="T", now=NOW)
