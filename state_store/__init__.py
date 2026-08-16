"""Stage 5 (roadmap Phase 5 companion, 사용자 지시서 Stage 5): local SQLite
trading state store.

Why SQLite, not more CSV files (evaluated per the user's Stage 5
instruction before writing any code -- see docs/autonomous/DECISION_LOG.md
"Stage 5" section for the full writeup):

  - order_history.csv + order_reconciliation.csv + POSITION_STORE.json are
    already three separate files correlated only by (symbol, order_date)
    or client_order_id as an informal foreign key. Nothing enforces that
    an order row, its fills, and its position record stay consistent --
    each file has its own atomic-write-plus-lock discipline, but there is
    no cross-file transaction. This is exactly the "부분 체결의 포지션
    상태 완전 반영" gap Phase 1B/Phase 5 already flagged as a known risk.
  - A local SQLite database gives real ACID transactions across orders,
    fills, and positions in one file, which plain CSV fundamentally
    cannot (a CSV write is one file at a time; combining two CSV writes
    into one atomic unit would require inventing a second lock/journal
    layer on top of CSV -- at which point it is not simpler than SQLite).

What this stage explicitly does NOT do (forbidden per the user's Stage 5
scope, absolute limits section):
  - Does not delete or modify order_history.csv, order_reconciliation.csv,
    or POSITION_STORE.json. csv_import.py is read-only against its CSV
    source.
  - Does not touch any production server or production data.
  - Does not switch the real operational order-submission path
    (paper_strategy_order.py / positions/lifecycle.py) to read from or
    write to this database. Those modules are unmodified by this stage.
    This package exists as parallel, independently-tested infrastructure;
    wiring it into the live path is a separate, explicit future decision
    (would need `NEEDS_USER_DECISION` per DECISION_LOG.md convention).

Modules:
  schema.py      -- DDL for orders/fills/positions/position_events/
                     strategy_runs/risk_events/kill_switch_events plus a
                     schema_migrations version table.
  migrations.py   -- ordered list of (version, description, sql_statements)
                     migrations; db.init_db() applies whichever haven't run.
  db.py           -- connect()/init_db()/get_schema_version().
  csv_import.py   -- read-only importer for order_history.csv rows.
  export.py       -- dump any table (or all tables) back out to CSV, for
                     audit/rollback verification -- never overwrites a
                     real operational CSV unless the caller explicitly
                     points dest_path at one (this module never does so
                     itself).
"""
