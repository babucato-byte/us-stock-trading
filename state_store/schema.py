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

# ---------------------------------------------------------------------------
# Migration 2 (CODEX-024): durable exit-intent ledger.
#
# A position's exit is reserved here BEFORE any broker call is made, so
# that a crash/timeout between "we decided to exit" and "we know what the
# broker actually did" is recoverable on restart instead of silently
# resubmitting a second sell. No FOREIGN KEY to positions/store.py's own
# JSON-based position records -- position_id is an informal correlation
# key, exactly like fills.client_order_id already is to orders (see
# migration 1's own note on this), since positions are not themselves
# stored in this database.
# ---------------------------------------------------------------------------

EXIT_INTENTS_TABLE = """
CREATE TABLE exit_intents (
    intent_id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL,
    requested_qty REAL NOT NULL,
    confirmed_filled_qty REAL NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    broker_order_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

EXIT_INTENTS_POSITION_INDEX = """
CREATE INDEX idx_exit_intents_position_id ON exit_intents (position_id)
"""

MIGRATION_2_STATEMENTS = [
    EXIT_INTENTS_TABLE,
    EXIT_INTENTS_POSITION_INDEX,
]

# ---------------------------------------------------------------------------
# Migration 3 (CODEX-028): positions/position_events become the canonical
# trading-state tables -- see positions/store.py's module docstring. Two
# columns track the health of POSITION_STORE.json, which is now a
# best-effort *projection* of this table, never the source of truth:
# projection_status distinguishes "never attempted", "written
# successfully", and "write failed" (the failure case that used to be
# silently indistinguishable from success before this migration), and
# projection_updated_at records when the projection was last written.
# ---------------------------------------------------------------------------

POSITIONS_ADD_PROJECTION_STATUS = """
ALTER TABLE positions ADD COLUMN projection_status TEXT
"""

POSITIONS_ADD_PROJECTION_UPDATED_AT = """
ALTER TABLE positions ADD COLUMN projection_updated_at TEXT
"""

MIGRATION_3_STATEMENTS = [
    POSITIONS_ADD_PROJECTION_STATUS,
    POSITIONS_ADD_PROJECTION_UPDATED_AT,
]

# ---------------------------------------------------------------------------
# Migration 4 (CODEX-031): durable live-entry budget/count reservations.
#
# Before this table, the 30,000 KRW ceiling, daily entry count, and
# concurrent-position count enforced by live_readiness/order_gateway.py
# were entirely caller-supplied (LiveEntryContext fields) -- a caller
# could self-report a 3,000,000 KRW budget and have it approved, since
# nothing independently tracked how much of the pilot's real budget was
# already committed or reserved. This table is the authoritative record
# of every live-entry attempt's reserved notional, keyed by day (via
# created_at), so the gateway can compute "how much of the 30,000 KRW is
# actually still available" from durable state instead of trusting the
# caller's own accounting.
# ---------------------------------------------------------------------------

LIVE_ENTRY_RESERVATIONS_TABLE = """
CREATE TABLE live_entry_reservations (
    reservation_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    notional_krw REAL NOT NULL,
    state TEXT NOT NULL,
    position_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

LIVE_ENTRY_RESERVATIONS_CREATED_AT_INDEX = """
CREATE INDEX idx_live_entry_reservations_created_at ON live_entry_reservations (created_at)
"""

MIGRATION_4_STATEMENTS = [
    LIVE_ENTRY_RESERVATIONS_TABLE,
    LIVE_ENTRY_RESERVATIONS_CREATED_AT_INDEX,
]

# ---------------------------------------------------------------------------
# Migration 5 (CODEX-034): client_order_id on live_entry_reservations, so a
# reservation whose broker response was lost (timeout/connection reset --
# NOT a definitive rejection) can later be reconciled by looking the same
# order up at the broker, instead of being guessed at. Before this column,
# AlpacaBroker.submit_order() had no way to durably record *which* broker
# order a reservation corresponded to, so an ambiguous failure had only two
# options: release it (CODEX-034's bug -- silently under-counts real
# exposure if the broker actually received the order) or leave it stuck
# forever with no way to ever resolve it. client_order_id is generated and
# stored before the broker call, exactly like state_store/
# exit_intent_ledger.py already does for exits.
# ---------------------------------------------------------------------------

LIVE_ENTRY_RESERVATIONS_ADD_CLIENT_ORDER_ID = """
ALTER TABLE live_entry_reservations ADD COLUMN client_order_id TEXT
"""

LIVE_ENTRY_RESERVATIONS_CLIENT_ORDER_ID_INDEX = """
CREATE UNIQUE INDEX idx_live_entry_reservations_client_order_id
    ON live_entry_reservations (client_order_id)
"""

MIGRATION_5_STATEMENTS = [
    LIVE_ENTRY_RESERVATIONS_ADD_CLIENT_ORDER_ID,
    LIVE_ENTRY_RESERVATIONS_CLIENT_ORDER_ID_INDEX,
]

# ---------------------------------------------------------------------------
# Migration 6 (KIS migration, spec §17): durable idempotency record for
# every KIS order submission attempt, keyed by internal_order_id (this
# codebase's own id, generated BEFORE the broker call -- never the
# broker's own order id, which doesn't exist yet at insert time). The
# UNIQUE constraint on internal_order_id is what makes a duplicate
# submission attempt (process restart, retried caller, etc.) a plain
# INSERT failure execution/idempotency.py catches and turns into "this
# order was already attempted" rather than a second real KIS order.
# ---------------------------------------------------------------------------

KIS_ORDER_IDEMPOTENCY_TABLE = """
CREATE TABLE kis_order_idempotency (
    internal_order_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    trading_date TEXT NOT NULL,
    broker_order_id TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (signal_id, symbol, side, trading_date)
)
"""

KIS_ORDER_IDEMPOTENCY_SYMBOL_DATE_INDEX = """
CREATE INDEX idx_kis_order_idempotency_symbol_date ON kis_order_idempotency (symbol, trading_date)
"""

MIGRATION_6_STATEMENTS = [
    KIS_ORDER_IDEMPOTENCY_TABLE,
    KIS_ORDER_IDEMPOTENCY_SYMBOL_DATE_INDEX,
]

# ---------------------------------------------------------------------------
# Migration 7 (CODEX-045): kis_order_idempotency.requested_quantity -- the
# ORIGINALLY requested share count for this order, recorded at register()
# time (before any fill is known). Without this, a broker-order-status
# lookup has no way to tell "1 of 2 shares filled" (PARTIALLY_FILLED) apart
# from "1 of 1 shares filled" (FILLED) -- the exact bug Codex found: a
# 2-share sell with a 1-share fill was misclassified as fully FILLED.
# ---------------------------------------------------------------------------

KIS_ORDER_IDEMPOTENCY_ADD_REQUESTED_QUANTITY = """
ALTER TABLE kis_order_idempotency ADD COLUMN requested_quantity REAL
"""

MIGRATION_7_STATEMENTS = [
    KIS_ORDER_IDEMPOTENCY_ADD_REQUESTED_QUANTITY,
]

# Every table this schema version creates -- used by export.py's
# export_all() and by tests asserting the full table set exists.
ALL_TABLES = [
    "orders", "fills", "positions", "position_events",
    "strategy_runs", "risk_events", "kill_switch_events",
    "exit_intents", "live_entry_reservations", "kis_order_idempotency",
]
