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

# ---------------------------------------------------------------------------
# Migration 8 (CODEX-047): durable compare-and-set state machine.
#
# Before this migration, execution/idempotency.py::update_status() wrote any
# status string with a bare `UPDATE ... WHERE internal_order_id = ?`: no
# current-state read, no expected-state predicate, no rowcount check, and no
# durable record of the transition itself. Two concurrent writers (the buy
# cycle, the sell/exit tick, a reconciliation pass) could therefore silently
# clobber each other, and nothing in the DB layer enforced that a status
# change had ever passed order_state_machine.transition().
#
# `version` is the optimistic-concurrency counter: every accepted transition
# bumps it, and every transition must name the version it believes it is
# advancing from. `order_state_events` is the append-only history, written in
# the SAME transaction as the state change so a state and its event can never
# disagree.
# ---------------------------------------------------------------------------

KIS_ORDER_IDEMPOTENCY_ADD_VERSION = """
ALTER TABLE kis_order_idempotency ADD COLUMN version INTEGER NOT NULL DEFAULT 0
"""

ORDER_STATE_EVENTS_TABLE = """
CREATE TABLE order_state_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    internal_order_id TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT,
    version INTEGER NOT NULL,
    occurred_at TEXT NOT NULL
)
"""

ORDER_STATE_EVENTS_ORDER_INDEX = """
CREATE INDEX idx_order_state_events_order ON order_state_events (internal_order_id)
"""

MIGRATION_8_STATEMENTS = [
    KIS_ORDER_IDEMPOTENCY_ADD_VERSION,
    ORDER_STATE_EVENTS_TABLE,
    ORDER_STATE_EVENTS_ORDER_INDEX,
]

# ---------------------------------------------------------------------------
# Migration 9 (CODEX-048): durable Shadow Mode audit event store.
#
# The JSONL Shadow log stays (shadow_mode.py -- the per-attempt structured
# record spec §5 requires), but it cannot be the audit system of record: a
# JSONL append has no cross-process atomicity guarantee beyond the flock this
# codebase wraps it in, no retention mechanism, and a torn line is silently
# unreadable. This table is the durable, concurrently-writable, queryable
# audit trail of every Shadow evaluation step -- one row per event, tied
# together by `shadow_run_id`, with a retention purge instead of file
# rotation.
# ---------------------------------------------------------------------------

SHADOW_AUDIT_EVENTS_TABLE = """
CREATE TABLE shadow_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    shadow_run_id TEXT NOT NULL,
    signal_id TEXT,
    internal_order_id TEXT,
    symbol TEXT,
    side TEXT,
    event_type TEXT NOT NULL,
    result TEXT NOT NULL,
    reason_code TEXT,
    payload TEXT,
    created_at TEXT NOT NULL
)
"""

SHADOW_AUDIT_EVENTS_RUN_INDEX = """
CREATE INDEX idx_shadow_audit_events_run ON shadow_audit_events (shadow_run_id)
"""

SHADOW_AUDIT_EVENTS_CREATED_AT_INDEX = """
CREATE INDEX idx_shadow_audit_events_created_at ON shadow_audit_events (created_at)
"""

MIGRATION_9_STATEMENTS = [
    SHADOW_AUDIT_EVENTS_TABLE,
    SHADOW_AUDIT_EVENTS_RUN_INDEX,
    SHADOW_AUDIT_EVENTS_CREATED_AT_INDEX,
]

# ---------------------------------------------------------------------------
# Migration 10 (CODEX-053): at most ONE terminal event per shadow_run_id,
# enforced by the database rather than only by the code that writes it.
#
# The exactly-one-terminal-event invariant was previously an application
# rule plus a reporting query -- which meant a second terminal event was
# detectable after the fact but not prevented, and two concurrent writers
# could both pass an application-level "has this run finished?" check.
# A partial unique index makes the second write fail outright, so
# shadow_audit.finalize_audit_run() can treat an IntegrityError as "this
# run already has a terminal event" and decide (idempotent no-op for the
# same event, AuditInvariantError for a conflicting one) from the durable
# truth rather than from a racy read.
# ---------------------------------------------------------------------------

