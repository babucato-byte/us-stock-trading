"""Stage 4 (roadmap Phase 5) / CODEX-028: position store tests. All paths
redirected to tmp_path -- never touches the real repo root.

Since CODEX-028, SQLite (STATE_STORE_DB_FILE) is the canonical store and
POSITION_STORE.json is a best-effort projection of it -- every test here
isolates BOTH env vars. (A prior version of this fixture isolated only
POSITION_STORE_FILE, which meant every test in this file was silently
writing real positions into the repo-root TRADING_STATE.db once SQLite
became canonical -- caught and fixed here.)"""
import json
import sqlite3

import pytest

from positions import states, store
from state_store import db as state_db


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    yield


def _db_conn():
    return state_db.open_db()


def test_load_all_empty_when_file_missing():
    assert store.load_all() == {}
    assert store.load_position("does-not-exist") is None


def test_create_and_load_position_round_trips():
    record = store.create_position("VWAP_MICRO_PULLBACK_MOMENTUM_V1", "1.0", "AAPL", "coid-1", 10)
    assert record["state"] == states.SETUP_DETECTED
    assert record["remaining_qty"] == 0

    loaded = store.load_position(record["position_id"])
    assert loaded == record


def test_save_position_persists_updates():
    record = store.create_position("VWAP_MICRO_PULLBACK_MOMENTUM_V1", "1.0", "AAPL", "coid-1", 10)
    record["state"] = states.ARMED
    record["state_history"].append({"state": states.ARMED, "at": "t", "reason": "armed"})
    store.save_position(record)

    loaded = store.load_position(record["position_id"])
    assert loaded["state"] == states.ARMED
    assert len(loaded["state_history"]) == 2


def test_save_position_unknown_id_raises():
    with pytest.raises(store.PositionStoreError):
        store.save_position({"position_id": "not-real", "state": states.ARMED})


def test_multiple_positions_independent():
    a = store.create_position("S", "1.0", "AAPL", "coid-a", 10)
    b = store.create_position("S", "1.0", "MSFT", "coid-b", 5)
    all_positions = store.load_all()
    assert set(all_positions) == {a["position_id"], b["position_id"]}
    assert all_positions[a["position_id"]]["symbol"] == "AAPL"
    assert all_positions[b["position_id"]]["symbol"] == "MSFT"


def test_load_non_terminal_excludes_closed_positions():
    a = store.create_position("S", "1.0", "AAPL", "coid-a", 10)
    b = store.create_position("S", "1.0", "MSFT", "coid-b", 5)
    b["state"] = states.CLOSED
    store.save_position(b)

    non_terminal = store.load_non_terminal()
    assert set(non_terminal) == {a["position_id"]}


# ---------------------------------------------------------------------------
# CODEX-028: SQLite (not POSITION_STORE.json) is now canonical -- these
# tests corrupt the SQLite database file, not the JSON projection.
# Corrupting POSITION_STORE.json alone must NOT surface as store
# corruption anymore: it is a regenerable projection, never authoritative
# (see test_corrupted_json_projection_alone_is_not_store_corruption below).
# ---------------------------------------------------------------------------

def _db_path(tmp_path):
    db_path = tmp_path / "TEST_STATE.db"
    return db_path


def _corrupt_db_file(tmp_path):
    """Overwrite the SQLite database file with bytes that are not a valid
    SQLite file at all -- the strongest form of corruption, guaranteed to
    fail on first read regardless of which table/row is touched."""
    db_path = _db_path(tmp_path)
    db_path.write_bytes(b"not a sqlite database, just garbage bytes" * 50)


def test_corrupted_db_file_fails_closed_not_silently_empty(tmp_path):
    _corrupt_db_file(tmp_path)

    # A position we know the id of must come back RECOVERY_REQUIRED, not None.
    record = store.load_position("some-id")
    assert record is not None
    assert record["state"] == states.RECOVERY_REQUIRED

    # And a brand-new create must refuse rather than silently overwrite a
    # database we can't trust.
    with pytest.raises(store.PositionStoreError):
        store.create_position("S", "1.0", "AAPL", "coid-x", 1)


