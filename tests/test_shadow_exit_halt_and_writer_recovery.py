"""Two findings from the same review round.

HIGH   Shadow exit never asked whether HALT was set. It read the
       reconciliation snapshot -- which records the HALT state at
       RECONCILIATION time -- and nothing checked the switch itself, so a
       HALT raised after the last reconciler run had no effect on the
       exit pass at all.

MEDIUM The snapshot writer reported a directory-fsync failure as "write
       failed", which claims the previous snapshot is still in place --
       exactly what is unknown after os.replace() has already landed. It
       also accepted a naive datetime (producing a snapshot the reader
       must reject) and left a SIGKILLed writer's temp behind forever.
"""
import errno
import json
import os
import signal
import stat
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import shadow_audit
from execution import idempotency
from positions import lifecycle
from reconciliation import freshness, reconciliation_state
from state_store import db as state_db

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
NOW = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)
UUID32 = "a" * 32


def _exit_module():
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import importlib

        return importlib.import_module("run_shadow_exit_evaluation")
    finally:
        sys.path.remove(str(SCRIPTS_DIR))


# =====================================================================
# Shadow exit: HALT is read at run time, from the switch itself.
# =====================================================================

class _Broker:
    """Records the order in which the pass reaches for account data."""

    def __init__(self, price=100.0):
        self.calls = []
        self.price = price

    def get_account_snapshot(self):
        self.calls.append("get_account_snapshot")
        return type("S", (), {"account_id": "44991234",
                              "usd_available_for_new_order": 0.0})()

    def get_positions(self):
        self.calls.append("get_positions")
        return []

    def get_open_orders(self):
        self.calls.append("get_open_orders")
        return []

    def get_fills(self, **kwargs):
        self.calls.append("get_fills")
        return []

    def get_current_price(self, instrument):
        self.calls.append("get_current_price")
        return self.price

    def submit_order(self, *args, **kwargs):        # pragma: no cover
        self.calls.append("SUBMIT")
        raise AssertionError("shadow exit reached an order transport")

    def cancel_order(self, *args, **kwargs):        # pragma: no cover
        self.calls.append("CANCEL")
        raise AssertionError("shadow exit reached a cancel transport")


@pytest.fixture
def exit_env(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setenv("OPERATIONS_HALT_STATE_FILE", str(tmp_path / "HALT.json"))
    monkeypatch.setenv("RECONCILIATION_STATE_FILE", str(tmp_path / "RECON.json"))
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "IDEMPOTENCY.lock")
    monkeypatch.delenv("SHADOW_MODE_LOG_FILE", raising=False)
    monkeypatch.delenv("SHADOW_MODE_LOG_DIR", raising=False)
    state_db.open_db().close()
    return tmp_path


def _set_halt(tmp_path, halted):
    from operations import kill_switch

    kill_switch.set_halt(halted, reason="test", actor="test")


def _events(symbol=None):
    conn = state_db.open_db()
    try:
        rows = conn.execute(
            "select shadow_run_id, symbol, event_type, result, reason_code, payload "
            "from shadow_audit_events order by rowid").fetchall()
    finally:
        conn.close()
    return [dict(run=r[0], symbol=r[1], event_type=r[2], result=r[3], reason_code=r[4],
                 payload=json.loads(r[5]) if r[5] else None)
            for r in rows if symbol is None or r[1] == symbol]