SHADOW_AUDIT_TERMINAL_ONCE_INDEX = """
CREATE UNIQUE INDEX idx_shadow_audit_terminal_once
    ON shadow_audit_events (shadow_run_id)
    WHERE event_type IN ('SHADOW_COMPLETED', 'SHADOW_BLOCKED', 'SHADOW_ERROR')
"""

MIGRATION_10_STATEMENTS = [
    SHADOW_AUDIT_TERMINAL_ONCE_INDEX,
]

# ---------------------------------------------------------------------------
# PHASE 4A: S1 Limited Live trade accounting.
#
# Separate from `orders`/`fills`, which record what the ORDER SYSTEM did.
# This records what the S1 EXPERIMENT earned, and the two answer
# different questions: an order is filled or not, a trade is profitable
# or not, and only the second needs a scanner run id next to a fee.
#
# Every money column is NULLABLE on purpose. "Unknown" and "zero" are
# different facts and this table refuses to conflate them -- see
# `fees_status`. A fee this project has not yet verified against KIS's
# published schedule must not be stored as 0 and then summed into a
# net P&L that reads as authoritative. Overseas trading has real
# commission, regulatory fees and FX cost; a net figure computed with
# fee=0 is a gross figure wearing the wrong label.
#
# `broker_order_id` and the fill timestamps come from the broker. Where
# this table and the broker disagree, the broker is right: KIS publishes
# order/execution/balance/period-P&L endpoints, and a locally computed
# number must never outrank a reported fill.
# ---------------------------------------------------------------------------

S1_LIVE_TRADES_TABLE = """
CREATE TABLE s1_live_trades (
    trade_id TEXT PRIMARY KEY,

    -- provenance: which scanner observation produced this trade
    source_signal_id TEXT NOT NULL,
    scanner_run_id TEXT NOT NULL,
    scanner_score REAL,
    candidate_rank INTEGER,
    allocation_version TEXT NOT NULL,
    trading_day TEXT NOT NULL,

    -- order identity
    internal_order_id TEXT,
    broker_order_id TEXT,

    -- entry
    entry_submitted_at TEXT,
    entry_filled_at TEXT,
    entry_price REAL,
    qty INTEGER,
    allocated_cash REAL,
    account_cash_before REAL,

    -- exit
    exit_submitted_at TEXT,
    exit_filled_at TEXT,
    exit_price REAL,
    exit_reason TEXT,

    -- accounting. NULL means not established; see fees_status.
    gross_pnl REAL,
    commission REAL,
    regulatory_fees REAL,
    fx_cost REAL,
    fees_total REAL,
    estimated_slippage REAL,
    net_pnl REAL,
    account_cash_after REAL,

    -- 'UNKNOWN'  no fee figure has been established -- net_pnl must stay NULL
    -- 'REPORTED' every component came from the broker
    -- 'PARTIAL'  some components reported, others still unknown
    fees_status TEXT NOT NULL DEFAULT 'UNKNOWN',
    fees_source TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

S1_LIVE_TRADES_SIGNAL_INDEX = """
CREATE UNIQUE INDEX idx_s1_live_trades_source_signal
    ON s1_live_trades (source_signal_id)
"""

S1_LIVE_TRADES_DAY_INDEX = """
CREATE INDEX idx_s1_live_trades_trading_day
    ON s1_live_trades (trading_day)
