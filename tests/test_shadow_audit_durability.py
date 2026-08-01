"""CODEX-048 (second pass): the durability properties Codex found still
missing after the first fix.

The headline defect was ORDERING -- GATE_APPROVED and EXECUTION_PLANNED
were recorded after `execution_engine.submit_*_order()` RETURNED, i.e.
after the broker call. A crash during that call left an order that may
have reached KIS with no audit record of the approval that authorized
it. The tests below assert the events exist AT THE MOMENT the transport
is invoked, not merely that they exist afterwards.
"""
import json
import multiprocessing
import os
import sqlite3
import time
from datetime import datetime, timezone

import pytest

import shadow_audit
import shadow_mode
from brokers.kis_broker import KISAmbiguousResponseError
from domain.execution_event import ExecutionRecord
from domain.instrument import build_instrument
from domain.order_intent import OrderIntent
from execution import execution_engine, idempotency, order_gate
from execution.execution_engine import ExecutionEngineError
from reconciliation.snapshot import ReconciliationSnapshot
from state_store import db as state_db

NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
ACCOUNT_ID = "12345678"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setenv("OPERATIONS_HALT_STATE_FILE", str(tmp_path / "OPS_HALT.json"))
    monkeypatch.setenv("SHADOW_MODE_LOG_FILE", str(tmp_path / "SHADOW.jsonl"))
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "KIS_ORDER_IDEMPOTENCY.lock")
    state_db.open_db().close()
    yield


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


def _signal():
    from domain.signal import build_signal
    return build_signal(
        strategy_id="strat-1", strategy_version="v1", config_version="cfg-1", code_commit="abc",
        symbol="AAPL", exchange="NASDAQ", signal_price=100.0, score=90.0,
        entry_reason="breakout", valid_for_seconds=300, now=NOW,
    )


def _buy_ctx_builder(order_intent):
    def _build(reconciliation):
        return order_gate.BuyGateContext(
            execution_broker="kis", live_order_enabled=True, entry_disabled=False,
            validated_commit="c1", deployed_commit="c1", kis_account_no=ACCOUNT_ID,
            allowed_account_no=ACCOUNT_ID, order_intent=order_intent, instrument=_instrument(),
            signal=_signal(), is_regular_session=True, kis_price_usd=100.1,
            max_price_deviation_percent=0.30, usd_orderable_cash=1000.0,
            has_open_order_for_symbol=False, has_order_for_signal_id=False,
            allowed_symbols=frozenset({"AAPL"}), reconciliation=reconciliation, now=NOW,
        )
    return _build


class _RecordingBroker:
    """Captures the audit trail AS IT EXISTS at the instant the transport
    call is made -- read through a SEPARATE connection, so what it sees is
    only what is genuinely committed."""

    def __init__(self, raise_exc=None):
        self.raise_exc = raise_exc
        self.events_at_transport = None
        self.calls = []

    def get_positions(self):
        return []

    def get_open_orders(self):
        return []

    def get_fills(self, *, start_date, end_date):
        return []

    def submit_order(self, order_intent, instrument, *, authorization=None):
        self.calls.append(order_intent)
        conn = state_db.open_db()
        try:
            self.events_at_transport = [
                row["event_type"] for row in shadow_audit.read_events(conn=conn)
            ]
        finally:
            conn.close()
        if self.raise_exc is not None:
            raise self.raise_exc
        return ExecutionRecord(
            internal_order_id=order_intent.internal_order_id, broker="kis",
            broker_order_id="kis-1", requested_quantity=order_intent.quantity,
            requested_price=order_intent.limit_price, filled_quantity=0.0,
            average_fill_price=None, status="ACCEPTED", submitted_at=NOW, updated_at=NOW,
        )