class TestHaltIsReadAtRunTime:
    def test_the_switch_itself_is_consulted(self, exit_env, monkeypatch):
        module = _exit_module()
        calls = []
        from operations import kill_switch

        monkeypatch.setattr(kill_switch, "is_halted",
                            lambda: calls.append("is_halted") or False)
        assert module.read_halt_state() is False
        assert calls == ["is_halted"]

    def test_it_is_not_taken_from_the_snapshot(self):
        """The snapshot's `halt` describes reconciliation time, which may
        be minutes old."""
        source = (SCRIPTS_DIR / "run_shadow_exit_evaluation.py").read_text(encoding="utf-8")
        assert "kill_switch.is_halted" in source
        assert 'snapshot.get("halt"' not in source
        assert "reconciliation_state" not in source

    def test_halt_true_is_reported_as_true(self, exit_env, monkeypatch):
        module = _exit_module()
        from operations import kill_switch

        monkeypatch.setattr(kill_switch, "is_halted", lambda: True)
        assert module.read_halt_state() is True

    def test_a_lookup_failure_is_fail_closed(self, exit_env, monkeypatch):
        module = _exit_module()
        from operations import kill_switch

        monkeypatch.setattr(kill_switch, "is_halted",
                            lambda: (_ for _ in ()).throw(OSError("state unreadable")))
        with pytest.raises(module.HaltStatusUnavailable) as excinfo:
            module.read_halt_state()
        assert excinfo.value.reason_code == "HALT_STATUS_UNAVAILABLE"

    def test_a_malformed_halt_state_fails_closed(self, exit_env, monkeypatch):
        """kill_switch itself fails closed to halted on corruption; this
        pass must not turn that into 'clear'."""
        module = _exit_module()
        Path(os.environ["OPERATIONS_HALT_STATE_FILE"]).write_text("{not json",
                                                                  encoding="utf-8")
        try:
            result = module.read_halt_state()
        except module.HaltStatusUnavailable:
            result = "blocked"
        assert result in (True, "blocked"), result

    def test_no_broker_call_precedes_the_halt_lookup(self, exit_env, monkeypatch):
        module = _exit_module()
        broker = _Broker()
        from operations import kill_switch

        monkeypatch.setattr(kill_switch, "is_halted",
                            lambda: (_ for _ in ()).throw(OSError("boom")))
        with pytest.raises(module.HaltStatusUnavailable):
            module.run_once(broker=broker, now=NOW)
        assert broker.calls == [], broker.calls

    def test_the_entrypoint_exits_non_zero_when_halt_is_unreadable(self, exit_env,
                                                                    monkeypatch):
        module = _exit_module()
        from operations import kill_switch

        monkeypatch.setattr(kill_switch, "is_halted",
                            lambda: (_ for _ in ()).throw(OSError("boom")))
        assert module.main([]) == module.EXIT_HALT_UNAVAILABLE

    def test_the_lookup_happens_before_positions_are_read(self, exit_env, monkeypatch):
        module = _exit_module()
        order = []
        from operations import kill_switch
        from positions import store

        monkeypatch.setattr(kill_switch, "is_halted",
                            lambda: order.append("halt") or False)
        monkeypatch.setattr(store, "load_non_terminal",
                            lambda: order.append("positions") or {})
        broker = _Broker()
        module.run_once(broker=broker, now=NOW)
        assert order[0] == "halt", order
        assert order.index("halt") < order.index("positions")


# =====================================================================
# What HALT does to an exit decision.
# =====================================================================

def _position(symbol="AAPL", stop=90.0, target_1=110.0, target_2=120.0, qty=10):
    """A TARGET_1_ACTIVE position: target_2 is reachable from this state,
    so a price above it produces a real TARGET_2 decision rather than
    ACTION_NONE."""
    from positions import states

    return {
        "symbol": symbol, "state": states.TARGET_1_ACTIVE, "stop_price": stop,
        "target_1_price": target_1, "target_2_price": target_2,
        "remaining_qty": qty, "entry_price": 100.0, "quantity": qty,
        "entry_time": None, "partial_taken": False,
        "trail_active": False, "highest_price": 100.0,
    }


class TestExitClassification:
    def test_protective_exits_are_risk_reduction(self):
        assert lifecycle.classify_exit("STOP_LOSS") == "RISK_REDUCTION"
        assert lifecycle.classify_exit("EOD_FORCED_CLOSE") == "RISK_REDUCTION"

    @pytest.mark.parametrize("reason", ["TARGET_1", "TARGET_2", "TIME_STOP",
                                        "TRAILING_BREAKEVEN"])
    def test_strategy_exits_are_strategy(self, reason):
        assert lifecycle.classify_exit(reason) == "STRATEGY"

    def test_an_unrecognised_reason_defaults_to_strategy(self):
        """A whitelist: a new exit rule cannot quietly acquire permission
        to act while halted."""
        assert lifecycle.classify_exit("SOME_FUTURE_RULE") == "STRATEGY"


