"""Idempotency / duplicate-order prevention (spec §17), backed by the
`kis_order_idempotency` table (state_store migration 6). This is the
durable check execution_engine.py runs BEFORE ever calling
KISBroker.submit_order() -- a process restart, a caller retry after a
network timeout, or a duplicate strategy signal must all resolve to "an
attempt for this key already exists", never a second real order.

Two independent uniqueness guards, matching the table's constraints:
  - `internal_order_id` (this codebase's own id, generated before the
    broker call) -- catches a literal retry of the exact same attempt.
  - `(signal_id, symbol, side, trading_date)` -- catches a *different*
    internal_order_id being generated for what is, in trading terms, the
    same signal trying to buy/sell the same symbol on the same day
    (e.g. a crashed process restarting and re-running the same
    already-attempted signal through the pipeline from scratch).

A single-process-instance file lock (mirrors live_readiness/
entry_reservation_ledger.py's reservation_lock()) additionally guards
against two concurrently-running instances of this same trading process
racing each other -- spec §17's "동일 실행 프로세스가 중복 실행되지
않도록 단일 실행 잠금을 유지한다".
"""

import fcntl
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
_LOCK_FILE = BASE_DIR / "KIS_ORDER_IDEMPOTENCY.lock"
LOCK_TIMEOUT_SECONDS = 5.0


class IdempotencyError(Exception):
    """Raised when the lock cannot be acquired, or (via the specific
    DuplicateOrderAttemptError subclass) when a duplicate is detected."""


class DuplicateOrderAttemptError(IdempotencyError):
    """Raised when an order attempt for an already-recorded key is
    detected. Callers must treat this as a hard block -- never as a
    signal to retry with a fresh internal_order_id (that would defeat
    the (signal_id, symbol, side, trading_date) guard entirely)."""

    def __init__(self, message, *, existing_row):
        super().__init__(message)
        self.existing_row = existing_row


@contextmanager
def single_run_lock(timeout=LOCK_TIMEOUT_SECONDS):
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(_LOCK_FILE, "a+")
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise IdempotencyError(
                        f"Could not acquire KIS order idempotency lock within {timeout}s -- "
                        "another instance of this process may already be running."
                    )
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        finally:
            lock_file.close()


def find_existing(conn, *, internal_order_id, signal_id, symbol, side, trading_date):
    """Read-only lookup -- returns the existing row (sqlite3.Row) if
    either uniqueness key already has an attempt recorded, else None.
    Callers should still call register() inside the same lock span
    rather than relying on this alone (this is advisory for a clear
    caller-facing check; register()'s INSERT is the actual atomic
    guarantee)."""
    row = conn.execute(
        "SELECT * FROM kis_order_idempotency WHERE internal_order_id = ?",
        (internal_order_id,),
    ).fetchone()
    if row is not None:
        return row
    return conn.execute(
        "SELECT * FROM kis_order_idempotency WHERE signal_id = ? AND symbol = ? "
        "AND side = ? AND trading_date = ?",
        (signal_id, symbol, side, trading_date),
    ).fetchone()


def register(conn, *, internal_order_id, signal_id, symbol, side, trading_date, status="CREATED", commit=True):
    """Atomically records a new order attempt. Raises
    DuplicateOrderAttemptError (via the table's UNIQUE constraints) if
    either key already exists -- callers must call this BEFORE
    KISBroker.submit_order(), inside single_run_lock()."""
    existing = find_existing(
        conn, internal_order_id=internal_order_id, signal_id=signal_id,
        symbol=symbol, side=side, trading_date=trading_date,
    )
    if existing is not None:
        raise DuplicateOrderAttemptError(
            f"order attempt already recorded for internal_order_id={internal_order_id!r} or "
            f"(signal_id={signal_id!r}, symbol={symbol!r}, side={side!r}, "
            f"trading_date={trading_date!r}): existing status={existing['status']!r}",
            existing_row=existing,
        )
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute(
            "INSERT INTO kis_order_idempotency "
            "(internal_order_id, signal_id, symbol, side, trading_date, broker_order_id, "
            "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (internal_order_id, signal_id, symbol, side, trading_date, status, now, now),
        )
    except sqlite3.IntegrityError as exc:
        # Race window between find_existing() and INSERT -- the UNIQUE
        # constraints themselves are the real atomic guarantee.
        raise DuplicateOrderAttemptError(
            f"order attempt for internal_order_id={internal_order_id!r} was recorded concurrently: {exc}",
            existing_row=find_existing(
                conn, internal_order_id=internal_order_id, signal_id=signal_id,
                symbol=symbol, side=side, trading_date=trading_date,
            ),
        ) from exc
    if commit:
        conn.commit()


def has_unknown_order(conn, *, symbol, side):
    """CODEX-044: real query backing the order gate's `has_unknown_order`
    check -- an order this system submitted but never got a definite
    ACCEPTED/REJECTED/FILLED answer for (broker timeout, ambiguous
    response) blocks any *new* order for the same symbol+side until a
    human/reconciliation resolves it. Replaces the previous
    `has_unknown_order=False` constant Codex flagged as a bypass."""
    row = conn.execute(
        "SELECT 1 FROM kis_order_idempotency WHERE symbol = ? AND side = ? AND status = 'UNKNOWN' LIMIT 1",
        (symbol, side),
    ).fetchone()
    return row is not None


def list_unknown_orders(conn):
    """CODEX-044: every order attempt still sitting in UNKNOWN status --
    the set kis_position_manager.py's periodic tick tries to resolve via
    reconciliation.order_reconciler.reconcile_unknown_order() against
    KIS's own open-order/fill history each pass."""
    return conn.execute(
        "SELECT internal_order_id, broker_order_id, symbol, side FROM kis_order_idempotency "
        "WHERE status = 'UNKNOWN'"
    ).fetchall()


def update_status(conn, internal_order_id, status, *, broker_order_id=None, commit=True):
    now = datetime.now(timezone.utc).isoformat()
    if broker_order_id is not None:
        conn.execute(
            "UPDATE kis_order_idempotency SET status = ?, broker_order_id = ?, updated_at = ? "
            "WHERE internal_order_id = ?",
            (status, broker_order_id, now, internal_order_id),
        )
    else:
        conn.execute(
            "UPDATE kis_order_idempotency SET status = ?, updated_at = ? WHERE internal_order_id = ?",
            (status, now, internal_order_id),
        )
    if commit:
        conn.commit()