class TestApprovalEventsPrecedeTransport:
    def test_gate_approved_and_execution_planned_are_committed_before_the_broker_call(self):
        run_id = shadow_audit.new_run_id()
        broker = _RecordingBroker()
        conn = state_db.open_db()
        oi = _order_intent()
        execution_engine.submit_buy_order(
            order_intent=oi, buy_gate_context_builder=_buy_ctx_builder(oi), conn=conn,
            broker=broker, instrument=_instrument(), account_id=ACCOUNT_ID, audit_run_id=run_id, now=NOW,
        )
        assert broker.calls, "the transport call never happened"
        assert "GATE_APPROVED" in broker.events_at_transport
        assert "EXECUTION_PLANNED" in broker.events_at_transport

    def test_events_survive_a_crash_during_the_transport_call(self):
        """A lost response is the crash-equivalent this can actually
        simulate: the broker call raises, so nothing recorded AFTER it
        would ever run -- yet the approval audit must still be there."""
        run_id = shadow_audit.new_run_id()
        broker = _RecordingBroker(raise_exc=KISAmbiguousResponseError("connection died"))
        conn = state_db.open_db()
        oi = _order_intent()
        with pytest.raises(KISAmbiguousResponseError):
            execution_engine.submit_buy_order(
                order_intent=oi, buy_gate_context_builder=_buy_ctx_builder(oi), conn=conn,
                broker=broker, instrument=_instrument(), account_id=ACCOUNT_ID, audit_run_id=run_id, now=NOW,
            )
        types = [row["event_type"] for row in shadow_audit.read_events(shadow_run_id=run_id)]
        assert "GATE_APPROVED" in types
        assert "EXECUTION_PLANNED" in types

    def test_execution_planned_is_the_last_event_before_transport(self):
        run_id = shadow_audit.new_run_id()
        broker = _RecordingBroker()
        conn = state_db.open_db()
        oi = _order_intent()
        execution_engine.submit_buy_order(
            order_intent=oi, buy_gate_context_builder=_buy_ctx_builder(oi), conn=conn,
            broker=broker, instrument=_instrument(), account_id=ACCOUNT_ID, audit_run_id=run_id, now=NOW,
        )
        assert broker.events_at_transport[-1] == "EXECUTION_PLANNED"
        assert broker.events_at_transport.index("GATE_APPROVED") < \
            broker.events_at_transport.index("EXECUTION_PLANNED")