class TestHaltBlocksStrategyExitsOnly:
    def _evaluate(self, module, broker, *, halted, price, record=None):
        conn = state_db.open_db()
        try:
            return module.evaluate_position(
                position_id="pos-1", record=record or _position(), broker=broker,
                conn=conn, exit_flags=type("F", (), {
                    "enable_partial_profit": False, "enable_trailing_stop": False,
                    "enable_time_stop": False, "enable_eod_exit": False})(),
                now=NOW, eastern_now=NOW, account_id="44991234", halted=halted,
            )
        finally:
            conn.close()

    def test_control_a_target_exit_is_approved_when_not_halted(self, exit_env,
                                                                monkeypatch):
        module = _exit_module()
        outcome = self._evaluate(module, _Broker(price=125.0), halted=False, price=125.0)
        assert outcome["reason_code"] == "TARGET_2", outcome
        assert outcome["result"] == "APPROVED"
        assert outcome["exit_classification"] == "STRATEGY"

    def test_a_strategy_exit_is_blocked_while_halted(self, exit_env, monkeypatch):
        module = _exit_module()
        outcome = self._evaluate(module, _Broker(price=125.0), halted=True, price=125.0)
        assert outcome["reason_code"] == "HALT_ACTIVE"
        assert outcome["result"] == "BLOCKED"
        events = [e["event_type"] for e in _events("AAPL")]
        assert "EXIT_BLOCKED_HALT" in events

    def test_a_protective_exit_is_still_evaluated_while_halted(self, exit_env,
                                                               monkeypatch):
        """HALT stops automated execution; it is not an instruction to
        sit on a position while its stop fires."""
        module = _exit_module()
        outcome = self._evaluate(module, _Broker(price=80.0), halted=True, price=80.0)
        assert outcome["reason_code"] == "STOP_LOSS", outcome
        assert outcome["exit_classification"] == "RISK_REDUCTION"
        assert outcome["result"] == "APPROVED"

    def test_the_halt_state_is_recorded_on_every_run(self, exit_env, monkeypatch):
        module = _exit_module()
        self._evaluate(module, _Broker(price=125.0), halted=True, price=125.0)
        checked = [e for e in _events("AAPL") if e["event_type"] == "HALT_CHECKED"]
        assert len(checked) == 1
        assert checked[0]["reason_code"] == "HALT_TRUE"

    def test_the_halt_state_is_recorded_when_clear_too(self, exit_env, monkeypatch):
        module = _exit_module()
        self._evaluate(module, _Broker(price=125.0), halted=False, price=125.0)
        checked = [e for e in _events("AAPL") if e["event_type"] == "HALT_CHECKED"]
        assert checked and checked[0]["reason_code"] == "HALT_FALSE"

    def test_an_approved_exit_records_transport_suppressed(self, exit_env, monkeypatch):
        module = _exit_module()
        self._evaluate(module, _Broker(price=80.0), halted=True, price=80.0)
        approved = [e for e in _events("AAPL") if e["event_type"] == "GATE_APPROVED"]
        assert approved
        assert "transport_suppressed=true" in approved[-1]["payload"]["detail"]
        assert "exit_classification=RISK_REDUCTION" in approved[-1]["payload"]["detail"]

    def test_no_order_or_cancel_transport_on_any_path(self, exit_env, monkeypatch):
        module = _exit_module()
        for halted, price in ((False, 125.0), (True, 125.0), (True, 80.0), (False, 80.0)):
            broker = _Broker(price=price)
            self._evaluate(module, broker, halted=halted, price=price)
            assert not [c for c in broker.calls if c in ("SUBMIT", "CANCEL")]

    def test_exactly_one_terminal_event_per_run(self, exit_env, monkeypatch):
        module = _exit_module()
        outcome = self._evaluate(module, _Broker(price=125.0), halted=True, price=125.0)
        run_events = [e for e in _events() if e["run"] == outcome["run_id"]]
        terminal = [e for e in run_events
                    if e["event_type"] in shadow_audit.TERMINAL_EVENT_TYPES]
        assert len(terminal) == 1, [e["event_type"] for e in run_events]

    def test_the_new_events_are_not_terminal(self):
        assert "HALT_CHECKED" in shadow_audit.EVENT_TYPES
        assert "EXIT_BLOCKED_HALT" in shadow_audit.EVENT_TYPES
        assert not ({"HALT_CHECKED", "EXIT_BLOCKED_HALT"}
                    & shadow_audit.TERMINAL_EVENT_TYPES)


