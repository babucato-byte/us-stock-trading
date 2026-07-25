"""DDL for the Stage 5 local trading state database.

Every table uses TEXT for timestamps (ISO 8601 strings, matching the
convention already used across order_history.csv, POSITION_STORE.json, and
kill_switch_state.py) rather than SQLite's native datetime affinity, so
values round-trip identically with the rest of the codebase.
"""

SCHEMA_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""

# ---------------------------------------------------------------------------
# Migration 1: initial schema.
# ---------------------------------------------------------------------------

ORDERS_TABLE = """
CREATE TABLE orders (
    order_id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT UNIQUE,
    symbol TEXT NOT NULL,
    order_date TEXT NOT NULL,
    side TEXT,
    mode TEXT,
    dry_run INTEGER,
    status TEXT,
    requested_qty INTEGER,
    broker_order_id TEXT,
    source TEXT NOT NULL DEFAULT 'live',
    created_at TEXT NOT NULL,
    UNIQUE (symbol, order_date, side)
)
"""

FILLS_TABLE = """
CREATE TABLE fills (
    fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    order_date TEXT NOT NULL,
    filled_qty INTEGER,
    remaining_qty INTEGER,
    average_fill_price REAL,
    broker_status TEXT,
    local_status TEXT,
    recorded_at TEXT NOT NULL
)
"""
# No FOREIGN KEY on client_order_id -> orders: order_history.csv (the
# source for `orders`) does not itself store client_order_id (that lives
# in the separate order_intent_ledger.csv, per paper_strategy_order.py's
# design), so a legacy-imported orders row frequently has no
# client_order_id to reference at all. fills.client_order_id is an
# informal correlation key, exactly like the CSV files it mirrors are
# correlated by (symbol, order_date) rather than a real foreign key.

POSITIONS_TABLE = """
CREATE TABLE positions (
    position_id TEXT PRIMARY KEY,
    client_order_id TEXT,
    broker_order_id TEXT,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    state TEXT NOT NULL,
    requested_qty INTEGER,
    filled_qty INTEGER,
    remaining_qty INTEGER,
    average_fill_price REAL,
    stop_price REAL,
    target_1_price REAL,
    target_2_price REAL,
    realized_pnl REAL,
    unrealized_pnl REAL,
    entry_time TEXT,
    last_reconciled_at TEXT,
    exit_reason TEXT,
    updated_at TEXT NOT NULL
)
"""

POSITION_EVENTS_TABLE = """
CREATE TABLE position_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id TEXT NOT NULL,
    state TEXT NOT NULL,
    reason TEXT,
    occurred_at TEXT NOT NULL,
    FOREIGN KEY (position_id) REFERENCES positions (position_id)
)
"""

STRATEGY_RUNS_TABLE = """
CREATE TABLE strategy_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    notes TEXT
)
"""

RISK_EVENTS_TABLE = """
CREATE TABLE risk_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    symbol TEXT,
    detail TEXT,
    occurred_at TEXT NOT NULL
)
"""

KILL_SWITCH_EVENTS_TABLE = """
CREATE TABLE kill_switch_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT,
    occurred_at TEXT NOT NULL
)
"""

MIGRATION_1_STATEMENTS = [
    ORDERS_TABLE,
    FILLS_TABLE,
    POSITIONS_TABLE,
    POSITION_EVENTS_TABLE,
    STRATEGY_RUNS_TABLE,
    RISK_EVENTS_TABLE,
    KILL_SWITCH_EVENTS_TABLE,
]

# Every table this schema version creates -- used by export.py's
# export_all() and by tests asserting the full table set exists.
ALL_TABLES = [
    "orders", "fills", "positions", "position_events",
    "strategy_runs", "risk_events", "kill_switch_events",
]
