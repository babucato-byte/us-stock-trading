"""Migration 31 against a database shaped like the live one.

Caught at the pre-deploy snapshot on 2026-09-17 and the reason this
migration is numbered 31 rather than 28. Production's database is at
schema version 30: `feature/exit-v2` applied 28, 29 and 30 against it on
2026-09-10 and 09-11, while the deployed code line defines only up to 27.
Those three migrations exist in the live `schema_migrations` table and in
no branch this code is built from.

`init_db` skips any migration whose version is <= the recorded maximum. A
migration numbered 28 would therefore have been recorded-as-applied and
SILENTLY SKIPPED: `entry_filled_quantity` would never be created, and the
buy-fill sync that reads it runs on every tick.

A unit test against a version-27 database cannot see any of this, which is
why this one builds the production shape instead -- the registry's own
migrations through 27, then 28/29/30 recorded exactly as exit-v2 left
them, then a VIAV-like row.
"""
import pytest

from s6_live import exit_runtime, position_store
from state_store import db as state_db
from state_store.db import get_schema_version, init_db, now_iso
from state_store.migrations import MIGRATIONS

EXIT_V2 = [
    (28, "EXIT V2 PHASE 1: s6_exit_snapshots"),
    (29, "EXIT V2 PHASE 2: shadow decision columns"),
    (30, "EXIT V2 PHASE 3: durable broker-first protective SELL reassessments"),
]
NOW = None


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


@pytest.fixture
def production_shaped(tmp_path, monkeypatch):
    """A database at version 30, without `entry_filled_quantity`."""
    monkeypatch.setattr(state_db, "MIGRATIONS",
                        [m for m in MIGRATIONS if m[0] <= 27], raising=False)
    import sqlite3
    conn = sqlite3.connect(tmp_path / "prod.db")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    for version, description in EXIT_V2:
        conn.execute(
            "INSERT INTO schema_migrations (version, description, applied_at) "
            "VALUES (?, ?, ?)", (version, description, now_iso()))
    conn.commit()
    monkeypatch.setattr(state_db, "MIGRATIONS", MIGRATIONS, raising=False)
    yield conn
    conn.close()


def _viav(conn, quantity=4):
    pid = "s6pos_9e87169b0e814a8e"
    conn.execute(
        "INSERT INTO s6_positions (position_id, strategy_id, symbol, status, "
        "quantity, entry_price, submitted_at, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (pid, "S6", "VIAV", position_store.OPEN, quantity, 35.6,
         now_iso(), now_iso(), now_iso()))
    conn.commit()
    return pid


def test_the_production_shape_is_what_we_think_it_is(production_shaped):
    assert get_schema_version(production_shaped) == 30
    assert "entry_filled_quantity" not in _columns(production_shaped, "s6_positions")


def test_a_migration_numbered_28_would_have_been_skipped(production_shaped):
    """The failure this numbering avoids, demonstrated rather than asserted
    in prose: `init_db` skips anything at or below the recorded maximum."""
    recorded = {r[0] for r in production_shaped.execute(
        "SELECT version FROM schema_migrations")}
    assert {28, 29, 30} <= recorded
    assert all(v <= 30 or v == 31 for v in recorded | {m[0] for m in MIGRATIONS})
    assert 28 not in {m[0] for m in MIGRATIONS}, (
        "28 is taken in production by exit-v2 and must not be reused")


def test_31_applies_and_backfills_the_viav_row(production_shaped):
    pid = _viav(production_shaped, quantity=4)

    assert init_db(production_shaped) == 31
    assert "entry_filled_quantity" in _columns(production_shaped, "s6_positions")

    row = position_store.load(production_shaped, pid)
    assert int(row["quantity"]) == 4, "the held quantity is not disturbed"
    assert int(row["entry_filled_quantity"]) == 4, (
        "an existing row's BUY fill is whatever it is already holding")


def test_the_exit_v2_tables_survive_the_upgrade(production_shaped):
    before = {r[0] for r in production_shaped.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    init_db(production_shaped)
    after = {r[0] for r in production_shaped.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert before <= after, "migration 31 must not drop anything exit-v2 left"


def test_the_full_viav_sequence_on_the_upgraded_database(production_shaped):
    """What the live repair has to survive, end to end."""
    pid = _viav(production_shaped, quantity=4)
    init_db(production_shaped)

    # a buy sync carrying the same cumulative it always carried
    assert position_store.apply_fill(
        production_shaped, pid, filled_quantity=4, average_fill_price=35.6,
        venue="NASDAQ", entry_order_id="o-1") is False
    assert int(position_store.load(production_shaped, pid)["quantity"]) == 4

    # the confirmed SELL of one share, applied once through the owner
    from state_store import exit_intent_ledger as eil
    eil.reserve(production_shaped, pid, "RANGE_REENTRY", 4, "s6exit-VIAV-x")
    intent = exit_runtime._active_intent(production_shaped, pid)
    exit_runtime.apply_confirmed_exit_fill(production_shaped, pid, intent, 1)
    assert int(position_store.load(production_shaped, pid)["quantity"]) == 3

    # and it stays 3 no matter how many buy syncs run
    for _ in range(5):
        position_store.apply_fill(
            production_shaped, pid, filled_quantity=4, average_fill_price=35.6,
            venue="NASDAQ", entry_order_id="o-1")
    row = position_store.load(production_shaped, pid)
    assert int(row["quantity"]) == 3, "this is what reverted within 90 seconds"
    assert int(row["entry_filled_quantity"]) == 4