# =====================================================================
# Writer: timezone.
# =====================================================================

class TestWriterRequiresAnAwareTimestamp:
    def test_control_an_aware_timestamp_is_written(self, tmp_path):
        target = tmp_path / "R.json"
        reconciliation_state.record_result(clean=True, mismatch_count=0, unknown_count=0,
                                           halt=False, now=NOW, path=target)
        assert json.loads(target.read_text(encoding="utf-8"))["checked_at"].endswith("+00:00")

    def test_a_naive_timestamp_is_refused(self, tmp_path):
        target = tmp_path / "R.json"
        with pytest.raises(reconciliation_state.ReconciliationStateError) as excinfo:
            reconciliation_state.record_result(
                clean=True, mismatch_count=0, unknown_count=0, halt=False,
                now=datetime(2026, 8, 5, 15, 0), path=target)
        assert excinfo.value.reason_code == "RECONCILIATION_TIMESTAMP_TIMEZONE_MISSING"

    def test_a_refused_naive_write_leaves_the_previous_snapshot(self, tmp_path):
        target = tmp_path / "R.json"
        reconciliation_state.record_result(clean=True, mismatch_count=0, unknown_count=0,
                                           halt=False, now=NOW, path=target)
        before = target.read_bytes()
        with pytest.raises(reconciliation_state.ReconciliationStateError):
            reconciliation_state.record_result(
                clean=False, mismatch_count=5, unknown_count=0, halt=False,
                now=datetime(2026, 8, 5, 15, 0), path=target)
        assert target.read_bytes() == before

    def test_a_refused_naive_write_leaves_no_temp(self, tmp_path):
        target = tmp_path / "R.json"
        with pytest.raises(reconciliation_state.ReconciliationStateError):
            reconciliation_state.record_result(
                clean=True, mismatch_count=0, unknown_count=0, halt=False,
                now=datetime(2026, 8, 5, 15, 0), path=target)
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []

    def test_a_non_utc_offset_is_normalised(self, tmp_path):
        target = tmp_path / "R.json"
        tokyo = timezone(timedelta(hours=9))
        reconciliation_state.record_result(
            clean=True, mismatch_count=0, unknown_count=0, halt=False,
            now=NOW.astimezone(tokyo), path=target)
        written = json.loads(target.read_text(encoding="utf-8"))["checked_at"]
        assert written.endswith("+00:00")
        assert freshness.evaluate(path=target, now=NOW).age_seconds == 0


# =====================================================================
# Writer: failures before and after the replace.
# =====================================================================

class TestFailuresBeforeReplacePreserveTheSnapshot:
    def _existing(self, tmp_path):
        target = tmp_path / "R.json"
        reconciliation_state.record_result(clean=True, mismatch_count=0, unknown_count=0,
                                           halt=False, now=NOW, path=target)
        return target, target.read_bytes()

    def test_a_temp_write_failure(self, tmp_path, monkeypatch):
        target, before = self._existing(tmp_path)
        real_open = open
        monkeypatch.setattr(
            "builtins.open",
            lambda p, *a, **k: (_ for _ in ()).throw(OSError(errno.EIO, "no"))
            if str(p).endswith(".tmp") else real_open(p, *a, **k))
        with pytest.raises(reconciliation_state.ReconciliationStateError):
            reconciliation_state.record_result(clean=False, mismatch_count=1,
                                               unknown_count=0, halt=False, now=NOW,
                                               path=target)
        assert target.read_bytes() == before

    def test_a_file_fsync_failure(self, tmp_path, monkeypatch):
        target, before = self._existing(tmp_path)
        monkeypatch.setattr(reconciliation_state.os, "fsync",
                            lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EIO, "no")))
        with pytest.raises(reconciliation_state.ReconciliationStateError):
            reconciliation_state.record_result(clean=False, mismatch_count=1,
                                               unknown_count=0, halt=False, now=NOW,
                                               path=target)
        assert target.read_bytes() == before
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []

    def test_a_replace_failure(self, tmp_path, monkeypatch):
        target, before = self._existing(tmp_path)
        monkeypatch.setattr(reconciliation_state.os, "replace",
                            lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EIO, "no")))
        with pytest.raises(reconciliation_state.ReconciliationStateError) as excinfo:
            reconciliation_state.record_result(clean=False, mismatch_count=1,
                                               unknown_count=0, halt=False, now=NOW,
                                               path=target)
        assert not isinstance(excinfo.value,
                              reconciliation_state.ReconciliationCommitUncertain)
        assert target.read_bytes() == before
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


