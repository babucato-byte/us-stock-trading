"""Connection and migration-runner for the Stage 5 local trading state
database.

Default location mirrors positions/store.py's env-var-override pattern
(STATE_STORE_DB_FILE), so tests can redirect to tmp_path without ever
touching a real database file, and the real default path lives at the
repo root (not inside state_store/) to keep it visually grouped with the
other operational files (order_history.csv, POSITION_STORE.json, etc.)
this module deliberately does NOT replace.
"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from state_store.migrations import MIGRATIONS

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_FILE = BASE_DIR / "TRADING_STATE.db"


class StateStoreError(Exception):
    """Raised for state-store-specific failures (migration errors, etc.)."""


def _resolve_db_path():
    override = os.environ.get("STATE_STORE_DB_FILE")
    return Path(override) if override else DEFAULT_DB_FILE


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def connect(db_path=None, *, busy_timeout_ms=5000):
    """Open a connection with WAL journaling and foreign keys enabled.

    WAL mode allows concurrent readers alongside a single writer without
    the whole-file-lock contention plain SQLite (rollback journal mode)
    would otherwise impose -- appropriate for this project's single-process,
    occasionally-multi-threaded (see positions/store.py's locked_position()
    threading test) usage pattern.
    """
    path = Path(db_path) if db_path is not None else _resolve_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=busy_timeout_ms / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    return conn


def open_db(db_path=None, *, busy_timeout_ms=5000):
    """connect() + init_db() in one call -- the convenience entry point for
    callers (e.g. positions/lifecycle.py's exit-intent reservation) that
    just need a ready-to-use, current-schema connection and don't care
    about the connect/migrate split. init_db() is itself idempotent, so
    calling this on every exit attempt is safe (a no-op migration check)
    rather than expensive schema work."""
    conn = connect(db_path, busy_timeout_ms=busy_timeout_ms)
    init_db(conn)
    return conn


def get_schema_version(conn):
    """Return the highest applied migration version, or 0 if the
    schema_migrations table doesn't exist yet (fresh database)."""
    table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if table_exists is None:
        return 0
    row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    return row["v"] or 0


def _run_with_lock_retry(fn, *, retries, retry_delay_seconds):
    """Run `fn()` (a zero-arg callable performing some DDL/write), retrying
    on sqlite3.OperationalError("database is locked") a few times with a
    short backoff. Two threads/processes racing to run first-time schema
    creation on a brand-new database file can hit this even with
    busy_timeout set -- DDL/CREATE TABLE contention on a just-created file
    isn't always covered by the busy handler the same way row-level write
    contention is. This is purely a "who gets there first" race, not a
    real data conflict, so retrying is safe. Any other error, or the final
    attempt, propagates immediately."""
    import time as _time

    for attempt in range(retries):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if attempt == retries - 1 or "locked" not in str(exc).lower():
                raise
            _time.sleep(retry_delay_seconds)


def init_db(conn, *, _retries=5, _retry_delay_seconds=0.05):
    """Apply every migration in MIGRATIONS not yet recorded as applied.

    Idempotent: calling this against an already-current database is a
    no-op. Each migration runs inside its own transaction -- a failure
    partway through one migration's statements rolls that migration back
    entirely rather than leaving a half-created set of tables. See
    _run_with_lock_retry() for why first-time schema creation is retried.
    """
    from state_store.schema import SCHEMA_MIGRATIONS_TABLE

    def _create_migrations_table():
        conn.execute(SCHEMA_MIGRATIONS_TABLE)
        conn.commit()

    _run_with_lock_retry(_create_migrations_table, retries=_retries, retry_delay_seconds=_retry_delay_seconds)

    current_version = get_schema_version(conn)
    for version, description, statements in MIGRATIONS:
        if version <= current_version:
            continue

        def _apply_migration(statements=statements, version=version, description=description):
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, description, applied_at) VALUES (?, ?, ?)",
                (version, description, now_iso()),
            )
            conn.commit()

        try:
            _run_with_lock_retry(_apply_migration, retries=_retries, retry_delay_seconds=_retry_delay_seconds)
        except sqlite3.Error as exc:
            conn.rollback()
            raise StateStoreError(f"Migration {version} ({description!r}) failed: {exc}") from exc
    return get_schema_version(conn)
