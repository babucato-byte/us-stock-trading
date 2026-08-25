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
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from execution import order_repository
from execution.order_repository import OrderRepositoryReadError

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LOCK_FILE = BASE_DIR / "KIS_ORDER_IDEMPOTENCY.lock"

# Module-level override point (tests patch this). The operational default
# is unchanged; see get_single_run_lock_file() for the resolution order.
_LOCK_FILE = DEFAULT_LOCK_FILE

LOCK_FILE_ENV_VAR = "TRADING_SINGLE_RUN_LOCK_FILE"
LOCK_TIMEOUT_SECONDS = 5.0

logger = logging.getLogger(__name__)


def get_single_run_lock_file():
    """CODEX-060: the single-run lock path, resolved per call rather than
    frozen at import.

    `TRADING_SINGLE_RUN_LOCK_FILE` wins when it is set to a non-blank
    value; blank or unset falls back to the operational default. A
    RELATIVE value is allowed and resolves against the current working
    directory -- which is why every subprocess in the test suite passes an
    absolute path under its own tmp_path.

    Only a filesystem path is ever read from or written to this variable;
    no credential, account number or payload is involved.
    """
    configured = os.environ.get(LOCK_FILE_ENV_VAR, "").strip()
    if not configured:
        return _LOCK_FILE
    return Path(configured).expanduser().resolve()


def _identity(stat_result):
    """(device, inode) -- what "the same file" actually means. A path is
    not an identity: it can be unlinked and recreated under our feet."""
    return (stat_result.st_dev, stat_result.st_ino)


def _path_identity(path):
    try:
        return _identity(os.stat(path))
    except OSError:
        return None


def _release_and_close(handle):
    try:
        fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


def _unlink_owned_lock(lock_path, identity):
    """Remove the lock file ONLY while it is still the exact file this
    context locked. If the path now resolves to a different inode, some
    other process created it after ours was replaced -- deleting it would
    destroy a lock we do not own, so warn instead."""
    on_disk = _path_identity(lock_path)
    if on_disk is None:
        return
    if on_disk != identity:
        logger.warning(
            "single-run lock at %s was replaced by a different file while held "
            "(expected inode %s, found %s) -- leaving it alone",
            lock_path, identity[1], on_disk[1],
        )
        return
    try:
        os.unlink(lock_path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("could not remove the single-run lock at %s: %s", lock_path, exc)


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
    """Exclusive single-instance lock, held for the duration of the block.

    Exclusion is decided by the kernel's flock, never by the presence of
    the file: a lock file left behind by a SIGKILLed or power-cut process
    is stale, not authoritative, and must not block the next run. The
    kernel drops a dead process's flock on exit, so a stale path is simply
    re-locked and cleaned up by whoever runs next (CODEX-060 §5).

    The file itself IS removed when this context owned it (CODEX-060 §1),
    on every exit path -- normal, exception, KeyboardInterrupt, SystemExit
    -- but never when the lock was not acquired, and never when the path
    has since come to name a different file.
    """
    lock_path = get_single_run_lock_file()
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise IdempotencyError(
            f"Could not prepare the directory for the single-run lock at {lock_path}: {exc}"
        ) from exc

    deadline = time.monotonic() + timeout
    handle, identity = _acquire_owned_lock(lock_path, deadline, timeout)
    try:
        yield
    finally:
        # Unlink BEFORE releasing, deliberately. Releasing first would let
        # a waiter acquire this same inode and start work, and our unlink
        # would then strip the path out from under it -- after which a
        # third process would create a fresh file, lock that, and run
        # concurrently with the waiter. Removing the path while we still
        # hold the lock means every waiter that wakes up on this inode
        # fails its identity re-check and starts over (CODEX-060 §4).
        _unlink_owned_lock(lock_path, identity)
        _release_and_close(handle)


def _acquire_owned_lock(lock_path, deadline, timeout):
    """Returns an (open handle, identity) pair for a lock this process
    genuinely owns: it holds the flock AND the path still names the very
    file the flock is on."""
    while True:
        handle = open(lock_path, "a+")
        try:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise IdempotencyError(
                            f"Could not acquire KIS order idempotency lock within {timeout}s -- "
                            "another instance of this process may already be running."
                        )
                    time.sleep(0.05)
        except BaseException:
            handle.close()
            raise

        identity = _identity(os.fstat(handle.fileno()))
        if _path_identity(lock_path) == identity:
            return handle, identity

        # The previous owner unlinked this file after we opened it, so our
        # flock now guards a file no future process will ever find. Drop it
        # and race for the real one.
        _release_and_close(handle)
        if time.monotonic() >= deadline:
            raise IdempotencyError(
                f"Could not acquire KIS order idempotency lock within {timeout}s -- "
                "the lock file kept being replaced by another instance."
            )


def _read(conn, sql, params=(), *, fetch="all"):
    """CODEX-057: every order-state READ is normalized here. A raw
    sqlite3 error must not reach a caller that would then treat "the
    database could not be read" as "there is no such order"."""
    order_repository._ensure_usable(conn)
    try:
        cursor = conn.execute(sql, params)
        return cursor.fetchone() if fetch == "one" else cursor.fetchall()
    except Exception as exc:  # noqa: BLE001 -- normalized, never raw
        raise OrderRepositoryReadError("failed to read durable order state") from exc


def find_existing(conn, *, internal_order_id, signal_id, symbol, side, trading_date):
    """Read-only lookup -- returns the existing row (sqlite3.Row) if
    either uniqueness key already has an attempt recorded, else None.
    Callers should still call register() inside the same lock span
    rather than relying on this alone (this is advisory for a clear
    caller-facing check; register()'s INSERT is the actual atomic
    guarantee)."""
    row = _read(
        conn, "SELECT * FROM kis_order_idempotency WHERE internal_order_id = ?",
        (internal_order_id,), fetch="one",
    )
    if row is not None:
        return row
    return _read(
        conn,
        "SELECT * FROM kis_order_idempotency WHERE signal_id = ? AND symbol = ? "
        "AND side = ? AND trading_date = ?",
        (signal_id, symbol, side, trading_date), fetch="one",
    )


def register(conn, *, internal_order_id, signal_id, symbol, side, trading_date, status="CREATED",
              requested_quantity=None, strategy_id=None, commit=True):
    """Atomically records a new order attempt. Raises
    DuplicateOrderAttemptError (via the table's UNIQUE constraints) if
    either key already exists -- callers must call this BEFORE
    KISBroker.submit_order(), inside single_run_lock().

    `requested_quantity` (CODEX-045) is recorded so a later broker-order-
    status lookup can tell "fully filled" apart from "partially filled"
    -- without it, any nonzero fill looks indistinguishable from a full
    fill.

    `strategy_id` is what makes the per-strategy position cap countable
    (execution/entry_limits.py). It is optional here rather than
    required because sells and the legacy paths have no strategy to
    give; the entry path always supplies one, and a buy row without it
    is counted against EVERY strategy rather than none."""
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
            "status, created_at, updated_at, requested_quantity, strategy_id, version) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, 0)",
            (internal_order_id, signal_id, symbol, side, trading_date, status, now, now,
             requested_quantity, strategy_id),
        )
        # CODEX-047: the creation event is written in the SAME transaction
        # as the row, so an order can never exist without the durable
        # event history that every later transition appends to.
        order_repository.append_creation_event(conn, order_id=internal_order_id, state=status)
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


