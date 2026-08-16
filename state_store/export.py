"""Export/rollback tooling for the Stage 5 state database.

export_table()/export_all() are read-only against the database and
write-only against `dest_path`/`dest_dir` -- they never touch a real
operational CSV unless the caller explicitly points them at one (this
module never chooses that path itself, and no call site in this codebase
does either). Intended use is audit ("what does the database currently
believe?") and disaster recovery ("dump everything back to CSV before
attempting a risky migration").

reset_schema() is the "rollback" half: it drops every table this package
created and re-applies migrations from scratch. It only ever operates on
the SQLite database file itself -- never on the CSVs the database was
imported from.
"""

import pandas as pd

from state_store.db import init_db
from state_store.schema import ALL_TABLES


def export_table(conn, table_name, dest_path):
    if table_name not in ALL_TABLES:
        raise ValueError(f"Unknown table: {table_name!r}; must be one of {ALL_TABLES}")
    df = pd.read_sql_query(f"SELECT * FROM {table_name}", conn)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(dest_path, index=False)
    return len(df)


def export_all(conn, dest_dir):
    """Export every table to <dest_dir>/<table_name>.csv. Returns a dict
    of table_name -> row count exported."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for table_name in ALL_TABLES:
        counts[table_name] = export_table(conn, table_name, dest_dir / f"{table_name}.csv")
    return counts


def reset_schema(conn):
    """Drop every table (and schema_migrations) and re-run migrations from
    scratch. Only ever touches the SQLite database this `conn` points at --
    the operational CSVs this database may have been imported from are
    untouched, and re-importing them afterwards is the caller's explicit
    choice via csv_import.py."""
    conn.execute("PRAGMA foreign_keys = OFF")
    for table_name in [*ALL_TABLES, "schema_migrations"]:
        conn.execute(f"DROP TABLE IF EXISTS {table_name}")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    return init_db(conn)
