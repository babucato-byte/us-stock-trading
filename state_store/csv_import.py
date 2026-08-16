"""Read-only CSV -> SQLite migration helpers.

Both functions here only ever call pandas.read_csv() on the source path --
never write, never truncate, never delete. This is a hard requirement from
the user's Stage 5 instruction ("금지: 운영 CSV 삭제"): the CSV remains the
live operational file; this module produces a queryable copy alongside it,
nothing more.

order_history.csv's schema has drifted over the project's history --
older rows may have only `symbol,order_date` columns, while
paper_strategy_order.REQUIRED_HISTORY_COLUMNS now expects
`symbol,order_date,mode,dry_run,status`. import_order_history() tolerates
either: any column absent in the source file is imported as NULL/None
rather than raising, since fail-closed here would mean "cannot build an
audit copy at all," which is disproportionate for a read-only,
non-safety-critical convenience import.
"""

import pandas as pd

from state_store.db import now_iso

ORDER_HISTORY_OPTIONAL_COLUMNS = ["mode", "dry_run", "status"]


def import_order_history(csv_path, conn, *, source="order_history.csv"):
    """Import every row of order_history.csv into the orders table.

    Idempotent via INSERT OR IGNORE against orders' (symbol, order_date, side)
    uniqueness constraint -- re-running this import after new rows have
    been appended to the CSV only inserts the new ones. `side` is not
    present in order_history.csv itself (it is an entry-only ledger by
    convention -- see paper_strategy_order.py), so it is always recorded
    as 'buy' here; this importer is for entry-order history specifically,
    not a general order log.

    Returns {"rows_read": int, "rows_imported": int, "rows_skipped": int}.
    Raises FileNotFoundError if csv_path does not exist -- the caller
    decides whether a missing file is acceptable (e.g. a fresh deployment
    that has never placed an order yet).
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"{csv_path} does not exist; nothing to import")

    df = pd.read_csv(csv_path)
    rows_read = len(df)
    imported = 0
    now = now_iso()

    for _, row in df.iterrows():
        symbol = row.get("symbol")
        order_date = row.get("order_date")
        if pd.isna(symbol) or pd.isna(order_date):
            continue  # cannot key a row missing its natural key; skip, don't fail the whole import
        values = {
            "symbol": str(symbol),
            "order_date": str(order_date),
            "mode": _clean(row.get("mode")),
            "dry_run": _clean_bool(row.get("dry_run")),
            "status": _clean(row.get("status")),
        }
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO orders
                (client_order_id, symbol, order_date, side, mode, dry_run, status,
                 requested_qty, broker_order_id, source, created_at)
            VALUES (NULL, ?, ?, 'buy', ?, ?, ?, NULL, NULL, ?, ?)
            """,
            (values["symbol"], values["order_date"], values["mode"], values["dry_run"],
             values["status"], source, now),
        )
        if cursor.rowcount:
            imported += 1
    conn.commit()
    return {"rows_read": rows_read, "rows_imported": imported, "rows_skipped": rows_read - imported}


def import_order_reconciliation(csv_path, conn, *, source="order_reconciliation.csv"):
    """Import every row of order_reconciliation.csv into the fills table.
    Same read-only, idempotent-by-natural-key contract as
    import_order_history(); keyed here by (client_order_id, recorded_at)
    via INSERT OR IGNORE on fills having no unique constraint of its own,
    so this checks for an existing identical (client_order_id,
    last_reconciled_at) pair before inserting to avoid duplicate rows on
    repeated imports.
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"{csv_path} does not exist; nothing to import")

    df = pd.read_csv(csv_path)
    rows_read = len(df)
    imported = 0
    now = now_iso()

    for _, row in df.iterrows():
        client_order_id = row.get("client_order_id")
        symbol = row.get("symbol")
        order_date = row.get("order_date")
        if pd.isna(client_order_id) or pd.isna(symbol) or pd.isna(order_date):
            continue
        last_reconciled_at = _clean(row.get("last_reconciled_at"))
        existing = conn.execute(
            "SELECT 1 FROM fills WHERE client_order_id = ? AND recorded_at = ?",
            (str(client_order_id), last_reconciled_at or now),
        ).fetchone()
        if existing:
            continue
        conn.execute(
            """
            INSERT INTO fills
                (client_order_id, symbol, order_date, filled_qty, remaining_qty,
                 average_fill_price, broker_status, local_status, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(client_order_id), str(symbol), str(order_date),
                _clean_int(row.get("filled_qty")), _clean_int(row.get("remaining_qty")),
                _clean_float(row.get("average_fill_price")),
                _clean(row.get("broker_status")), _clean(row.get("local_status")),
                last_reconciled_at or now,
            ),
        )
        imported += 1
    conn.commit()
    return {"rows_read": rows_read, "rows_imported": imported, "rows_skipped": rows_read - imported}


def _clean(value):
    return None if pd.isna(value) else str(value)


def _clean_bool(value):
    if pd.isna(value):
        return None
    if isinstance(value, bool):
        return int(value)
    return int(str(value).strip().lower() == "true")


def _clean_int(value):
    return None if pd.isna(value) else int(value)


def _clean_float(value):
    return None if pd.isna(value) else float(value)
