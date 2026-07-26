"""Ordered schema migrations. Each entry is (version, description, sql_statements).

Versions must be applied in order starting from 1; db.init_db() records
each applied version in schema_migrations so re-running it against an
already-migrated database is a safe no-op (idempotent), and a future
migration 2+ only needs to add a new tuple here -- existing tuples are
never edited in place once released, matching the project's general
"never silently rewrite history" convention (see order_intent_ledger.py's
own append-only design).
"""

from state_store.schema import (
    MIGRATION_1_STATEMENTS, MIGRATION_2_STATEMENTS, MIGRATION_3_STATEMENTS, MIGRATION_4_STATEMENTS,
    MIGRATION_5_STATEMENTS,
)

MIGRATIONS = [
    (1, "initial schema: orders/fills/positions/position_events/strategy_runs/risk_events/kill_switch_events",
     MIGRATION_1_STATEMENTS),
    (2, "CODEX-024: durable exit_intents ledger", MIGRATION_2_STATEMENTS),
    (3, "CODEX-028: positions.projection_status/projection_updated_at for canonical-SQLite/JSON-projection split",
     MIGRATION_3_STATEMENTS),
    (4, "CODEX-031: durable live_entry_reservations ledger for authoritative budget/count tracking",
     MIGRATION_4_STATEMENTS),
    (5, "CODEX-034: live_entry_reservations.client_order_id for ambiguous-failure reconciliation",
     MIGRATION_5_STATEMENTS),
]

CURRENT_SCHEMA_VERSION = MIGRATIONS[-1][0]
