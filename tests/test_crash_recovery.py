"""Crash-recovery tests for scalping_watchlist's atomic file I/O.

Covers what must happen after an abnormal termination leaves behind a
`.lock` file whose owner is gone, and/or a `.tmp` file whose write never
finished: the next run must never block indefinitely, must never adopt the
stale temp file as real data, and must never guess values for a corrupted
data file. All fixtures use tmp_path only — nothing outside the pytest
temp directory is touched.
"""

import fcntl
import multiprocessing
import os
import time

import pandas as pd
import pytest

from scalping_watchlist import repository
from scalping_watchlist.atomic_io import (
    FileUnavailable,
    atomic_write_csv,
    file_lock,
    read_csv_fail_closed,
    read_csv_or_empty,
)


# ---------------------------------------------------------------------------
# 1. Stale .lock left by a crashed process: finite-time reclaim, no hang.
# ---------------------------------------------------------------------------

def _hold_lock_then_die(lock_path, ready_path):
    """Child process: acquire the flock, signal readiness, then sit idle
    without ever releasing the lock, deleting the lock file, or otherwise
    cleaning up. The test kills this process with SIGKILL afterwards so it
    never gets a chance to run any cleanup at all — simulating a hard
    crash."""
    lock_file = open(lock_path, "a+")
    fcntl.flock(lock_file, fcntl.LOCK_EX)
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    ready_path.write_text("ready")
    time.sleep(60)


def test_stale_lock_from_crashed_process_is_reclaimed_without_indefinite_wait(tmp_path):
    lock_path = tmp_path / "watchlist.lock"
    ready_path = tmp_path / "ready.flag"

    ctx = multiprocessing.get_context("fork")
    proc = ctx.Process(target=_hold_lock_then_die, args=(lock_path, ready_path))
    proc.start()
    try:
        deadline = time.monotonic() + 5.0
        while not ready_path.exists():
            assert time.monotonic() < deadline, "child never signalled that it held the lock"
            time.sleep(0.02)

        # SIGKILL: no finally block, no LOCK_UN, no unlink — a hard crash,
        # not a clean shutdown.
        os.kill(proc.pid, 9)
        proc.join(timeout=5.0)
        assert not proc.is_alive()

        started = time.monotonic()
        with file_lock(lock_path, timeout=3.0):
            pass
        elapsed = time.monotonic() - started

        # The kernel releases the dead process's flock on exit, so the next
        # acquirer succeeds almost immediately — it must not sit out the
        # full 3s timeout waiting for a lock nobody holds anymore.
        assert elapsed < 1.5
    finally:
        if proc.is_alive():
            proc.terminate()
            proc.join()


def test_lock_still_held_by_a_live_process_fails_closed_within_timeout(tmp_path):
    lock_path = tmp_path / "watchlist.lock"
    held = open(lock_path, "a+")
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="Could not acquire lock"):
            with file_lock(lock_path, timeout=0.3):
                pass
        elapsed = time.monotonic() - started
        assert elapsed < 2.0  # bounded wait, not an indefinite block
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


# ---------------------------------------------------------------------------
# 2. Leftover .tmp from an interrupted write: discarded, never promoted.
# ---------------------------------------------------------------------------

def test_leftover_tmp_file_is_discarded_and_never_promoted(tmp_path):
    target = tmp_path / "scalping_watchlist.csv"
    stray_tmp = tmp_path / f".{target.stem}_deadbeef.tmp"
    stray_tmp.write_text("this,is,garbage,left,by,a,crash\n1,2,3,4,5,6,7\n")

    good = pd.DataFrame([{"symbol": "AAPL", "status": "NEW"}])
    assert atomic_write_csv(target, good) is True

    # The stray temp file must not have been renamed into place, or left
    # behind to confuse a future run.
    assert not stray_tmp.exists()
    written = pd.read_csv(target)
    assert list(written.columns) == ["symbol", "status"]
    assert written.iloc[0]["symbol"] == "AAPL"


def test_leftover_tmp_file_is_never_read_as_final_data(tmp_path):
    target = tmp_path / "scalping_watchlist.csv"
    stray_tmp = tmp_path / f".{target.stem}_abc123.tmp"
    stray_tmp.write_text("symbol,status\nEVIL,ACTIVE\n")

    # No real file was ever committed (the crash happened before
    # os.replace) — this must read as legitimately missing/empty, never as
    # the tmp file's content.
    assert read_csv_or_empty(target, ["symbol", "status"]).empty
    with pytest.raises(FileUnavailable, match="MISSING"):
        read_csv_fail_closed(target, ["symbol", "status"])


# ---------------------------------------------------------------------------
# 3. Partially-written / corrupted data file: fail-closed, no guessing.
# ---------------------------------------------------------------------------

def test_partially_written_corrupted_file_raises_instead_of_guessing(tmp_path):
    target = tmp_path / "scalping_watchlist.csv"
    # Unterminated quoted field: exactly what a write cut off mid-fsync
    # (bypassing the atomic-replace protection some other way) would leave
    # behind — the CSV parser cannot make sense of it.
    target.write_text('symbol,status,scalping_score\nAAPL,"NEW\n')

    with pytest.raises(FileUnavailable, match="CORRUPTED"):
        read_csv_fail_closed(target, ["symbol", "status", "scalping_score"])


def test_corrupted_watchlist_file_is_refused_not_reinitialized(monkeypatch, tmp_path):
    monkeypatch.setattr(repository, "WATCHLIST_FILE", tmp_path / "scalping_watchlist.csv")
    monkeypatch.setattr(repository, "WATCHLIST_LOCK_FILE", tmp_path / "scalping_watchlist.lock")
    repository.WATCHLIST_FILE.write_text("totally,wrong,columns\n1,2,3\n")

    with pytest.raises(repository.WatchlistUnavailable):
        repository.load_watchlist()


# ---------------------------------------------------------------------------
# 4. Normal path is unaffected by the crash-recovery additions.
# ---------------------------------------------------------------------------

def test_normal_write_then_read_roundtrip_is_unaffected(tmp_path):
    target = tmp_path / "scalping_watchlist.csv"
    df = pd.DataFrame([
        {"symbol": "AAPL", "status": "NEW"},
        {"symbol": "MSFT", "status": "ACTIVE"},
    ])

    assert atomic_write_csv(target, df) is True
    reread = read_csv_fail_closed(target, ["symbol", "status"])

    assert reread["symbol"].tolist() == ["AAPL", "MSFT"]
    assert reread["status"].tolist() == ["NEW", "ACTIVE"]


def test_repository_load_missing_watchlist_is_still_legitimate_empty_state(monkeypatch, tmp_path):
    monkeypatch.setattr(repository, "WATCHLIST_FILE", tmp_path / "scalping_watchlist.csv")
    monkeypatch.setattr(repository, "WATCHLIST_LOCK_FILE", tmp_path / "scalping_watchlist.lock")

    df = repository.load_watchlist()

    assert df.empty