class TestDirectoryFsyncFailureIsCommitUncertain:
    def _break_dir_fsync(self, monkeypatch):
        real_open = os.open

        def _open(path, *args, **kwargs):
            if Path(str(path)).is_dir():
                raise OSError(errno.EIO, "no directory descriptor")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(reconciliation_state.os, "open", _open)

    def test_it_is_not_reported_as_a_failed_write(self, tmp_path, monkeypatch):
        """os.replace() already landed, so claiming the old snapshot is
        still in place would be a false statement."""
        target = tmp_path / "R.json"
        reconciliation_state.record_result(clean=True, mismatch_count=0, unknown_count=0,
                                           halt=False, now=NOW, path=target)
        before = target.read_bytes()
        self._break_dir_fsync(monkeypatch)
        with pytest.raises(reconciliation_state.ReconciliationCommitUncertain) as excinfo:
            reconciliation_state.record_result(clean=True, mismatch_count=0,
                                               unknown_count=1, halt=False, now=NOW,
                                               path=target)
        assert excinfo.value.reason_code == "RECONCILIATION_SNAPSHOT_COMMIT_UNCERTAIN"
        assert target.read_bytes() != before, "the new snapshot was rolled back"

    def test_nothing_is_rolled_back(self, tmp_path, monkeypatch):
        target = tmp_path / "R.json"
        self._break_dir_fsync(monkeypatch)
        with pytest.raises(reconciliation_state.ReconciliationCommitUncertain):
            reconciliation_state.record_result(clean=True, mismatch_count=0,
                                               unknown_count=0, halt=False, now=NOW,
                                               path=target)
        assert target.exists(), "a possibly-committed snapshot was deleted"

    def test_the_writer_is_marked_uncertain(self, tmp_path, monkeypatch):
        target = tmp_path / "R.json"
        self._break_dir_fsync(monkeypatch)
        with pytest.raises(reconciliation_state.ReconciliationCommitUncertain):
            reconciliation_state.record_result(clean=True, mismatch_count=0,
                                               unknown_count=0, halt=False, now=NOW,
                                               path=target)
        assert reconciliation_state.commit_is_uncertain(target) is True

    def test_the_freshness_gate_refuses_while_uncertain(self, tmp_path, monkeypatch):
        target = tmp_path / "R.json"
        self._break_dir_fsync(monkeypatch)
        with pytest.raises(reconciliation_state.ReconciliationCommitUncertain):
            reconciliation_state.record_result(clean=True, mismatch_count=0,
                                               unknown_count=0, halt=False, now=NOW,
                                               path=target)
        monkeypatch.undo()
        with pytest.raises(freshness.SnapshotUnusable) as excinfo:
            freshness.evaluate(path=target, now=NOW)
        assert excinfo.value.reason_code == "RECONCILIATION_SNAPSHOT_COMMIT_UNCERTAIN"

    def test_a_complete_write_clears_the_uncertainty(self, tmp_path, monkeypatch):
        target = tmp_path / "R.json"
        self._break_dir_fsync(monkeypatch)
        with pytest.raises(reconciliation_state.ReconciliationCommitUncertain):
            reconciliation_state.record_result(clean=True, mismatch_count=0,
                                               unknown_count=0, halt=False, now=NOW,
                                               path=target)
        monkeypatch.undo()
        reconciliation_state.record_result(clean=True, mismatch_count=0, unknown_count=0,
                                           halt=False, now=NOW, path=target)
        assert reconciliation_state.commit_is_uncertain(target) is False
        assert freshness.evaluate(path=target, now=NOW).clean is True

    def test_the_marker_survives_across_processes(self, tmp_path, monkeypatch):
        target = tmp_path / "R.json"
        self._break_dir_fsync(monkeypatch)
        with pytest.raises(reconciliation_state.ReconciliationCommitUncertain):
            reconciliation_state.record_result(clean=True, mismatch_count=0,
                                               unknown_count=0, halt=False, now=NOW,
                                               path=target)
        monkeypatch.undo()
        monkeypatch.setattr(reconciliation_state, "_COMMIT_UNCERTAIN", False)
        assert reconciliation_state.commit_is_uncertain(target) is True


