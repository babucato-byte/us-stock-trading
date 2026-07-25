"""Stage 4 (roadmap Phase 5): position store tests. All paths redirected to
tmp_path -- never touches the real repo root."""
import json

import pytest

from positions import states, store


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    yield


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


def test_corrupted_json_file_fails_closed_not_silently_empty(tmp_path, monkeypatch):
    store_path = tmp_path / "POSITION_STORE.json"
    monkeypatch.setenv("POSITION_STORE_FILE", str(store_path))
    store_path.write_text("{not valid json")

    # A position we know the id of must come back RECOVERY_REQUIRED, not None.
    record = store.load_position("some-id")
    assert record is not None
    assert record["state"] == states.RECOVERY_REQUIRED

    # And a brand-new create must refuse rather than silently overwrite a
    # file we can't trust.
    with pytest.raises(store.PositionStoreError):
        store.create_position("S", "1.0", "AAPL", "coid-x", 1)


def test_record_missing_required_field_fails_closed(tmp_path, monkeypatch):
    store_path = tmp_path / "POSITION_STORE.json"
    monkeypatch.setenv("POSITION_STORE_FILE", str(store_path))
    payload = {"positions": {"pos_1": {"position_id": "pos_1", "symbol": "AAPL"}}}
    store_path.write_text(json.dumps(payload))

    record = store.load_position("pos_1")
    assert record["state"] == states.RECOVERY_REQUIRED
    # Other (nonexistent) positions in the same file are unaffected -- this
    # is a per-record fail-closed, not a whole-file one.
    assert store.load_position("pos_2") is None


def test_record_with_unrecognized_state_fails_closed(tmp_path, monkeypatch):
    store_path = tmp_path / "POSITION_STORE.json"
    monkeypatch.setenv("POSITION_STORE_FILE", str(store_path))
    base = store.create_position("S", "1.0", "AAPL", "coid-1", 1)
    payload = store._read_raw()
    payload["positions"][base["position_id"]]["state"] = "TOTALLY_MADE_UP_STATE"
    store_path.write_text(json.dumps(payload))

    record = store.load_position(base["position_id"])
    assert record["state"] == states.RECOVERY_REQUIRED


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
    # The autouse fixture points POSITION_STORE_FILE at tmp_path for every
    # test in this module; assert the real repo-root default path was
    # never touched as a side effect.
    store.create_position("S", "1.0", "AAPL", "coid-1", 1)
    assert not store.STORE_FILE.exists()
