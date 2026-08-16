"""Atomic, lock-protected CSV read/write for the scalping watchlist files.

Reimplements the same technique paper_strategy_order.py uses for
order_history.csv / order_reconciliation.csv (temp file + flush + fsync +
os.replace, guarded by fcntl.flock) as an independent, small module — Phase
1's order-execution files are not imported from or modified by Phase 2
(instructions section 12). See DECISION_LOG.md for why the pattern is
reused but not the code.
"""

import fcntl
import os
import tempfile
import time
from contextlib import contextmanager

import pandas as pd

DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0


@contextmanager
def file_lock(lock_path, timeout=DEFAULT_LOCK_TIMEOUT_SECONDS):
    """Process-level exclusive lock via fcntl.flock (macOS/Ubuntu only).

    Crash recovery is intentional, not incidental: fcntl.flock locks are
    held by the kernel against the process's open file descriptors, so a
    process that dies for any reason (including SIGKILL, before it can run
    its `finally` block or delete `lock_path`) has every lock it held
    released automatically on exit. A `.lock` file left behind by a dead
    process therefore never blocks the next acquirer — the retry loop
    below simply succeeds on its next attempt instead of waiting out
    `timeout`. If the lock is genuinely still held (owner process alive),
    acquisition fails closed with a raised exception once `timeout`
    elapses, rather than blocking forever.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "a+")
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Could not acquire lock ({lock_path}) within {timeout}s")
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        finally:
            lock_file.close()


def _discard_stale_tmp_files(path):
    """Remove leftover `.{stem}_*.tmp` files for `path` from a previous
    write that crashed before its os.replace() ran. Such a file was never
    fsynced-and-renamed into place, so it cannot be valid data — only
    clutter that must never be mistaken for, or promoted to, the real
    file. Safe to call while holding `path`'s lock, since that is the same
    lock every writer of `path` is required to hold before calling
    atomic_write_csv."""
    for stale in path.parent.glob(f".{path.stem}_*.tmp"):
        try:
            os.remove(stale)
        except OSError:
            pass


def atomic_write_csv(path, dataframe):
    """Write `dataframe` to `path` atomically. Returns True/False; never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _discard_stale_tmp_files(path)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", newline="") as tmp_file:
                dataframe.to_csv(tmp_file, index=False)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            os.replace(tmp_name, path)
        except Exception:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
            raise
        return True
    except Exception as exc:
        print(f"Failed to save {path}: {exc}")
        return False


class FileUnavailable(Exception):
    """A tracked CSV exists but could not be safely read (fail-closed)."""


def read_csv_fail_closed(path, required_columns):
    """Read a CSV that must exist and have the given columns, or raise.

    Mirrors order_history.csv's fail-closed policy (CODEX-002/007): a
    missing or corrupted file must never be silently treated as empty when
    the caller needs to know the current persisted state.
    """
    if not path.exists():
        raise FileUnavailable(f"MISSING: {path} does not exist")
    try:
        df = pd.read_csv(path)
    except Exception as exc:
        raise FileUnavailable(f"CORRUPTED: failed to parse {path}: {exc}")
    missing = [c for c in required_columns if c not in df.columns]
    if missing:
        raise FileUnavailable(f"CORRUPTED: {path} is missing required columns {missing}")
    return df.astype({c: "object" for c in required_columns})


def read_csv_or_empty(path, columns):
    """Read a CSV, returning an empty (but correctly-shaped) frame if it
    does not exist yet — used for state that legitimately starts empty
    (e.g. the repeat-tracker before any symbol has ever been detected)."""
    if not path.exists():
        return pd.DataFrame(columns=columns).astype({c: "object" for c in columns})
    df = read_csv_fail_closed(path, columns)
    return df
