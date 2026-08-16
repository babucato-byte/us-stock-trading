"""[MEDIUM] CODEX-019: read-modify-write file locking for the JSON state
stores in kill_switch_state.py (activate()/release()) and
notification_health.py (record_success()/record_failure()).

Reuses two established patterns already in this repo instead of inventing a
third:
  - fcntl.flock-guarded lock -> reread -> merge -> write -> unlock, the same
    technique order_history.csv / order_reconciliation.csv use in
    paper_strategy_order.py and scalping_watchlist/atomic_io.py's file_lock.
  - CODEX-008's multiprocessing.Process regression pattern
    (tests/test_paper_order_execution.py): module-level, picklable child
    functions that reassign the target module's file-path attributes
    directly after a fresh import (monkeypatch does not cross process
    boundaries), synchronized against each other via a shared
    multiprocessing.Barrier so the processes actually contend for the lock
    instead of merely running back-to-back.
"""

import fcntl
import json
import multiprocessing
import os

import pytest

import kill_switch_state as kss
import notification_health as nh


def _mp_activate(state_path, state, reason, activated_by, barrier):
    import kill_switch_state as kss_child

    os.environ.pop("KILL_SWITCH_STATE_FILE", None)
    kss_child.STATE_FILE = state_path
    barrier.wait(timeout=10)
    kss_child.activate(state, reason=reason, activated_by=activated_by)


def _mp_record_failure(state_path, log_path, error_kind, barrier):
    import notification_health as nh_child

    os.environ.pop("NOTIFICATION_HEALTH_STATE_FILE", None)
    os.environ.pop("NOTIFICATION_HEALTH_LOG_FILE", None)
    # High enough that this test's handful of failures never reaches
    # threshold, so _escalate_kill_switch never touches kill_switch_state's
    # (unrelated, not-pointed-at-tmp_path-here) file.
    os.environ["NOTIFICATION_HEALTH_FAILURE_THRESHOLD"] = "1000"
    nh_child.STATE_FILE = state_path
    nh_child.LOG_FILE = log_path
    barrier.wait(timeout=10)
    nh_child.record_failure(error_kind=error_kind)


# ---------------------------------------------------------------------------
# Concurrent activate(): audit history must not lose a write
# ---------------------------------------------------------------------------

def test_concurrent_activate_preserves_every_audit_entry(tmp_path, monkeypatch):
    monkeypatch.delenv("KILL_SWITCH_STATE_FILE", raising=False)
    state_path = tmp_path / "KILL_SWITCH_STATE.json"
    monkeypatch.setattr(kss, "STATE_FILE", state_path)

    barrier = multiprocessing.Barrier(3)
    procs = [
        multiprocessing.Process(
            target=_mp_activate,
            args=(state_path, kss.ENTRY_DISABLED, f"incident-{i}", "ops1", barrier),
        )
        for i in range(3)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=15)
    for p in procs:
        assert p.exitcode == 0

    history = kss.get_history()
    assert len(history) == 3  # no lost update from concurrent activate() calls
    assert {entry["reason"] for entry in history} == {"incident-0", "incident-1", "incident-2"}
    assert kss.get_state() == kss.ENTRY_DISABLED


# ---------------------------------------------------------------------------
# Concurrent record_failure(): consecutive-failure count must not be lost
# ---------------------------------------------------------------------------

def test_concurrent_record_failure_preserves_every_increment(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTIFICATION_HEALTH_STATE_FILE", raising=False)
    monkeypatch.delenv("NOTIFICATION_HEALTH_LOG_FILE", raising=False)
    state_path = tmp_path / "NOTIFICATION_HEALTH_STATE.json"
    log_path = tmp_path / "notification_health.log"

    barrier = multiprocessing.Barrier(3)
    procs = [
        multiprocessing.Process(
            target=_mp_record_failure,
            args=(state_path, log_path, f"Error{i}", barrier),
        )
        for i in range(3)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=15)
    for p in procs:
        assert p.exitcode == 0

    record = json.loads(state_path.read_text())
    assert record["consecutive_failures"] == 3  # no lost update from concurrent record_failure() calls


# ---------------------------------------------------------------------------
# Lock timeout: original file preserved, safe failure (no lost/partial write)
# ---------------------------------------------------------------------------

def test_kill_switch_activate_lock_timeout_leaves_file_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("KILL_SWITCH_STATE_FILE", raising=False)
    state_path = tmp_path / "KILL_SWITCH_STATE.json"
    monkeypatch.setattr(kss, "STATE_FILE", state_path)
    kss.activate(kss.ENTRY_DISABLED, reason="seed", activated_by="ops1")
    original_bytes = state_path.read_bytes()

    lock_path = state_path.with_suffix(".lock")
    held = open(lock_path, "a+")
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        with pytest.raises(kss.KillSwitchStateError):
            kss.activate(kss.MANUAL_REVIEW, reason="blocked", activated_by="ops2", lock_timeout=0.2)
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()

    assert state_path.read_bytes() == original_bytes
    assert kss.get_state() == kss.ENTRY_DISABLED  # the blocked activate() never took effect


def test_notification_health_record_failure_lock_timeout_leaves_file_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTIFICATION_HEALTH_STATE_FILE", raising=False)
    monkeypatch.delenv("NOTIFICATION_HEALTH_LOG_FILE", raising=False)
    state_path = tmp_path / "NOTIFICATION_HEALTH_STATE.json"
    log_path = tmp_path / "notification_health.log"
    monkeypatch.setattr(nh, "STATE_FILE", state_path)
    monkeypatch.setattr(nh, "LOG_FILE", log_path)
    nh.record_success(status_code=200)
    original_bytes = state_path.read_bytes()

    lock_path = state_path.with_suffix(".lock")
    held = open(lock_path, "a+")
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        record = nh.record_failure(error_kind="Timeout", lock_timeout=0.2)
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()

    assert record is not None  # never raises, even on a lock timeout
    assert state_path.read_bytes() == original_bytes
    assert nh.get_record()["consecutive_failures"] == 0  # the blocked update never took effect


# ---------------------------------------------------------------------------
# Corrupted-file fail-closed behavior: no regression from the locking change
# ---------------------------------------------------------------------------

def test_corrupted_kill_switch_state_file_still_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("KILL_SWITCH_STATE_FILE", raising=False)
    state_path = tmp_path / "KILL_SWITCH_STATE.json"
    monkeypatch.setattr(kss, "STATE_FILE", state_path)
    state_path.write_text("{ not valid json ]")

    assert kss.get_state() in (kss.ALL_TRADING_DISABLED, kss.MANUAL_REVIEW)
    assert kss.is_entry_allowed() is False

    record = kss.activate(kss.ENTRY_DISABLED, reason="recover", activated_by="ops1")
    assert record["state"] == kss.ENTRY_DISABLED
    assert kss.get_state() == kss.ENTRY_DISABLED


def test_corrupted_notification_health_state_file_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTIFICATION_HEALTH_STATE_FILE", raising=False)
    monkeypatch.delenv("NOTIFICATION_HEALTH_LOG_FILE", raising=False)
    state_path = tmp_path / "NOTIFICATION_HEALTH_STATE.json"
    log_path = tmp_path / "notification_health.log"
    monkeypatch.setattr(nh, "STATE_FILE", state_path)
    monkeypatch.setattr(nh, "LOG_FILE", log_path)
    state_path.write_text("{ not valid json ]")

    assert nh.get_status() == nh.UNKNOWN  # corrupted state treated like absent, never crashes

    record = nh.record_failure(error_kind="Timeout")
    assert record["consecutive_failures"] == 1