def has_unknown_order(conn):
    """CODEX-044: an order this system submitted but never got a definite
    ACCEPTED/REJECTED/FILLED answer for (broker timeout, ambiguous
    response) blocks EVERY new order on the account until a human/
    reconciliation resolves it.

    Deliberately account-wide, not `(symbol, side)`-scoped as it was
    before: while any order is UNKNOWN this codebase does not know the
    account's true exposure, so scoping the block to the same symbol and
    the same side (Codex's second CODEX-044 finding) would let a new buy
    through while a sell of the same symbol sat unresolved."""
    row = _read(
        conn, "SELECT 1 FROM kis_order_idempotency WHERE status = 'UNKNOWN' LIMIT 1",
        fetch="one",
    )
    return row is not None


def count_unknown_orders(conn):
    """How many orders are UNKNOWN, not merely whether any is.

    The reconciliation snapshot records a COUNT, and a count derived from
    a boolean would say "1" for any number of them -- which understates
    exactly when it matters most.
    """
    row = _read(
        conn, "SELECT COUNT(*) FROM kis_order_idempotency WHERE status = 'UNKNOWN'",
        fetch="one",
    )
    return int(row[0]) if row else 0


def list_orders_by_status(conn, statuses):
    """Every order attempt currently in one of `statuses` -- the internal
    side of reconciliation/snapshot.py's open-order comparison.

    `version` is selected because the settlement pass
    (scripts/run_reconciliation.py) compare-and-sets these rows forward,
    and a CAS without the caller's expected version is not a CAS."""
    statuses = tuple(statuses)
    if not statuses:
        return []
    placeholders = ",".join("?" for _ in statuses)
    return _read(
        conn,
        "SELECT internal_order_id, broker_order_id, symbol, side, status, "
        "requested_quantity, version "
        f"FROM kis_order_idempotency WHERE status IN ({placeholders})",
        statuses,
    )


def list_orders_with_broker_id(conn):
    """Every order attempt that has a KIS-side order id, for matching
    KIS's own fill rows back to what this codebase actually requested."""
    return _read(
        conn,
        "SELECT internal_order_id, broker_order_id, symbol, side, status, requested_quantity "
        "FROM kis_order_idempotency WHERE broker_order_id IS NOT NULL",
    )


def list_unknown_orders(conn):
    """CODEX-044: every order attempt still sitting in UNKNOWN status --
    the set kis_position_manager.py's periodic tick tries to resolve via
    reconciliation.order_reconciler.reconcile_unknown_order() against
    KIS's own open-order/fill history each pass."""
    return _read(
        conn,
        "SELECT internal_order_id, broker_order_id, symbol, side, requested_quantity, version "
        "FROM kis_order_idempotency WHERE status = 'UNKNOWN'",
    )


# CODEX-047: there is deliberately NO update_status() here any more. Every
# order state change goes through execution/order_repository.py's
# compare-and-set, which validates the transition against
# order_state_machine.py, requires the caller's expected state AND version
# to still hold, and writes the state change and its order_state_events row
# in one transaction. A bare "set this status" API cannot offer any of that,
# so keeping one available -- even for "simple" cases -- is exactly the
# bypass Codex flagged.
