"""Stage 5: local SQLite trading state store tests.

Every test uses an isolated tmp_path database and tmp_path CSV fixtures --
never touches the real TRADING_STATE.db, order_history.csv, or
order_reconciliation.csv.
"""
import sqlite3

import pandas as pd
import pytest

from state_store import csv_import, db, export
from state_store.schema import ALL_TABLES


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test_state.db")
    db.init_db(connection)
    yield connection
    connection.close()


# ---------------------------------------------------------------------------
# Schema / migrations
# ---------------------------------------------------------------------------

def test_init_db_creates_every_expected_table(conn):
    tables = {
        row["name"] for row in
        conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    for table_name in ALL_TABLES:
        assert table_name in tables


def test_init_db_records_schema_version(conn):
    from state_store.migrations import CURRENT_SCHEMA_VERSION
    assert db.get_schema_version(conn) == CURRENT_SCHEMA_VERSION


def test_init_db_is_idempotent(conn):
    from state_store.migrations import MIGRATIONS
    version_before = db.get_schema_version(conn)
    result = db.init_db(conn)  # re-run against an already-migrated database
    assert result == version_before
    row_count = conn.execute("SELECT COUNT(*) AS c FROM schema_migrations").fetchone()["c"]
    assert row_count == len(MIGRATIONS)  # no migration was re-applied/re-recorded


def test_get_schema_version_zero_on_fresh_uninitialized_connection(tmp_path):
    fresh_conn = db.connect(tmp_path / "fresh.db")
    assert db.get_schema_version(fresh_conn) == 0
    fresh_conn.close()


def test_connect_creates_parent_directory(tmp_path):
    nested = tmp_path / "nested" / "dir" / "state.db"
    connection = db.connect(nested)
    db.init_db(connection)
    connection.close()
    assert nested.exists()


# ---------------------------------------------------------------------------
# Orders / fills / positions basic writes (transactional integrity check)
# ---------------------------------------------------------------------------

def test_orders_and_fills_transaction_is_atomic(conn):
    conn.execute(
        "INSERT INTO orders (client_order_id, symbol, order_date, side, source, created_at) "
        "VALUES ('coid-1', 'AAPL', '2026-07-25', 'buy', 'test', ?)",
        (db.now_iso(),),
    )
    conn.execute(
        "INSERT INTO fills (client_order_id, symbol, order_date, filled_qty, recorded_at) "
        "VALUES ('coid-1', 'AAPL', '2026-07-25', 10, ?)",
        (db.now_iso(),),
    )
    conn.commit()

    orders = conn.execute("SELECT * FROM orders").fetchall()
    fills = conn.execute("SELECT * FROM fills").fetchall()
    assert len(orders) == 1
    assert len(fills) == 1
    assert fills[0]["client_order_id"] == orders[0]["client_order_id"]


def test_duplicate_symbol_order_date_side_rejected(conn):
    conn.execute(
        "INSERT INTO orders (symbol, order_date, side, source, created_at) "
        "VALUES ('AAPL', '2026-07-25', 'buy', 'test', ?)",
        (db.now_iso(),),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO orders (symbol, order_date, side, source, created_at) "
            "VALUES ('AAPL', '2026-07-25', 'buy', 'test', ?)",
            (db.now_iso(),),
        )


def test_position_events_foreign_key_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO position_events (position_id, state, occurred_at) "
            "VALUES ('does-not-exist', 'ARMED', ?)",
            (db.now_iso(),),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# CSV import (read-only)
# ---------------------------------------------------------------------------

def test_import_order_history_legacy_two_column_format(conn, tmp_path):
    csv_path = tmp_path / "order_history.csv"
    csv_path.write_text("symbol,order_date\nAAPL,2026-07-20\nMSFT,2026-07-21\n")
    original_bytes = csv_path.read_bytes()

    result = csv_import.import_order_history(csv_path, conn)

    assert result == {"rows_read": 2, "rows_imported": 2, "rows_skipped": 0}
    orders = conn.execute("SELECT symbol, order_date, mode, status FROM orders ORDER BY symbol").fetchall()
    assert [dict(r) for r in orders] == [
        {"symbol": "AAPL", "order_date": "2026-07-20", "mode": None, "status": None},
        {"symbol": "MSFT", "order_date": "2026-07-21", "mode": None, "status": None},
    ]
    assert csv_path.read_bytes() == original_bytes  # never modified


def test_import_order_history_full_column_format(conn, tmp_path):
    csv_path = tmp_path / "order_history.csv"
    pd.DataFrame([
        {"symbol": "AAPL", "order_date": "2026-07-20", "mode": "PAPER", "dry_run": False, "status": "SUBMITTED"},
    ]).to_csv(csv_path, index=False)

    result = csv_import.import_order_history(csv_path, conn)
    assert result["rows_imported"] == 1
    row = conn.execute("SELECT mode, dry_run, status FROM orders").fetchone()
    assert row["mode"] == "PAPER"
    assert row["dry_run"] == 0
    assert row["status"] == "SUBMITTED"


def test_import_order_history_is_idempotent(conn, tmp_path):
    csv_path = tmp_path / "order_history.csv"
    csv_path.write_text("symbol,order_date\nAAPL,2026-07-20\n")

    first = csv_import.import_order_history(csv_path, conn)
    second = csv_import.import_order_history(csv_path, conn)

    assert first["rows_imported"] == 1
    assert second == {"rows_read": 1, "rows_imported": 0, "rows_skipped": 1}
    assert conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"] == 1


def test_import_order_history_missing_file_raises(conn, tmp_path):
    with pytest.raises(FileNotFoundError):
        csv_import.import_order_history(tmp_path / "does_not_exist.csv", conn)


def test_import_order_reconciliation(conn, tmp_path):
    csv_path = tmp_path / "order_reconciliation.csv"
    pd.DataFrame([{
        "client_order_id": "coid-1", "symbol": "AAPL", "order_date": "2026-07-20",
        "requested_qty": 10, "filled_qty": 10, "remaining_qty": 0,
        "average_fill_price": 100.5, "broker_status": "filled", "local_status": "FILLED",
        "last_reconciled_at": "2026-07-20T10:00:00+00:00",
    }]).to_csv(csv_path, index=False)

    result = csv_import.import_order_reconciliation(csv_path, conn)
    assert result["rows_imported"] == 1
    row = conn.execute("SELECT * FROM fills").fetchone()
    assert row["client_order_id"] == "coid-1"
    assert row["average_fill_price"] == 100.5


def test_import_order_reconciliation_idempotent(conn, tmp_path):
    csv_path = tmp_path / "order_reconciliation.csv"
    pd.DataFrame([{
        "client_order_id": "coid-1", "symbol": "AAPL", "order_date": "2026-07-20",
        "requested_qty": 10, "filled_qty": 10, "remaining_qty": 0,
        "average_fill_price": 100.5, "broker_status": "filled", "local_status": "FILLED",
        "last_reconciled_at": "2026-07-20T10:00:00+00:00",
    }]).to_csv(csv_path, index=False)

    csv_import.import_order_reconciliation(csv_path, conn)
    second = csv_import.import_order_reconciliation(csv_path, conn)
    assert second["rows_imported"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM fills").fetchone()["c"] == 1


# ---------------------------------------------------------------------------
# Export / rollback
# ---------------------------------------------------------------------------

def test_export_table_round_trips(conn, tmp_path):
    conn.execute(
        "INSERT INTO orders (symbol, order_date, side, source, created_at) "
        "VALUES ('AAPL', '2026-07-25', 'buy', 'test', ?)",
        (db.now_iso(),),
    )
    conn.commit()

    dest = tmp_path / "orders_export.csv"
    count = export.export_table(conn, "orders", dest)
    assert count == 1
    exported = pd.read_csv(dest)
    assert exported.loc[0, "symbol"] == "AAPL"


def test_export_table_rejects_unknown_table(conn, tmp_path):
    with pytest.raises(ValueError):
        export.export_table(conn, "not_a_real_table", tmp_path / "x.csv")


def test_export_all_writes_every_table(conn, tmp_path):
    dest_dir = tmp_path / "export"
    counts = export.export_all(conn, dest_dir)
    assert set(counts) == set(ALL_TABLES)
    for table_name in ALL_TABLES:
        assert (dest_dir / f"{table_name}.csv").exists()


def test_reset_schema_clears_data_and_reinitializes(conn):
    conn.execute(
        "INSERT INTO orders (symbol, order_date, side, source, created_at) "
        "VALUES ('AAPL', '2026-07-25', 'buy', 'test', ?)",
        (db.now_iso(),),
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"] == 1

    version = export.reset_schema(conn)

    from state_store.migrations import CURRENT_SCHEMA_VERSION
    assert version == CURRENT_SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"] == 0


def test_reset_schema_never_touches_source_csv(conn, tmp_path):
    csv_path = tmp_path / "order_history.csv"
    csv_path.write_text("symbol,order_date\nAAPL,2026-07-20\n")
    original_bytes = csv_path.read_bytes()

    csv_import.import_order_history(csv_path, conn)
    export.reset_schema(conn)

    assert csv_path.read_bytes() == original_bytes


def test_real_db_file_never_created_by_tests():
    assert not db.DEFAULT_DB_FILE.exists()


# ---------------------------------------------------------------------------
# Concurrent first-time schema creation retries on "database is locked"
# instead of failing outright (see db._run_with_lock_retry()).
# ---------------------------------------------------------------------------

def test_run_with_lock_retry_succeeds_after_transient_lock_error():
    import sqlite3
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    result = db._run_with_lock_retry(flaky, retries=5, retry_delay_seconds=0.001)
    assert result == "ok"
    assert len(attempts) == 3


def test_run_with_lock_retry_gives_up_after_max_retries():
    import sqlite3

    def always_locked():
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        db._run_with_lock_retry(always_locked, retries=3, retry_delay_seconds=0.001)


def test_run_with_lock_retry_does_not_retry_unrelated_errors():
    import sqlite3
    attempts = []

    def different_error():
        attempts.append(1)
        raise sqlite3.OperationalError("no such table: bogus")

    with pytest.raises(sqlite3.OperationalError):
        db._run_with_lock_retry(different_error, retries=5, retry_delay_seconds=0.001)
    assert len(attempts) == 1  # not retried -- not a locking error