# =====================================================================
# Writer: stale temp recovery.
# =====================================================================

def _temp_name(target, pid, token=UUID32):
    return f".{Path(target).name}.{pid}.{token}.tmp"


class TestStaleTempRecovery:
    def test_the_temp_name_carries_the_pid_and_a_uuid(self, tmp_path, monkeypatch):
        target = tmp_path / "R.json"
        seen = {}
        real_replace = os.replace

        def _capture(src, dst):
            seen["temp"] = Path(src).name
            return real_replace(src, dst)

        monkeypatch.setattr(reconciliation_state.os, "replace", _capture)
        reconciliation_state.record_result(clean=True, mismatch_count=0, unknown_count=0,
                                           halt=False, now=NOW, path=target)
        assert seen["temp"].startswith(".R.json.")
        assert seen["temp"].endswith(".tmp")
        assert str(os.getpid()) in seen["temp"]

    def test_a_dead_owners_temp_is_removed_by_the_next_write(self, tmp_path):
        target = tmp_path / "R.json"
        orphan = tmp_path / _temp_name(target, 999999)
        orphan.write_text("partial", encoding="utf-8")
        reconciliation_state.record_result(clean=True, mismatch_count=0, unknown_count=0,
                                           halt=False, now=NOW, path=target)
        assert not orphan.exists()
        assert freshness.evaluate(path=target, now=NOW).clean is True

    def test_several_orphans_are_all_removed(self, tmp_path):
        target = tmp_path / "R.json"
        for index in range(3):
            (tmp_path / _temp_name(target, 999990 + index, f"{index:032x}")).write_text(
                "partial", encoding="utf-8")
        reconciliation_state.record_result(clean=True, mismatch_count=0, unknown_count=0,
                                           halt=False, now=NOW, path=target)
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []

    def test_a_live_owners_temp_blocks_and_is_kept(self, tmp_path):
        target = tmp_path / "R.json"
        mine = tmp_path / _temp_name(target, os.getpid())
        mine.write_text("partial", encoding="utf-8")
        with pytest.raises(reconciliation_state.ReconciliationStateError) as excinfo:
            reconciliation_state.record_result(clean=True, mismatch_count=0,
                                               unknown_count=0, halt=False, now=NOW,
                                               path=target)
        assert excinfo.value.reason_code == "RECONCILIATION_TEMP_ARTIFACT_INVALID"
        assert mine.exists()

    @pytest.mark.parametrize("name", [
        ".R.json.notpid.{u}.tmp", ".R.json.123.{u}.temp",
        ".R.json.123.{u}.tmp.extra", ".R.json.123.tmp",
    ])
    def test_a_malformed_artifact_blocks_and_is_kept(self, tmp_path, name):
        target = tmp_path / "R.json"
        bad = tmp_path / name.format(u=UUID32)
        bad.write_text("junk", encoding="utf-8")
        with pytest.raises(reconciliation_state.ReconciliationStateError) as excinfo:
            reconciliation_state.record_result(clean=True, mismatch_count=0,
                                               unknown_count=0, halt=False, now=NOW,
                                               path=target)
        assert excinfo.value.reason_code == "RECONCILIATION_TEMP_ARTIFACT_INVALID"
        assert bad.exists(), "an artifact of unknown origin was deleted"

    def test_a_symlink_artifact_blocks_and_is_never_followed(self, tmp_path):
        target = tmp_path / "R.json"
        outside = tmp_path.parent / "recon-precious.txt"
        outside.write_text("do not delete", encoding="utf-8")
        link = tmp_path / _temp_name(target, 999999, "d" * 32)
        os.symlink(outside, link)
        with pytest.raises(reconciliation_state.ReconciliationStateError) as excinfo:
            reconciliation_state.record_result(clean=True, mismatch_count=0,
                                               unknown_count=0, halt=False, now=NOW,
                                               path=target)
        assert excinfo.value.detail == "symlink"
        assert os.path.islink(link)
        assert outside.read_text(encoding="utf-8") == "do not delete"
        link.unlink()
        outside.unlink()

    def test_a_directory_artifact_blocks(self, tmp_path):
        target = tmp_path / "R.json"
        fake = tmp_path / _temp_name(target, 999999, "e" * 32)
        fake.mkdir()
        with pytest.raises(reconciliation_state.ReconciliationStateError) as excinfo:
            reconciliation_state.record_result(clean=True, mismatch_count=0,
                                               unknown_count=0, halt=False, now=NOW,
                                               path=target)
        assert excinfo.value.detail == "non_regular_file"
        assert fake.is_dir()
        fake.rmdir()

    def test_a_fifo_artifact_blocks(self, tmp_path):
        target = tmp_path / "R.json"
        fifo = tmp_path / _temp_name(target, 999999, "c" * 32)
        os.mkfifo(fifo)
        with pytest.raises(reconciliation_state.ReconciliationStateError):
            reconciliation_state.record_result(clean=True, mismatch_count=0,
                                               unknown_count=0, halt=False, now=NOW,
                                               path=target)
        assert stat.S_ISFIFO(os.lstat(fifo).st_mode)
        fifo.unlink()

    def test_another_snapshots_temp_is_ignored(self, tmp_path):
        target = tmp_path / "R.json"
        theirs = tmp_path / f".OTHER.json.999999.{UUID32}.tmp"
        theirs.write_text("theirs", encoding="utf-8")
        reconciliation_state.record_result(clean=True, mismatch_count=0, unknown_count=0,
                                           halt=False, now=NOW, path=target)
        assert theirs.exists()

    def test_a_blocked_cleanup_writes_nothing(self, tmp_path):
        target = tmp_path / "R.json"
        reconciliation_state.record_result(clean=True, mismatch_count=0, unknown_count=0,
                                           halt=False, now=NOW, path=target)
        before = target.read_bytes()
        bad = tmp_path / f".R.json.notpid.{UUID32}.tmp"
        bad.write_text("junk", encoding="utf-8")
        with pytest.raises(reconciliation_state.ReconciliationStateError):
            reconciliation_state.record_result(clean=False, mismatch_count=7,
                                               unknown_count=0, halt=False, now=NOW,
                                               path=target)
        assert target.read_bytes() == before
        bad.unlink()

    def test_an_unremovable_orphan_blocks_the_write(self, tmp_path, monkeypatch):
        target = tmp_path / "R.json"
        reconciliation_state.record_result(clean=True, mismatch_count=0, unknown_count=0,
                                           halt=False, now=NOW, path=target)
        before = target.read_bytes()
        (tmp_path / _temp_name(target, 999999)).write_text("partial", encoding="utf-8")
        monkeypatch.setattr(reconciliation_state.os, "unlink",
                            lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EACCES, "no")))
        with pytest.raises(reconciliation_state.ReconciliationStateError) as excinfo:
            reconciliation_state.record_result(clean=False, mismatch_count=1,
                                               unknown_count=0, halt=False, now=NOW,
                                               path=target)
        assert excinfo.value.reason_code == "RECONCILIATION_STALE_TEMP_CLEANUP_FAILED"
        assert target.read_bytes() == before

    def test_the_cleanup_runs_inside_the_writer_lock(self):
        source = (REPO_ROOT / "reconciliation" / "reconciliation_state.py").read_text(
            encoding="utf-8")
        lock_at = source.index("fcntl.flock(lock_handle, fcntl.LOCK_EX)")
        cleanup_at = source.index("_cleanup_stale_temps(target)", lock_at)
        replace_at = source.index("os.replace(temp_path, target)")
        unlock_at = source.index("fcntl.flock(lock_handle, fcntl.LOCK_UN)")
        assert lock_at < cleanup_at < replace_at < unlock_at


