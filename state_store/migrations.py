"""Ordered schema migrations. Each entry is (version, description, sql_statements).

Versions must be applied in order starting from 1; db.init_db() records
each applied version in schema_migrations so re-running it against an
already-migrated database is a safe no-op (idempotent), and a future
migration 2+ only needs to add a new tuple here -- existing tuples are
never edited in place once released, matching the project's general
"never silently rewrite history" convention (see order_intent_ledger.py's
own append-only design).
"""

from state_store.schema import MIGRATION_1_STATEMENTS

MIGRATIONS = [
    (1, "initial schema: orders/fills/positions/position_events/strategy_runs/risk_events/kill_switch_events",
     MIGRATION_1_STATEMENTS),
]

CURRENT_SCHEMA_VERSION = MIGRATIONS[-1][0]
