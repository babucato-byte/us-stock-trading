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


def init_db(conn):
    """Apply every migration in MIGRATIONS not yet recorded as applied.

    Idempotent: calling this against an already-current database is a
    no-op. Each migration runs inside its own transaction -- a failure
    partway through one migration's statements rolls that migration back
    entirely rather than leaving a half-created set of tables.
    """
    from state_store.schema import SCHEMA_MIGRATIONS_TABLE
    conn.execute(SCHEMA_MIGRATIONS_TABLE)
    conn.commit()

    current_version = get_schema_version(conn)
    for version, description, statements in MIGRATIONS:
        if version <= current_version:
            continue
        try:
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, description, applied_at) VALUES (?, ?, ?)",
                (version, description, now_iso()),
            )
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise StateStoreError(f"Migration {version} ({description!r}) failed: {exc}") from exc
    return get_schema_version(conn)