"""

MIGRATION_11_STATEMENTS = [
    S1_LIVE_TRADES_TABLE,
    S1_LIVE_TRADES_SIGNAL_INDEX,
    S1_LIVE_TRADES_DAY_INDEX,
]

# ---------------------------------------------------------------------------
# PHASE 4B: durable account risk state.
#
# Two tables because the two facts have different lifetimes. The daily
# row is scoped to one ET trading day and is immutable in its start
# figure once captured; the peak is a single account-level high-water
# mark that outlives every day.
#
# Why `start_equity` must be durable rather than recomputed: the daily
# loss limit is measured against the equity the day STARTED from. A
# process that restarts at 14:00 and re-reads equity would treat the
# post-loss figure as the day's starting point, which resets the loss
# budget to zero used -- turning a -2% day into a fresh -2% of room, and
# doing so silently at exactly the moment a restart is most likely
# (something went wrong). The row is therefore written once per day and
# read thereafter; `start_equity_source` records how it was obtained so
# a late or reconstructed capture is never indistinguishable from a
# clean one at the open.
#
# Every equity column is NULLABLE and the status columns carry UNKNOWN,
# because on this broker they currently ARE unknown -- see
# `s1_live/equity.py`. A schema that made them NOT NULL would force a
# caller to invent a number to satisfy it.
# ---------------------------------------------------------------------------

S1_RISK_STATE_TABLE = """
CREATE TABLE s1_risk_state (
    trading_day TEXT PRIMARY KEY,

    start_equity REAL,
    start_equity_source TEXT,
    start_equity_captured_at TEXT,

    current_equity REAL,
    current_equity_source TEXT,
    current_equity_as_of TEXT,

    equity_currency TEXT,

    daily_return_pct REAL,
    drawdown_pct REAL,

    -- ALLOW / BLOCK / UNKNOWN
    daily_loss_status TEXT NOT NULL DEFAULT 'UNKNOWN',
    drawdown_status TEXT NOT NULL DEFAULT 'UNKNOWN',
    status_detail TEXT,

    last_successful_refresh TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

# Single row, id = 1. Account-level and cross-day: a drawdown is not a
# daily measure and its baseline must not reset with the calendar.
S1_RISK_PEAK_TABLE = """
CREATE TABLE s1_risk_peak (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    peak_equity REAL NOT NULL,
    equity_currency TEXT NOT NULL,
    peak_updated_at TEXT NOT NULL,
    peak_source TEXT,
    -- Set whenever the peak was raised by something this system cannot
    -- prove was trading profit. See s1_live/risk_state.py on deposits.
    external_flow_suspected INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

MIGRATION_12_STATEMENTS = [
    S1_RISK_STATE_TABLE,
    S1_RISK_PEAK_TABLE,
]

# ---------------------------------------------------------------------------
# PHASE 4D: facts only a real order can establish.
#
# Two of the rollout requirements cannot be verified by any amount of
# testing, because they are claims about what the BROKER does:
#
#   position_valuation  does qty*avg_price + unrealized equal the
#                       broker's own valuation of the same position?
#   reserved_order_cash does an open order's cash leave the orderable
#                       figure while it is resting?
#
# Both default to UNVERIFIED and both block promotion. They are durable
# rather than computed because the observation happens once, during a
# real trade, and must survive every restart afterwards -- and because a
# MISMATCH must latch: a valuation disagreement is not something a later
# clean read should silently clear.
# ---------------------------------------------------------------------------

S1_VERIFICATION_STATE_TABLE = """
CREATE TABLE s1_verification_state (
    key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    detail TEXT,
    observed_at TEXT,
    evidence TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

MIGRATION_13_STATEMENTS = [
    S1_VERIFICATION_STATE_TABLE,
]

# --- migration 14: the S1 position state the exit policy runs on ----------
#
# `s1_live/exit_policy.py` is a pure function over S1PositionState. This
# table is where that state lives between ticks, and it exists because
# three of its fields CANNOT be recomputed from the market:
#
#   protective_floor_r  ratchets. A position that touched +2R and fell
#                       back keeps the floor that move earned it.
#                       Recomputing from the current price hands the
#                       floor back on every restart -- the exact loss the
#                       profit-protection axis exists to prevent.
#   peak_r              the same, one level down: it is what the floor is
#                       derived FROM, so losing it loses the floor next tick.
#   sessions_held       the time exit counts sessions since entry. A
#                       restart that reset it to 0 would hold a dead
#                       position indefinitely.
#
# `exit_submitted` is the policy-layer half of duplicate-SELL prevention
# (exit_intents is the ledger half). Both must survive a restart or the
# position gets sold twice.
#
# `pending_exit_reason` implements spec §9: an exit that triggered in a
# session the broker will not accept an order in is NOT discarded and NOT
# re-evaluated from scratch -- it is latched here and submitted first
# thing in the next orderable session. A trigger is never reset.
S1_POSITIONS_TABLE = """
CREATE TABLE IF NOT EXISTS s1_positions (
    position_id         TEXT PRIMARY KEY,
    symbol              TEXT NOT NULL,
    strategy_id         TEXT NOT NULL,
    signal_id           TEXT NOT NULL,
    -- The KIS ACTUAL AVERAGE FILL PRICE, never the intended limit price.
    -- Every R level is measured from this, so an intended-price entry
    -- would put the stop in the wrong place by the slippage amount.
    entry_price         REAL NOT NULL,
    quantity            INTEGER NOT NULL,
    entry_order_id      TEXT,
    sessions_held       INTEGER NOT NULL DEFAULT 0,
    last_session_date   TEXT,
    protective_floor_r  REAL,
    peak_r              REAL NOT NULL DEFAULT 0.0,
    exit_submitted      INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'OPEN',
    pending_exit_reason TEXT,
    pending_exit_since  TEXT,
    exit_reason         TEXT,
    opened_at           TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    closed_at           TEXT,
    CHECK (status IN ('OPEN', 'EXIT_PENDING', 'EXIT_SUBMITTED', 'CLOSED')),
    CHECK (entry_price > 0),
    CHECK (quantity >= 1),
    CHECK (exit_submitted IN (0, 1))
)
"""

# One OPEN position per symbol. Stage 1 allows max_positions=1 overall,
# but the constraint that matters for correctness is per-symbol: it makes
# a second entry into a name we already hold impossible at the storage
# layer, not merely unlikely at the gate layer.
S1_POSITIONS_OPEN_SYMBOL_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_s1_positions_open_symbol
    ON s1_positions (symbol) WHERE status != 'CLOSED'
"""

MIGRATION_14_STATEMENTS = [
    S1_POSITIONS_TABLE,
    S1_POSITIONS_OPEN_SYMBOL_INDEX,
]

# S2's open positions.
#
# A separate table rather than columns bolted onto s1_positions, and not
# a generic one either. The two strategies persist DIFFERENT things
# because their exits are different: S1 ratchets an R-multiple floor, S2
# tracks a volume peak. Merging them would give each strategy a set of
# columns that are always NULL for it, and a CHECK constraint that can
# only be written as "NULL is fine", which is how a storage layer stops
# being able to reject a malformed row.
#
# What generalising WOULD have bought -- one reconciliation query -- is
# bought instead by `venue` and `strategy_id` being present here in the
# same shape S1 uses, so a UNION answers "what do we hold" without a
# refactor of either table.
#
# Only history is stored. `effective_stop` and `hard_stop` are written
# because they are the levels that were in force AT ENTRY, and the
# config they came from is expected to change; recomputing them later
# would answer with today's policy about yesterday's position. The
# volume peak and the price at that peak are gone the moment volume
# falls. Everything else a decision needs is recomputed from the
# current observation, deliberately.
S2_POSITIONS_TABLE = """
CREATE TABLE IF NOT EXISTS s2_positions (
    position_id             TEXT PRIMARY KEY,
    strategy_id             TEXT NOT NULL,
    symbol                  TEXT NOT NULL,
    -- The broker's own exchange code for the row, not the one we asked
    -- with. KIS answers a NASD request with NYSE rows, so the requested
    -- code is not an identity -- the same correction TX needed.
    venue                   TEXT,
    quantity                INTEGER NOT NULL,
    -- The KIS ACTUAL AVERAGE FILL PRICE, never the intended limit. The
    -- catastrophic cap is measured from this, so an intended-price
    -- entry would put the stop wrong by the slippage.
    entry_price             REAL NOT NULL,
    entry_time              TEXT NOT NULL,
    entry_session           TEXT,
    entry_order_id          TEXT,
    entry_volume_multiple   REAL,
    baseline_volume         REAL,
    peak_volume_multiple    REAL,
    price_at_volume_peak    REAL,
    decay_since             TEXT,
    effective_stop          REAL,
    hard_stop               REAL,
    status                  TEXT NOT NULL DEFAULT 'OPEN',
    exit_reason             TEXT,
    exit_submitted          INTEGER NOT NULL DEFAULT 0,
    pending_exit_reason     TEXT,
    pending_exit_since      TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    closed_at               TEXT,
    CHECK (status IN ('OPEN', 'EXIT_PENDING', 'EXIT_SUBMITTED', 'CLOSED')),
    CHECK (entry_price > 0),
    CHECK (quantity >= 1),
    CHECK (exit_submitted IN (0, 1))
)
"""

# One OPEN position per symbol, for the reason S1's index exists: it
# makes a second entry into a name already held impossible at the
# storage layer rather than merely unlikely at the gate layer.
S2_POSITIONS_OPEN_SYMBOL_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_s2_positions_open_symbol
    ON s2_positions (symbol) WHERE status != 'CLOSED'
"""

MIGRATION_15_STATEMENTS = [
    S2_POSITIONS_TABLE,
    S2_POSITIONS_OPEN_SYMBOL_INDEX,
]

# S6's open positions.
#
# SUBMITTED is a state of its own, which s1_positions and s2_positions do
# not have. Those stores only ever see a position after a fill, because
# their entry path records nothing until one arrives. S6 records the
# submission so an ambiguous BUY -- sent, no confirmation -- is a row
# that reconciliation can find rather than an order nobody is tracking.
# A SUBMITTED row has no entry_price by construction: the column is
# nullable and `open_from_fill()` is the only thing that sets it.
#
# `variant` and `entry_session` are stored because one scanner runs in
# four sessions and each forms its own range. A position that cannot say
# which variant produced it cannot be compared with the shadow dataset
# that measured it.
S6_POSITIONS_TABLE = """
CREATE TABLE IF NOT EXISTS s6_positions (
    position_id             TEXT PRIMARY KEY,
    strategy_id             TEXT NOT NULL,
    variant                 TEXT,
    symbol                  TEXT NOT NULL,
    venue                   TEXT,
    quantity                INTEGER,
    -- NULL while SUBMITTED. Set only from the broker's actual average
    -- fill: the structural stop is compared against it, and an
    -- intended-price entry would misplace every later decision.
    entry_price             REAL,
    entry_time              TEXT,
    entry_session           TEXT,
    entry_order_id          TEXT,
    client_order_id         TEXT,
    range_minutes           INTEGER,
    range_high              REAL,
    range_low               REAL,
    entry_vwap              REAL,
    entry_ema9              REAL,
    entry_ema21             REAL,
    entry_volume_expansion  REAL,
    peak_price              REAL,
    peak_volume_expansion   REAL,
    effective_stop          REAL,
    status                  TEXT NOT NULL DEFAULT 'SUBMITTED',
    exit_reason             TEXT,
    exit_submitted          INTEGER NOT NULL DEFAULT 0,
    pending_exit_reason     TEXT,
    pending_exit_since      TEXT,
    submitted_at            TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    closed_at               TEXT,
    CHECK (status IN ('SUBMITTED', 'OPEN', 'EXIT_PENDING', 'EXIT_SUBMITTED',
                      'CLOSED')),
    CHECK (exit_submitted IN (0, 1)),
    -- An OPEN position must have a real fill behind it. The storage
    -- layer refuses the combination rather than trusting every caller.
    CHECK (status = 'SUBMITTED' OR status = 'CLOSED'
           OR (entry_price IS NOT NULL AND entry_price > 0
               AND quantity IS NOT NULL AND quantity >= 1))
)
"""

# One live row per symbol, covering SUBMITTED too: a second BUY into a
# name whose first order is still unconfirmed is exactly the duplicate
# an ambiguous submission invites.
S6_POSITIONS_OPEN_SYMBOL_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_s6_positions_open_symbol
    ON s6_positions (symbol) WHERE status != 'CLOSED'
"""

MIGRATION_16_STATEMENTS = [
    S6_POSITIONS_TABLE,
    S6_POSITIONS_OPEN_SYMBOL_INDEX,
]

#: The two figures a completed S6 trade cannot be described without, and
#: which nothing recomputes after the fact.
#:
#: `trough_price` is the mirror of `peak_price`: the peak ratchets UP and
#: is what the give-back exit reads, so the position already carries its
#: best moment and not its worst. MAE -- how far the trade went against
#: us before it worked or failed -- is therefore unanswerable from a
#: closed row, and it is the number that says whether a stop was ever
#: genuinely threatened. Recorded, never read by a decision: no exit
#: condition, score or threshold consults it.
#:
#: `exit_price` is the SELL's actual average fill. `sync_sell_fills`
#: received it from the broker and discarded it, so realised P&L on an
#: S6 trade could only ever have been estimated from the intended price
#: -- which is the same mistake `entry_price` exists to refuse on the
#: buy side.
S6_POSITIONS_TROUGH_PRICE = (
    "ALTER TABLE s6_positions ADD COLUMN trough_price REAL")
S6_POSITIONS_EXIT_PRICE = (
    "ALTER TABLE s6_positions ADD COLUMN exit_price REAL")

MIGRATION_17_STATEMENTS = [
    S6_POSITIONS_TROUGH_PRICE,
    S6_POSITIONS_EXIT_PRICE,
]

# Migration 18: which strategy an entry attempt belongs to.
#
# The global position cap could always be counted without this: the
# union of held symbols and in-flight symbols needs no attribution. A
# PER-STRATEGY cap cannot. With S1 and S6 both live under one global cap
# of 2, "may S6 enter?" is not answerable from a count of 1 -- the
# answer depends on whose slot that 1 is.
#
# Nullable on purpose. Rows written before this migration have no
# strategy, and back-filling one would be inventing attribution for
# orders whose signal is long gone. `execution/entry_limits.py` treats
# an unattributed in-flight row as consuming EVERY strategy's slot, so
# the missing value fails closed rather than silently freeing capacity.
KIS_ORDER_IDEMPOTENCY_STRATEGY_ID = (
    "ALTER TABLE kis_order_idempotency ADD COLUMN strategy_id TEXT")

MIGRATION_18_STATEMENTS = [
    KIS_ORDER_IDEMPOTENCY_STRATEGY_ID,
]

# Migration 19: NORMAL LIVE -- the session a position was left in, and
# the research path that studies exits after they happen.
#
# exit_session
# ------------
# `entry_session` has always been recorded; `exit_session` was derived
# after the fact from `closed_at`. Derivation is not the same fact: a
# REGULAR entry closed in AFTER_HOURS and an AFTER_HOURS entry closed in
# REGULAR are different trades, and the session a fill actually happened
# in is execution metadata that only the closing tick knows. Recorded
# once, immutable thereafter, on every strategy's book rather than S6's
# alone.
#
# exit_price on S1 and S2
# -----------------------
# S6 gained this in migration 17 for the same reason it is needed here:
# realised P&L measured against an intended price is not realised P&L.
# Without it neither strategy can be studied at all.
S1_POSITIONS_EXIT_SESSION = (
    "ALTER TABLE s1_positions ADD COLUMN exit_session TEXT")
S1_POSITIONS_EXIT_PRICE = (
    "ALTER TABLE s1_positions ADD COLUMN exit_price REAL")
S2_POSITIONS_EXIT_SESSION = (
    "ALTER TABLE s2_positions ADD COLUMN exit_session TEXT")
S2_POSITIONS_EXIT_PRICE = (
    "ALTER TABLE s2_positions ADD COLUMN exit_price REAL")
S6_POSITIONS_EXIT_SESSION = (
    "ALTER TABLE s6_positions ADD COLUMN exit_session TEXT")

#: One row per completed exit, for every strategy.
#:
#: Deliberately a copy rather than a join. The position row it descends
#: from stays mutable and strategy-shaped; this is the flat, immutable
#: description of one finished trade that analytics reads, and it must
#: keep meaning the same thing after the strategy's book changes shape.
#:
#: `status` is TRACKING until `tracking_end_at` passes, then COMPLETED.
#: Nothing observes a completed row again -- see config/post_exit_policy.
POST_EXIT_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS post_exit_tracking (
    tracking_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT,
    scanner_id TEXT,
    position_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    venue TEXT,
    quantity REAL,
    entry_time TEXT,
    entry_session TEXT,
    entry_price REAL,
    exit_time TEXT,
    exit_session TEXT,
    exit_price REAL,
    exit_reason TEXT,
    realized_pnl REAL,
    realized_pnl_pct REAL,
    trading_day TEXT NOT NULL,
    tracking_started_at TEXT NOT NULL,
    tracking_end_at TEXT NOT NULL,
    status TEXT NOT NULL,
    max_price_after_exit REAL,
    min_price_after_exit REAL,
    max_return_after_exit_pct REAL,
    min_return_after_exit_pct REAL,
    exit_mfe_pct REAL,
    avoided_loss_pct REAL,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

POST_EXIT_TRACKING_STRATEGY_INDEX = """
CREATE INDEX IF NOT EXISTS idx_post_exit_tracking_strategy_reason
ON post_exit_tracking (strategy_id, exit_reason)
"""

POST_EXIT_TRACKING_STATUS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_post_exit_tracking_status
ON post_exit_tracking (status, tracking_end_at)
"""

#: One row per (tracking_id, horizon). UNIQUE so a re-run records the
#: same horizon once: these are observations of a moment that has passed,
#: and a second read of it is the same fact, not a new one.
#:
#: A horizon whose price could not be read is stored with status
#: UNAVAILABLE rather than omitted. "Not yet observed" and "observed and
#: unavailable" are different, and only the second should stop retrying.
POST_EXIT_OBSERVATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS post_exit_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tracking_id TEXT NOT NULL,
    horizon TEXT NOT NULL,
    observed_at TEXT,
    price REAL,
    return_pct REAL,
    source TEXT,
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (tracking_id, horizon)
)
"""

POST_EXIT_OBSERVATIONS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_post_exit_observations_tracking
ON post_exit_observations (tracking_id)
"""

#: Every BUY refused because the strategy already sold that symbol today.
#:
#: Recorded so the policy can be judged rather than assumed: the block
#: prevents a trade, and whether preventing it was right is only knowable
#: from what the price did afterwards, which post_exit_tracking is
#: separately collecting for the exit that caused the block.
REENTRY_BLOCKS_TABLE = """
CREATE TABLE IF NOT EXISTS reentry_blocks (
    block_id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    blocked_at TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    candidate_rank INTEGER,
    candidate_score REAL,
    candidate_price REAL,
    previous_exit_price REAL,
    previous_exit_reason TEXT,
    previous_position_id TEXT,
    tracking_id TEXT,
    created_at TEXT NOT NULL
)
"""

REENTRY_BLOCKS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_reentry_blocks_day
ON reentry_blocks (trading_day, strategy_id, symbol)
"""

MIGRATION_19_STATEMENTS = [
    S1_POSITIONS_EXIT_SESSION,
    S1_POSITIONS_EXIT_PRICE,
    S2_POSITIONS_EXIT_SESSION,
    S2_POSITIONS_EXIT_PRICE,
    S6_POSITIONS_EXIT_SESSION,
    POST_EXIT_TRACKING_TABLE,
    POST_EXIT_TRACKING_STRATEGY_INDEX,
    POST_EXIT_TRACKING_STATUS_INDEX,
    POST_EXIT_OBSERVATIONS_TABLE,
    POST_EXIT_OBSERVATIONS_INDEX,
    REENTRY_BLOCKS_TABLE,
    REENTRY_BLOCKS_INDEX,
]


# Migration 20: telling a late exit RULE from a late exit ORDER.
#
# DT, 2026-08-26: the exit signal (SESSION_EXIT) fired at 23:45:10Z, by
# which time the sell route had been unavailable for 1h45m. Stored as a
# single "exit time", that trade reads as an exit rule that fired late.
# It is two facts -- when the strategy decided, and when the broker
# would accept the order -- and pooling them would teach the wrong
# lesson to every analysis built on top.
#
# `route_wait_duration_seconds` is the part that belongs to the broker
# and not to the strategy. An exit-quality statistic that includes it is
# measuring the venue.
POST_EXIT_EXIT_SIGNAL_TIME = (
    "ALTER TABLE post_exit_tracking ADD COLUMN exit_signal_time TEXT")
POST_EXIT_EXIT_SIGNAL_REASON = (
    "ALTER TABLE post_exit_tracking ADD COLUMN exit_signal_reason TEXT")
POST_EXIT_EXIT_PENDING_SINCE = (
    "ALTER TABLE post_exit_tracking ADD COLUMN exit_pending_since TEXT")
POST_EXIT_SELL_SUBMIT_TIME = (
    "ALTER TABLE post_exit_tracking ADD COLUMN sell_submit_time TEXT")
POST_EXIT_ACTUAL_SELL_TIME = (
    "ALTER TABLE post_exit_tracking ADD COLUMN actual_sell_time TEXT")
POST_EXIT_SIGNAL_TO_SUBMIT = (
    "ALTER TABLE post_exit_tracking ADD COLUMN signal_to_submit_seconds REAL")
POST_EXIT_SUBMIT_TO_FILL = (
    "ALTER TABLE post_exit_tracking ADD COLUMN submit_to_fill_seconds REAL")
POST_EXIT_ROUTE_WAIT = (
    "ALTER TABLE post_exit_tracking ADD COLUMN route_wait_duration_seconds REAL")

MIGRATION_20_STATEMENTS = [
    POST_EXIT_EXIT_SIGNAL_TIME,
    POST_EXIT_EXIT_SIGNAL_REASON,
    POST_EXIT_EXIT_PENDING_SINCE,
    POST_EXIT_SELL_SUBMIT_TIME,
    POST_EXIT_ACTUAL_SELL_TIME,
    POST_EXIT_SIGNAL_TO_SUBMIT,
    POST_EXIT_SUBMIT_TO_FILL,
    POST_EXIT_ROUTE_WAIT,
]


# Every table this schema version creates -- used by export.py's
# export_all() and by tests asserting the full table set exists.
ALL_TABLES = [
    "orders", "fills", "positions", "position_events",
    "strategy_runs", "risk_events", "kill_switch_events",
    "exit_intents", "live_entry_reservations", "kis_order_idempotency",
    "order_state_events", "shadow_audit_events", "s1_live_trades",
    "s1_risk_state", "s1_risk_peak", "s1_verification_state",
    "s1_positions", "s2_positions", "s6_positions",
    "post_exit_tracking", "post_exit_observations", "reentry_blocks",
]