class _FailingAuditConn:
    """Makes the shadow audit INSERT fail while leaving everything else
    working -- the audit-failure policy's trigger."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *args, **kwargs):
        if "INSERT INTO shadow_audit_events" in sql:
            raise sqlite3.OperationalError("simulated audit store failure")
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TestAuditFailureIsFailClosed:
    def test_order_is_blocked_when_the_approval_audit_cannot_be_persisted(self, monkeypatch):
        real_open = shadow_audit._open_conn

        def _broken_open():
            return _FailingAuditConn(real_open())

        monkeypatch.setattr(shadow_audit, "_open_conn", _broken_open)
        run_id = shadow_audit.new_run_id()
        broker = _RecordingBroker()
        conn = state_db.open_db()
        oi = _order_intent()
        with pytest.raises(ExecutionEngineError, match="audit event could not be persisted"):
            execution_engine.submit_buy_order(
                order_intent=oi, buy_gate_context_builder=_buy_ctx_builder(oi), conn=conn,
                broker=broker, instrument=_instrument(), account_id=ACCOUNT_ID, audit_run_id=run_id, now=NOW,
            )
        # The whole point: no order was submitted.
        assert broker.calls == []

    def test_no_try_except_pass_around_audit_persistence_on_the_order_path(self):
        """Structural: a swallowed audit failure is invisible at runtime,
        so it is asserted against statically."""
        import ast
        import pathlib

        repo_root = pathlib.Path(__file__).resolve().parent.parent
        for rel in ("kis_live_trading.py", "brokers/kis_broker_adapter.py",
                    "execution/execution_engine.py", "shadow_audit.py"):
            tree = ast.parse((repo_root / rel).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Try):
                    continue
                for handler in node.handlers:
                    body = [n for n in handler.body if not isinstance(n, ast.Expr)]
                    if len(body) == 1 and isinstance(body[0], ast.Pass):
                        source = ast.dump(node)
                        assert "ShadowAudit" not in source, (
                            f"{rel} swallows a shadow-audit failure with `pass`"
                        )

    def test_handle_audit_failure_alerts_and_raises(self, monkeypatch):
        sent = []
        from operations import alerts

        monkeypatch.setattr(alerts, "send_alert", lambda message: sent.append(message) or True)
        with pytest.raises(shadow_audit.ShadowAuditFailure):
            shadow_audit.handle_audit_failure(
                shadow_audit.ShadowAuditError("disk full"), shadow_run_id="run-x",
                symbol="AAPL", side="buy", stage="GATE_APPROVED",
            )
        assert sent, "no operator alert was raised for an audit persistence failure"
        # The retry recorded the terminal SHADOW_ERROR for that run.
        types = [row["event_type"] for row in shadow_audit.read_events(shadow_run_id="run-x")]
        assert types == ["SHADOW_ERROR"]


class TestExactlyOneTerminalEventPerRun:
    def _record(self, run_id, event_type, result):
        shadow_audit.record_event(
            shadow_run_id=run_id, event_type=event_type, result=result, now=NOW,
        )

    def test_zero_terminal_events_is_reported(self):
        self._record("run-open", shadow_audit.SIGNAL_RECEIVED, shadow_audit.RESULT_INFO)
        report = shadow_audit.audit_integrity_report()
        assert report["runs_without_terminal_event"] == ["run-open"]

    def test_two_terminal_events_is_reported(self):
        self._record("run-double", shadow_audit.SHADOW_COMPLETED, shadow_audit.RESULT_APPROVED)
        self._record("run-double", shadow_audit.SHADOW_BLOCKED, shadow_audit.RESULT_BLOCKED)
        report = shadow_audit.audit_integrity_report()
        assert report["runs_with_multiple_terminal_events"] == ["run-double"]

    def test_exactly_one_is_clean(self):
        self._record("run-ok", shadow_audit.SIGNAL_RECEIVED, shadow_audit.RESULT_INFO)
        self._record("run-ok", shadow_audit.SHADOW_BLOCKED, shadow_audit.RESULT_BLOCKED)
        report = shadow_audit.audit_integrity_report()
        assert report["runs_without_terminal_event"] == []
        assert report["runs_with_multiple_terminal_events"] == []

    @pytest.mark.parametrize("result,expected", [
        (shadow_audit.RESULT_APPROVED, "SHADOW_COMPLETED"),
        (shadow_audit.RESULT_INFO, "SHADOW_COMPLETED"),
        (shadow_audit.RESULT_BLOCKED, "SHADOW_BLOCKED"),
        (shadow_audit.RESULT_ERROR, "SHADOW_ERROR"),
        ("SOMETHING_UNRECOGNISED", "SHADOW_ERROR"),
    ])
    def test_terminal_event_mapping(self, result, expected):
        assert shadow_audit.terminal_event_for(result) == expected


class TestCrossConnectionDurability:
    def test_event_is_readable_after_close_and_reopen(self, tmp_path):
        db_path = str(tmp_path / "DURABLE.db")
        state_db.open_db(db_path).close()
        conn = state_db.open_db(db_path)
        shadow_audit.record_event(
            shadow_run_id="durable", event_type=shadow_audit.SHADOW_COMPLETED,
            result=shadow_audit.RESULT_APPROVED, now=NOW, conn=conn,
        )
        conn.close()

        fresh = state_db.open_db(db_path)
        try:
            rows = shadow_audit.read_events(shadow_run_id="durable", conn=fresh)
        finally:
            fresh.close()
        assert len(rows) == 1

    def test_migration_9_is_applied(self):
        from state_store.migrations import CURRENT_SCHEMA_VERSION

        conn = state_db.open_db()
        try:
            assert state_db.get_schema_version(conn) >= 9
            assert state_db.get_schema_version(conn) == CURRENT_SCHEMA_VERSION
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='shadow_audit_events'"
            ).fetchone()
            assert table is not None
        finally:
            conn.close()


class TestBusyRetry:
    def test_write_retries_on_a_locked_database(self, monkeypatch):
        """A writer that hits SQLITE_BUSY must retry, not drop the event."""
        attempts = {"count": 0}
        real_insert = shadow_audit._insert_once

        def _flaky_insert(conn, values):
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise sqlite3.OperationalError("database is locked")
            return real_insert(conn, values)

        monkeypatch.setattr(shadow_audit, "_insert_once", _flaky_insert)
        monkeypatch.setattr(shadow_audit, "WRITE_RETRY_BASE_SECONDS", 0.001)
        shadow_audit.record_event(
            shadow_run_id="retried", event_type=shadow_audit.SHADOW_COMPLETED,
            result=shadow_audit.RESULT_APPROVED, now=NOW,
        )
        assert attempts["count"] == 3
        assert len(shadow_audit.read_events(shadow_run_id="retried")) == 1

    def test_persistent_lock_raises_rather_than_silently_dropping(self, monkeypatch):
        def _always_locked(conn, values):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(shadow_audit, "_insert_once", _always_locked)
        monkeypatch.setattr(shadow_audit, "WRITE_RETRY_BASE_SECONDS", 0.001)
        with pytest.raises(shadow_audit.ShadowAuditError, match="after 5 attempts"):
            shadow_audit.record_event(
                shadow_run_id="never", event_type=shadow_audit.SHADOW_COMPLETED,
                result=shadow_audit.RESULT_APPROVED, now=NOW,
            )


def _jsonl_writer(log_path, index):
    """Separate process: appends rows to the SAME JSONL file while others
    do, across a size threshold that forces rotation mid-run."""
    os.environ["SHADOW_MODE_LOG_FILE"] = ""
    import importlib

    import shadow_mode as sm
    importlib.reload(sm)
    sm.BASE_DIR = os.path.dirname(log_path)
    target = __import__("pathlib").Path(log_path)
    for step in range(10):
        record = sm.build_record(
            signal_id=f"p{index}-{step}", strategy_id="s", strategy_version="v1",
            code_commit="c", symbol=f"SYM{index}", risk_gate_result="BLOCKED",
            rejection_reason="concurrent rotation test",
        )
        sm.persist(record, path=target)


class TestJsonlDurability:
    def test_persist_calls_fsync(self, tmp_path, monkeypatch):
        calls = []
        real_fsync = os.fsync
        monkeypatch.setattr(
            shadow_mode.os, "fsync", lambda fd: calls.append(fd) or real_fsync(fd),
        )
        target = tmp_path / "SHADOW.jsonl"
        shadow_mode.persist(
            shadow_mode.build_record(
                signal_id="s1", strategy_id="s", strategy_version="v1", code_commit="c",
                symbol="AAPL", risk_gate_result="BLOCKED", rejection_reason="r", now=NOW,
            ),
            path=target,
        )
        assert calls, "persist() did not fsync the appended line"

    def test_rotation_happens_inside_the_write_lock(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SHADOW_AUDIT_MAX_FILE_MB", "0.0001")  # ~100 bytes
        target = tmp_path / "shadow-2026-07-29.jsonl"
        for index in range(5):
            shadow_mode.persist(
                shadow_mode.build_record(
                    signal_id=f"s{index}", strategy_id="s", strategy_version="v1",
                    code_commit="c", symbol="AAPL", risk_gate_result="BLOCKED",
                    rejection_reason="rotation", now=NOW,
                ),
                path=target,
            )
        rotated = sorted(tmp_path.glob("shadow-2026-07-29.*.jsonl"))
        assert rotated, "size-based rotation never triggered"
        total = len(shadow_mode.read_all(path=target))
        for path in rotated:
            total += len(shadow_mode.read_all(path=path))
        assert total == 5, "rotation lost records"

    def test_concurrent_processes_do_not_interleave_or_lose_lines(self, tmp_path):
        log_path = str(tmp_path / "shadow-concurrent.jsonl")
        ctx = multiprocessing.get_context("spawn")
        processes = [ctx.Process(target=_jsonl_writer, args=(log_path, i)) for i in range(12)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=60)
        assert all(p.exitcode == 0 for p in processes), [p.exitcode for p in processes]

        from pathlib import Path

        lines = Path(log_path).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 120
        for line in lines:
            json.loads(line)  # every line is a complete, parseable object

    def test_corruption_is_detected_and_reported(self, tmp_path, monkeypatch):
        target = tmp_path / "SHADOW.jsonl"
        shadow_mode.persist(
            shadow_mode.build_record(
                signal_id="s1", strategy_id="s", strategy_version="v1", code_commit="c",
                symbol="AAPL", risk_gate_result="BLOCKED", rejection_reason="r", now=NOW,
            ),
            path=target,
        )
        with open(target, "a", encoding="utf-8") as fh:
            fh.write('{"truncated": tru\n')

        records, corruption = shadow_mode.read_all_with_integrity(path=target)
        assert len(records) == 1
        assert corruption == [(str(target), 2)]

        with pytest.raises(shadow_mode.ShadowModeError, match="unreadable"):
            shadow_mode.read_all_strict(path=target)

        sent = []
        from operations import alerts

        monkeypatch.setattr(alerts, "send_alert", lambda message: sent.append(message) or True)
        assert shadow_mode.verify_log_integrity(path=target) == [(str(target), 2)]
        assert sent, "corruption was not raised as an operational alert"

    def test_retention_deletes_only_old_day_files(self, tmp_path):
        old = tmp_path / "shadow-2026-01-01.jsonl"
        recent = tmp_path / "shadow-2026-07-28.jsonl"
        old.write_text("{}\n", encoding="utf-8")
        recent.write_text("{}\n", encoding="utf-8")
        deleted = shadow_mode.purge_old_files(days=30, now=NOW, base_dir=tmp_path)
        assert [p.name for p in deleted] == ["shadow-2026-01-01.jsonl"]
        assert recent.exists()


class TestRedactionAtBothStores:
    def test_no_planted_secret_reaches_either_store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIS_ACCOUNT_NO", "60606060")
        secret = "PLANTEDSECRET99999"
        shadow_audit.record_event(
            shadow_run_id="redact", event_type=shadow_audit.SHADOW_ERROR,
            result=shadow_audit.RESULT_ERROR, reason_code=f"appkey={secret}",
            payload={"CANO": "60606060", "raw": f"Bearer {secret}"}, now=NOW,
        )
        target = tmp_path / "SHADOW.jsonl"
        shadow_mode.persist(
            shadow_mode.build_record(
                signal_id="s1", strategy_id="s", strategy_version="v1", code_commit="c",
                symbol="AAPL", risk_gate_result="BLOCKED",
                rejection_reason=f"account 60606060 appsecret={secret}", now=NOW,
            ),
            path=target,
        )
        blob = json.dumps(shadow_audit.read_events(shadow_run_id="redact"), default=str)
        blob += target.read_text(encoding="utf-8")
        assert secret not in blob
        assert "60606060" not in blob
