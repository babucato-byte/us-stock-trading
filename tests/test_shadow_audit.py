"""CODEX-048: the durable Shadow audit event store.

Covers the properties Codex found missing from the JSONL-only design:
every block reason recorded, sell-path coverage, a terminal event on
every run, multi-process concurrent writes without loss or corruption,
survival across a restart, retention, and no secret ever reaching the
stored rows.
"""
import multiprocessing
import os
from datetime import datetime, timedelta, timezone

import pytest

import shadow_audit
from execution import idempotency
from state_store import db as state_db

NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setenv("SHADOW_MODE_LOG_FILE", str(tmp_path / "SHADOW.jsonl"))
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "KIS_ORDER_IDEMPOTENCY.lock")
    monkeypatch.delenv("SHADOW_AUDIT_RETENTION_DAYS", raising=False)
    state_db.open_db().close()  # apply migrations once up front
    yield


class TestEventVocabulary:
    def test_every_required_event_type_exists(self):
        required = {
            "SIGNAL_RECEIVED", "CONFIG_BLOCKED", "SIGNAL_EXPIRED", "INSTRUMENT_BLOCKED",
            "PRICE_DEVIATION_BLOCKED", "CASH_BLOCKED", "RECONCILIATION_BLOCKED",
            "UNKNOWN_ORDER_BLOCKED", "DUPLICATE_BLOCKED", "HALT_BLOCKED", "GATE_REJECTED",
            "GATE_APPROVED", "EXECUTION_PLANNED", "SHADOW_COMPLETED", "SHADOW_ERROR",
        }
        assert required <= shadow_audit.EVENT_TYPES

    def test_unknown_event_type_is_refused(self):
        with pytest.raises(shadow_audit.ShadowAuditError):
            shadow_audit.record_event(
                shadow_run_id="r1", event_type="MADE_UP", result="BLOCKED", now=NOW,
            )

    @pytest.mark.parametrize("reason_code,expected", [
        ("DUPLICATE", "DUPLICATE_BLOCKED"),
        ("RECONCILIATION_UNAVAILABLE", "RECONCILIATION_BLOCKED"),
        ("RECONCILIATION_DIRTY", "RECONCILIATION_BLOCKED"),
        ("UNKNOWN_ORDER", "UNKNOWN_ORDER_BLOCKED"),
        ("HALT", "HALT_BLOCKED"),
        ("GATE:SIGNAL_EXPIRED", "SIGNAL_EXPIRED"),
        ("GATE:PRICE_DEVIATION", "PRICE_DEVIATION_BLOCKED"),
        ("GATE:CASH", "CASH_BLOCKED"),
        ("GATE:ENTRY_DISABLED", "CONFIG_BLOCKED"),
        ("GATE:OPEN_ORDER", "DUPLICATE_BLOCKED"),
        ("GATE:INSTRUMENT", "INSTRUMENT_BLOCKED"),
        ("GATE:RECONCILIATION", "RECONCILIATION_BLOCKED"),
        (None, "GATE_REJECTED"),
    ])
    def test_reason_codes_map_to_the_right_event(self, reason_code, expected):
        assert shadow_audit.event_type_for_reason_code(reason_code) == expected


class TestPersistence:
    def test_event_is_readable_back(self):
        run_id = shadow_audit.new_run_id()
        shadow_audit.record_event(
            shadow_run_id=run_id, event_type=shadow_audit.CASH_BLOCKED,
            result=shadow_audit.RESULT_BLOCKED, symbol="AAPL", side="buy",
            reason_code="INSUFFICIENT_CASH", payload={"need": 100}, now=NOW,
        )
        rows = shadow_audit.read_events(shadow_run_id=run_id)
        assert len(rows) == 1
        assert rows[0]["event_type"] == "CASH_BLOCKED"
        assert rows[0]["symbol"] == "AAPL"
        assert rows[0]["reason_code"] == "INSUFFICIENT_CASH"

    def test_events_survive_a_restart(self):
        run_id = shadow_audit.new_run_id()
        shadow_audit.record_event(
            shadow_run_id=run_id, event_type=shadow_audit.SHADOW_COMPLETED,
            result=shadow_audit.RESULT_BLOCKED, now=NOW,
        )
        # read_events() opens a brand-new connection == a restarted process.
        assert len(shadow_audit.read_events(shadow_run_id=run_id)) == 1

    def test_runs_without_a_terminal_event_are_reported(self):
        incomplete = shadow_audit.new_run_id()
        complete = shadow_audit.new_run_id()
        shadow_audit.record_event(
            shadow_run_id=incomplete, event_type=shadow_audit.SIGNAL_RECEIVED,
            result=shadow_audit.RESULT_INFO, now=NOW,
        )
        shadow_audit.record_event(
            shadow_run_id=complete, event_type=shadow_audit.SIGNAL_RECEIVED,
            result=shadow_audit.RESULT_INFO, now=NOW,
        )
        shadow_audit.record_event(
            shadow_run_id=complete, event_type=shadow_audit.SHADOW_COMPLETED,
            result=shadow_audit.RESULT_BLOCKED, now=NOW,
        )
        assert shadow_audit.runs_without_terminal_event() == [incomplete]