def test_corrupted_json_projection_alone_is_not_store_corruption(tmp_path, monkeypatch):
    """CODEX-028's whole point: POSITION_STORE.json is a projection, not
    the source of truth. Corrupting only the JSON file (leaving SQLite
    intact) must not raise PositionStoreCorruptedError anywhere -- reads
    go straight to SQLite and never look at the JSON file at all."""
    record = store.create_position("S", "1.0", "AAPL", "coid-1", 1)
    store_path = tmp_path / "POSITION_STORE.json"
    store_path.write_text("{not valid json at all")

    loaded = store.load_position(record["position_id"])
    assert loaded["state"] == states.SETUP_DETECTED  # read from SQLite, unaffected
    assert store.load_all() == {record["position_id"]: loaded}
    # A regenerated projection overwrites the corrupted JSON with a fresh,
    # valid one derived entirely from SQLite (requirement #10).
    store.regenerate_projection()
    assert json.loads(store_path.read_text())["positions"][record["position_id"]]["state"] == states.SETUP_DETECTED


# ---------------------------------------------------------------------------
# CODEX-025 (ported to the SQLite layer by CODEX-028): load_all()/
# load_non_terminal() fail closed on whole-database corruption instead of
# silently returning {} (was indistinguishable from a legitimately
# empty/fresh store).
# ---------------------------------------------------------------------------

def test_load_all_raises_on_corrupted_db_not_silently_empty(tmp_path):
    _corrupt_db_file(tmp_path)

    with pytest.raises(store.PositionStoreCorruptedError):
        store.load_all()


def test_load_non_terminal_raises_on_corrupted_db(tmp_path):
    _corrupt_db_file(tmp_path)

    with pytest.raises(store.PositionStoreCorruptedError):
        store.load_non_terminal()