_CRASH_BEFORE_REPLACE = textwrap.dedent(
    """
    import os, signal, sys
    sys.path.insert(0, sys.argv[1])
    from reconciliation import reconciliation_state
    reconciliation_state.os.replace = lambda *a, **k: os.kill(os.getpid(), signal.SIGKILL)
    from datetime import datetime, timezone
    reconciliation_state.record_result(
        clean=False, mismatch_count=3, unknown_count=0, halt=False,
        now=datetime.now(timezone.utc), path=__import__("pathlib").Path(sys.argv[2]))
    """
)

_CRASH_AFTER_REPLACE = textwrap.dedent(
    """
    import os, signal, sys
    sys.path.insert(0, sys.argv[1])
    from reconciliation import reconciliation_state
    real_open = os.open
    def _open(path, *a, **k):
        import pathlib
        if pathlib.Path(str(path)).is_dir():
            os.kill(os.getpid(), signal.SIGKILL)
        return real_open(path, *a, **k)
    reconciliation_state.os.open = _open
    from datetime import datetime, timezone
    reconciliation_state.record_result(
        clean=False, mismatch_count=3, unknown_count=0, halt=False,
        now=datetime.now(timezone.utc), path=__import__("pathlib").Path(sys.argv[2]))
    """
)


class TestRealSigkillRecovery:
    def test_a_crash_before_replace_preserves_the_snapshot(self, tmp_path):
        target = tmp_path / "R.json"
        reconciliation_state.record_result(clean=True, mismatch_count=0, unknown_count=0,
                                           halt=False, now=NOW, path=target)
        good = target.read_bytes()
        result = subprocess.run(
            [sys.executable, "-c", _CRASH_BEFORE_REPLACE, str(REPO_ROOT), str(target)],
            capture_output=True, text=True, timeout=120)
        assert result.returncode != 0
        assert target.read_bytes() == good
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
        assert len(leftovers) == 1, leftovers

    def test_the_next_write_cleans_up_after_the_crash(self, tmp_path):
        target = tmp_path / "R.json"
        reconciliation_state.record_result(clean=True, mismatch_count=0, unknown_count=0,
                                           halt=False, now=NOW, path=target)
        subprocess.run(
            [sys.executable, "-c", _CRASH_BEFORE_REPLACE, str(REPO_ROOT), str(target)],
            capture_output=True, text=True, timeout=120)
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]

        reconciliation_state.record_result(clean=True, mismatch_count=0, unknown_count=0,
                                           halt=False, now=NOW, path=target)
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []
        assert freshness.evaluate(path=target, now=NOW).clean is True

    def test_a_crash_after_replace_leaves_the_new_snapshot(self, tmp_path):
        """Durability is unknown, so the next reconciliation must run
        before anything is armed -- but nothing is rolled back."""
        target = tmp_path / "R.json"
        reconciliation_state.record_result(clean=True, mismatch_count=0, unknown_count=0,
                                           halt=False, now=NOW, path=target)
        old = target.read_bytes()
        result = subprocess.run(
            [sys.executable, "-c", _CRASH_AFTER_REPLACE, str(REPO_ROOT), str(target)],
            capture_output=True, text=True, timeout=120)
        assert result.returncode != 0
        assert target.read_bytes() != old, "the replace did not land"
        assert json.loads(target.read_text(encoding="utf-8"))["mismatch_count"] == 3
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []

    def test_a_later_write_restores_a_usable_snapshot(self, tmp_path):
        target = tmp_path / "R.json"
        reconciliation_state.record_result(clean=True, mismatch_count=0, unknown_count=0,
                                           halt=False, now=NOW, path=target)
        subprocess.run(
            [sys.executable, "-c", _CRASH_AFTER_REPLACE, str(REPO_ROOT), str(target)],
            capture_output=True, text=True, timeout=120)
        reconciliation_state.record_result(clean=True, mismatch_count=0, unknown_count=0,
                                           halt=False, now=NOW, path=target)
        assert freshness.evaluate(path=target, now=NOW).clean is True


class TestWriterSchemaRegression:
    def test_the_output_still_satisfies_the_reader(self, tmp_path):
        target = tmp_path / "R.json"
        reconciliation_state.record_result(clean=True, mismatch_count=0, unknown_count=0,
                                           halt=False, now=NOW, path=target)
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["schema_version"] == freshness.SCHEMA_VERSION
        assert type(payload["clean"]) is bool
        assert type(payload["mismatch_count"]) is int
        assert type(payload["unknown_count"]) is int
        assert type(payload["halt"]) is bool
        assert freshness.validate_schema(payload) is payload

    def test_the_snapshot_is_not_world_readable(self, tmp_path):
        target = tmp_path / "R.json"
        reconciliation_state.record_result(clean=True, mismatch_count=0, unknown_count=0,
                                           halt=False, now=NOW, path=target)
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