def _writer(db_path, index):
    """Runs in a SEPARATE PROCESS -- the only way to prove cross-process
    write safety, which a threading test cannot."""
    os.environ["STATE_STORE_DB_FILE"] = db_path
    import importlib

    import shadow_audit as sa
    from state_store import db as sdb
    importlib.reload(sdb)
    importlib.reload(sa)
    for step in range(5):
        sa.record_event(
            shadow_run_id=f"run-{index}", event_type=sa.SIGNAL_RECEIVED, result=sa.RESULT_INFO,
            symbol=f"SYM{index}", reason_code=f"step-{step}",
        )
    sa.record_event(
        shadow_run_id=f"run-{index}", event_type=sa.SHADOW_COMPLETED, result=sa.RESULT_INFO,
        symbol=f"SYM{index}",
    )


class TestConcurrentWriters:
    def test_twelve_concurrent_processes_lose_no_events(self, tmp_path):
        db_path = str(tmp_path / "TEST_STATE.db")
        state_db.open_db(db_path).close()
        ctx = multiprocessing.get_context("spawn")
        processes = [ctx.Process(target=_writer, args=(db_path, i)) for i in range(12)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=60)
        assert all(p.exitcode == 0 for p in processes), [p.exitcode for p in processes]

        conn = state_db.open_db(db_path)
        try:
            total = conn.execute("SELECT COUNT(*) AS c FROM shadow_audit_events").fetchone()["c"]
            runs = conn.execute(
                "SELECT COUNT(DISTINCT shadow_run_id) AS c FROM shadow_audit_events"
            ).fetchone()["c"]
        finally:
            conn.close()
        assert total == 12 * 6  # 5 steps + 1 terminal, per process
        assert runs == 12


class TestRedaction:
    def test_secrets_never_reach_the_stored_row(self):
        run_id = shadow_audit.new_run_id()
        shadow_audit.record_event(
            shadow_run_id=run_id, event_type=shadow_audit.SHADOW_ERROR,
            result=shadow_audit.RESULT_ERROR,
            reason_code="appkey=PSUNIQUEKEY9999 failed",
            payload={
                "appkey": "PSUNIQUEKEY9999", "CANO": "98765432",
                "nested": {"access_token": "UNIQUETOKEN7777"},
                "text": "Authorization: Bearer UNIQUETOKEN7777",
            },
            now=NOW,
        )
        row = shadow_audit.read_events(shadow_run_id=run_id)[0]
        blob = f"{row['reason_code']}{row['payload']}"
        assert "PSUNIQUEKEY9999" not in blob
        assert "UNIQUETOKEN7777" not in blob
        assert "98765432" not in blob


class TestRetention:
    def test_old_events_are_purged_and_recent_ones_kept(self):
        shadow_audit.record_event(
            shadow_run_id="old", event_type=shadow_audit.SHADOW_COMPLETED,
            result=shadow_audit.RESULT_INFO, now=NOW - timedelta(days=45),
        )
        shadow_audit.record_event(
            shadow_run_id="recent", event_type=shadow_audit.SHADOW_COMPLETED,
            result=shadow_audit.RESULT_INFO, now=NOW - timedelta(days=2),
        )
        deleted = shadow_audit.purge_old_events(now=NOW)
        assert deleted == 1
        remaining = {row["shadow_run_id"] for row in shadow_audit.read_events()}
        assert remaining == {"recent"}

    def test_retention_days_env_override(self, monkeypatch):
        monkeypatch.setenv("SHADOW_AUDIT_RETENTION_DAYS", "1")
        assert shadow_audit.retention_days() == 1

    @pytest.mark.parametrize("bad", ["", "abc", "0", "-5"])
    def test_unusable_retention_override_falls_back_to_the_default(self, monkeypatch, bad):
        monkeypatch.setenv("SHADOW_AUDIT_RETENTION_DAYS", bad)
        assert shadow_audit.retention_days() == 30