def test_load_all_raises_on_schema_mismatch(tmp_path):
    db_path = _db_path(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE positions (position_id TEXT PRIMARY KEY)")  # missing every other column
    conn.commit()
    conn.close()

    with pytest.raises(store.PositionStoreCorruptedError):
        store.load_all()


def test_load_all_succeeds_on_legitimately_empty_store(tmp_path):
    # No database file at all -- a fresh install, must not be conflated with corruption.
    assert store.load_all() == {}
    assert store.load_non_terminal() == {}


# ---------------------------------------------------------------------------
# CODEX-025/CODEX-028: check_store_health() diagnostic classification
# ---------------------------------------------------------------------------

def test_check_store_health_missing(tmp_path):
    health = store.check_store_health()
    assert health["status"] == store.STORE_STATUS_MISSING


def test_check_store_health_valid_empty(tmp_path):
    store.create_position("S", "1.0", "AAPL", "coid-1", 1)
    conn = state_db.open_db()
    conn.execute("DELETE FROM position_events")
    conn.execute("DELETE FROM positions")
    conn.commit()
    conn.close()
    health = store.check_store_health()
    assert health["status"] == store.STORE_STATUS_VALID_EMPTY


def test_check_store_health_valid_with_positions(tmp_path):
    store.create_position("S", "1.0", "AAPL", "coid-1", 1)
    health = store.check_store_health()
    assert health["status"] == store.STORE_STATUS_VALID_WITH_POSITIONS


def test_check_store_health_corrupted(tmp_path):
    _corrupt_db_file(tmp_path)
    health = store.check_store_health()
    assert health["status"] == store.STORE_STATUS_CORRUPTED


def test_check_store_health_schema_mismatch(tmp_path):
    db_path = _db_path(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE positions (position_id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    health = store.check_store_health()
    assert health["status"] == store.STORE_STATUS_SCHEMA_MISMATCH


def test_check_store_health_permission_error(tmp_path):
    store.create_position("S", "1.0", "AAPL", "coid-1", 1)
    db_path = _db_path(tmp_path)
    db_path.chmod(0o000)
    try:
        health = store.check_store_health()
        assert health["status"] in (store.STORE_STATUS_READ_FAILURE, store.STORE_STATUS_CORRUPTED)
    finally:
        db_path.chmod(0o644)  # restore so tmp_path cleanup can remove it


def test_check_store_health_partial_truncated_file(tmp_path):
    store.create_position("S", "1.0", "AAPL", "coid-1", 1)
    db_path = _db_path(tmp_path)
    full_content = db_path.read_bytes()
    db_path.write_bytes(full_content[: len(full_content) // 2])  # truncate mid-write
    health = store.check_store_health()
    assert health["status"] == store.STORE_STATUS_CORRUPTED


def test_record_with_unrecognized_state_fails_closed(tmp_path):
    base = store.create_position("S", "1.0", "AAPL", "coid-1", 1)
    conn = state_db.open_db()
    conn.execute(
        "UPDATE positions SET state = 'TOTALLY_MADE_UP_STATE' WHERE position_id = ?",
        (base["position_id"],),
    )
    conn.commit()
    conn.close()

    record = store.load_position(base["position_id"])
    assert record["state"] == states.RECOVERY_REQUIRED
    # A nonexistent position_id in the same database is unaffected -- this
    # is a per-record fail-closed, not a whole-database one.
    assert store.load_position("pos_does_not_exist") is None


def test_lock_file_left_by_dead_process_does_not_block_next_writer(tmp_path, monkeypatch):
    # Simulate a stale lock file (no live holder) -- flock is per-fd, so a
    # freshly-opened lock file with no other process holding it acquires
    # immediately regardless of the file's mere existence/content.
    store_path = tmp_path / "POSITION_STORE.json"
    monkeypatch.setenv("POSITION_STORE_FILE", str(store_path))
    lock_path = store_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("stale")

    record = store.create_position("S", "1.0", "AAPL", "coid-1", 1)
    assert store.load_position(record["position_id"]) is not None


def test_locked_position_persists_mutation_on_clean_exit():
    record = store.create_position("S", "1.0", "AAPL", "coid-1", 10)
    with store.locked_position(record["position_id"]) as locked:
        assert locked["state"] == states.SETUP_DETECTED
        states.validate_transition(locked["state"], states.ARMED)
        locked["state"] = states.ARMED
        locked["state_history"].append({"state": states.ARMED, "at": "t", "reason": "test"})

    reloaded = store.load_position(record["position_id"])
    assert reloaded["state"] == states.ARMED


def test_locked_position_does_not_persist_on_exception():
    record = store.create_position("S", "1.0", "AAPL", "coid-1", 10)
    with pytest.raises(RuntimeError):
        with store.locked_position(record["position_id"]) as locked:
            locked["state"] = states.ARMED
            raise RuntimeError("simulated broker failure mid-transition")

    reloaded = store.load_position(record["position_id"])
    assert reloaded["state"] == states.SETUP_DETECTED  # unchanged


def test_locked_position_unknown_id_raises():
    with pytest.raises(store.PositionStoreError):
        with store.locked_position("not-a-real-id"):
            pass


def test_locked_position_serializes_concurrent_callers(tmp_path, monkeypatch):
    import threading
    import time as time_module

    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    record = store.create_position("S", "1.0", "AAPL", "coid-1", 10)
    position_id = record["position_id"]

    order = []
    barrier = threading.Barrier(2)

    def holder():
        with store.locked_position(position_id) as locked:
            barrier.wait(timeout=2)
            order.append("holder-acquired")
            time_module.sleep(0.2)  # hold the lock long enough for the second thread to block
            locked["state"] = states.ARMED
            locked["state_history"].append({"state": states.ARMED, "at": "t", "reason": "holder"})
            order.append("holder-released")

    def waiter():
        barrier.wait(timeout=2)
        time_module.sleep(0.05)  # ensure holder has the lock first
        with store.locked_position(position_id) as locked:
            order.append("waiter-acquired")
            assert locked["state"] == states.ARMED  # sees holder's committed change

    t1 = threading.Thread(target=holder)
    t2 = threading.Thread(target=waiter)
    t1.start()
    t2.start()
    t1.join(timeout=3)
    t2.join(timeout=3)

    assert order == ["holder-acquired", "holder-released", "waiter-acquired"]


def test_store_file_never_created_at_real_repo_root():
    # The autouse fixture points POSITION_STORE_FILE and STATE_STORE_DB_FILE
    # at tmp_path for every test in this module; assert neither real
    # repo-root default path was touched as a side effect.
    store.create_position("S", "1.0", "AAPL", "coid-1", 1)
    assert not store.STORE_FILE.exists()
    assert not state_db.DEFAULT_DB_FILE.exists()


# ---------------------------------------------------------------------------
# CODEX-028: SQLite commit succeeds independently of JSON projection
# outcome, and a mid-transaction SQLite failure leaves everything
# unchanged (never a partial write).
# ---------------------------------------------------------------------------

def test_sqlite_commit_succeeds_even_when_json_projection_write_fails(tmp_path, monkeypatch):
    record = store.create_position("S", "1.0", "AAPL", "coid-1", 10)

    def _boom(path, payload):
        raise OSError("simulated disk full during JSON projection write")

    monkeypatch.setattr(store, "_atomic_write", _boom)
    with store.locked_position(record["position_id"]) as locked:
        states.validate_transition(locked["state"], states.ARMED)
        locked["state"] = states.ARMED
        locked["state_history"].append({"state": states.ARMED, "at": "t", "reason": "armed"})

    # SQLite (canonical) reflects the change regardless of the JSON failure.
    reloaded = store.load_position(record["position_id"])
    assert reloaded["state"] == states.ARMED

    conn = state_db.open_db()
    row = conn.execute(
        "SELECT projection_status FROM positions WHERE position_id = ?", (record["position_id"],)
    ).fetchone()
    conn.close()
    assert row["projection_status"] == "FAILED"


def test_db_commit_failure_leaves_position_state_entirely_unchanged(tmp_path, monkeypatch):
    """A mid-transaction DB error (simulated here as the position_events
    INSERT step failing) must roll back the position row UPSERT that
    preceded it in the same transaction -- position scalar fields and
    state_history are never allowed to diverge (CODEX-028 requirement #6/8)."""
    record = store.create_position("S", "1.0", "AAPL", "coid-1", 10)

    def _boom(conn, position_id, new_events):
        raise sqlite3.OperationalError("simulated disk I/O error mid-transaction")

    monkeypatch.setattr(store, "_insert_new_events", _boom)
    with pytest.raises(sqlite3.OperationalError):
        with store.locked_position(record["position_id"]) as locked:
            locked["remaining_qty"] = 999  # would-be scalar change
            states.validate_transition(locked["state"], states.ARMED)
            locked["state"] = states.ARMED
            locked["state_history"].append({"state": states.ARMED, "at": "t", "reason": "armed"})

    reloaded = store.load_position(record["position_id"])
    assert reloaded["state"] == states.SETUP_DETECTED  # entirely unchanged, not partially applied
    assert reloaded["remaining_qty"] == 0  # scalar change rolled back together with the failed event insert


def test_regenerate_projection_rebuilds_json_from_sqlite(tmp_path):
    a = store.create_position("S", "1.0", "AAPL", "coid-a", 10)
    b = store.create_position("S", "1.0", "MSFT", "coid-b", 5)
    store_path = tmp_path / "POSITION_STORE.json"
    store_path.write_text("garbage, not json")

    payload = store.regenerate_projection()
    assert set(payload["positions"]) == {a["position_id"], b["position_id"]}
    on_disk = json.loads(store_path.read_text())
    assert set(on_disk["positions"]) == {a["position_id"], b["position_id"]}


def test_regenerate_projection_raises_on_corrupted_db(tmp_path):
    _corrupt_db_file(tmp_path)
    with pytest.raises(store.PositionStoreCorruptedError):
        store.regenerate_projection()
