"""Position store (Stage 4 / CODEX-028): SQLite is the single canonical
source of truth for position lifecycle state; POSITION_STORE.json is a
best-effort, fully-regenerable *projection* of it, never authoritative.

Before CODEX-028, this module's canonical store WAS the JSON file, written
via temp-file + flush + fsync + os.replace under an fcntl.flock -- safe on
its own, but positions/lifecycle.py's exit-reconciliation path also writes
to a *separate* SQLite database (state_store/exit_intent_ledger.py,
CODEX-024) for durable exit-intent reservation. Those two writes were not
transactionally linked: `_apply_exit_fill_progress()` could commit a fill
observation to SQLite's exit_intents table and then have the JSON
position write fail (or the process crash) before ever recording the
matching remaining_qty/realized_pnl change, permanently losing track of a
real fill (CODEX-028's reproduction: SQLite confirms qty=4, JSON position
never updates, a later cumulative-10 reconciliation only applies delta
6-4=2... err 10-4=6, leaving state=CLOSED with remaining_qty=4 and PnL
for only 6 of 10 shares).

The fix: `positions` and `position_events` (both already part of
state_store/schema.py's Stage 5 schema, previously unused by this module)
now hold the position record's full canonical state -- filled_qty,
remaining_qty, average_fill_price, realized_pnl, state, and the full
state_history (reconstructed from position_events rows, one per
transition). locked_position() accepts the SAME SQLite connection
positions/lifecycle.py already threads through its exit-intent
reservation/reconciliation calls, so a position's state change and its
exit_intents mutation commit together in one atomic SQLite transaction --
the exact gap CODEX-028 (and CODEX-024's own "not a single transaction"
remaining risk) identifies. POSITION_STORE.json is written AFTER that
commit succeeds, wrapped in its own try/except that can never affect
whether the SQLite commit "took" -- a JSON write failure is recorded as
positions.projection_status='FAILED' and otherwise ignored, never rolled
back into, and never re-derived from (CODEX-028 requirement #3: "JSON에서
remaining_qty/PnL/state를 다시 읽어 계산하지 않음").

Scope note (see docs/autonomous/DECISION_LOG.md, CODEX-024/026/028/029/030
section): only `positions`/`position_events`/`exit_intents` became
canonical here. `orders`/`fills` (also present in state_store/schema.py)
remain out of scope -- the entry-order dedup/audit trail
(order_history.csv, order_intent_ledger.csv via paper_strategy_order.py)
is untouched by this change and was never implicated in CODEX-028's
reproduction, which is specifically about exit-side remaining_qty/PnL/
state drifting from the durable exit-intent ledger.

The file lock (`_store_lock`, unchanged from before CODEX-028) remains
the cross-process concurrency primitive -- it serializes the entire
read-decide-act-write span exactly as before; SQLite's own locking is a
secondary, harmless safety net given WAL mode allows only one writer at a
time regardless.

A record that cannot be parsed, or is missing required fields, or holds a
state value states.py doesn't recognize, is never silently treated as
healthy -- load_position()/load_all() surface it with state forced to
states.FAIL_CLOSED_STATE (RECOVERY_REQUIRED) rather than raising and
rather than guessing, exactly as before CODEX-028. A *whole-store*
failure (the SQLite database file itself is corrupted, unreadable, or
missing expected columns) raises PositionStoreCorruptedError, never
silently returns {} -- also unchanged in spirit from before CODEX-028,
just now diagnosing the SQLite file instead of the JSON file (a corrupted
POSITION_STORE.json no longer implies data loss at all, since it is only
a projection SQLite can always regenerate -- see regenerate_projection()).
"""

import json
import os
import fcntl
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from positions import states
from state_store import db as state_db

BASE_DIR = Path(__file__).resolve().parent.parent
STORE_FILE = BASE_DIR / "POSITION_STORE.json"
LOCK_TIMEOUT_SECONDS = 5.0

# Fields every position record must have. Anything missing at load time is
# treated as corrupted (fail-closed), not silently defaulted. `state_history`
# is handled separately (reconstructed from position_events), so it is not
# a SQLite `positions` column but is still part of every in-memory record.
_REQUIRED_FIELDS = [
    "position_id", "client_order_id", "broker_order_id", "strategy_id",
    "strategy_version", "symbol", "state", "state_history",
    "requested_qty", "filled_qty", "remaining_qty", "average_fill_price",
    "stop_price", "target_1_price", "target_2_price",
    "realized_pnl", "unrealized_pnl", "entry_time",
    "last_reconciled_at", "exit_reason",
]

# The SQLite `positions` table columns backing every _REQUIRED_FIELDS entry
# except state_history (which lives in position_events instead).
_POSITION_COLUMNS = [f for f in _REQUIRED_FIELDS if f != "state_history"]

# Extra bookkeeping columns (CODEX-028) not part of the in-memory record --
# used only by _write_projection_best_effort()/check_store_health().
_REQUIRED_SCHEMA_COLUMNS = set(_POSITION_COLUMNS) | {
    "updated_at", "projection_status", "projection_updated_at",
}


class PositionStoreError(Exception):
    """Raised for lock/IO failures against the position store."""


class PositionStoreCorruptedError(PositionStoreError):
    """CODEX-025/CODEX-028: raised by load_all()/load_non_terminal() when
    the canonical SQLite database is unreadable, corrupted, or missing
    expected schema -- instead of silently returning {}.

    Whole-database corruption means every position that may have been
    recorded is now of unknown truth -- there could be a live, unmanaged
    open position this process can no longer read. Returning an empty
    dict from that state is indistinguishable from "this account has
    genuinely never traded," which is a fail-open outcome dressed up as
    fail-closed. Callers (positions/lifecycle.py::recover_on_restart())
    must treat this exception as "the whole store is unavailable," not
    attempt to catch it and proceed as if positions were simply absent.

    Note: a corrupted POSITION_STORE.json (the projection) no longer
    raises this -- it is regenerable from SQLite at any time via
    regenerate_projection() and was never itself authoritative.
    """


# Store-health classification (CODEX-025/CODEX-028). Purely diagnostic/
# reporting -- load_all()/load_non_terminal() only ever draw the coarser
# healthy-vs-PositionStoreCorruptedError distinction for control flow, but
# check_store_health() exposes the finer-grained reason for logs/dashboards.
STORE_STATUS_MISSING = "MISSING_STORE"
STORE_STATUS_VALID_EMPTY = "VALID_EMPTY"
STORE_STATUS_VALID_WITH_POSITIONS = "VALID_WITH_POSITIONS"
STORE_STATUS_CORRUPTED = "CORRUPTED_STORE"
STORE_STATUS_SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
STORE_STATUS_READ_FAILURE = "READ_FAILURE"


def _resolve_db_path():
    return state_db._resolve_db_path()


def check_store_health():
    """Read-only diagnostic classification of the canonical SQLite store's
    current state. Never raises; always returns {"status": ..., "reason": ...}.

    Deliberately does NOT call state_db.init_db()/open_db() -- running
    migrations against a database whose `positions` table already exists
    with the wrong shape would itself raise ("table positions already
    exists", since migration 1's CREATE TABLE has no IF NOT EXISTS),
    which would misclassify a genuine schema mismatch as CORRUPTED_STORE.
    This function only ever reads (PRAGMA table_info / sqlite_master),
    never migrates, so a schema mismatch is diagnosed directly instead.
    """
    db_path = _resolve_db_path()
    if not db_path.exists():
        return {"status": STORE_STATUS_MISSING, "reason": "no database file yet (never traded, or fresh install)"}
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
    except sqlite3.OperationalError as exc:
        return {"status": STORE_STATUS_READ_FAILURE, "reason": f"could not open database: {exc}"}
    try:
        try:
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(positions)").fetchall()}
        except sqlite3.Error as exc:
            return {"status": STORE_STATUS_CORRUPTED, "reason": f"could not read positions table: {exc}"}
        if not cols:
            return {"status": STORE_STATUS_SCHEMA_MISMATCH, "reason": "positions table does not exist"}
        missing = _REQUIRED_SCHEMA_COLUMNS - cols
        if missing:
            return {"status": STORE_STATUS_SCHEMA_MISMATCH, "reason": f"positions table missing columns: {sorted(missing)}"}
        try:
            count = conn.execute("SELECT COUNT(*) AS n FROM positions").fetchone()["n"]
        except sqlite3.Error as exc:
            return {"status": STORE_STATUS_CORRUPTED, "reason": f"could not read positions rows: {exc}"}
        if count == 0:
            return {"status": STORE_STATUS_VALID_EMPTY, "reason": None}
        return {"status": STORE_STATUS_VALID_WITH_POSITIONS, "reason": None}
    except sqlite3.OperationalError as exc:
        return {"status": STORE_STATUS_READ_FAILURE, "reason": f"operational error: {exc}"}
    finally:
        conn.close()


def _resolve_store_path():
    override = os.environ.get("POSITION_STORE_FILE")
    return Path(override) if override else STORE_FILE


def _resolve_lock_path():
    return _resolve_store_path().with_suffix(".lock")


@contextmanager
def _store_lock(timeout=LOCK_TIMEOUT_SECONDS):
    """Process-level exclusive lock guarding read-modify-write of a
    position. A dead lock holder's flock is released by the kernel on
    process exit (same reasoning as kill_switch_state.py's _state_lock),
    so a stale .lock file left by a crashed process never blocks the next
    acquirer. Genuine contention that outlasts `timeout` fails closed:
    PositionStoreError is raised and nothing is written."""
    lock_path = _resolve_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "a+")
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise PositionStoreError(
                        f"Could not acquire position store lock ({lock_path}) within {timeout}s"
                    )
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        finally:
            lock_file.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def new_position_id():
    return f"pos_{uuid.uuid4().hex[:16]}"


def _atomic_write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as tmp_file:
            json.dump(payload, tmp_file, indent=2, sort_keys=True)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_name, path)
    except Exception:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise


def _open_db_or_raise():
    """Open the canonical SQLite connection (running migrations), raising
    PositionStoreCorruptedError -- never a bare sqlite3 exception -- for
    anything a caller should treat as "the whole store is unavailable"."""
    try:
        conn = state_db.open_db()
    except sqlite3.DatabaseError as exc:
        raise PositionStoreCorruptedError(
            f"Position store database is corrupted or not a valid SQLite file: {exc}"
        ) from exc
    except sqlite3.OperationalError as exc:
        raise PositionStoreCorruptedError(
            f"Position store database could not be opened: {exc}"
        ) from exc
    except state_db.StateStoreError as exc:
        raise PositionStoreCorruptedError(f"Position store database migration failed: {exc}") from exc
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(positions)").fetchall()}
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise PositionStoreCorruptedError(f"Position store database could not be read: {exc}") from exc
    missing = _REQUIRED_SCHEMA_COLUMNS - cols
    if missing:
        conn.close()
        raise PositionStoreCorruptedError(
            f"Position store positions table is missing expected columns: {sorted(missing)}"
        )
    return conn


def _fail_closed_record(position_id, reason, raw=None):
    """Build a RECOVERY_REQUIRED record for a position_id whose stored
    record could not be trusted. Preserves whatever fields *did* parse
    (symbol, strategy_id, etc.) purely for operator visibility -- none of
    the preserved fields are trusted for control-flow decisions."""
    record = {field: (raw or {}).get(field) for field in _REQUIRED_FIELDS}
    record["position_id"] = position_id
    record["state"] = states.FAIL_CLOSED_STATE
    history = record.get("state_history")
    if not isinstance(history, list):
        history = []
    history.append({"state": states.FAIL_CLOSED_STATE, "at": now_iso(), "reason": reason})
    record["state_history"] = history
    return record


def _validate_or_fail_closed(position_id, raw):
    """Return a trustworthy record, or a fail-closed RECOVERY_REQUIRED
    record if `raw` is missing fields or holds a state states.py doesn't
    recognize. Never raises -- a corrupted record is data, not a bug."""
    if not isinstance(raw, dict):
        return _fail_closed_record(position_id, "record is not an object")
    missing = [f for f in _REQUIRED_FIELDS if f not in raw]
    if missing:
        return _fail_closed_record(position_id, f"missing fields: {missing}", raw)
    if not states.is_valid_state(raw.get("state")):
        return _fail_closed_record(position_id, f"unrecognized state: {raw.get('state')!r}", raw)
    if not isinstance(raw.get("state_history"), list):
        return _fail_closed_record(position_id, "state_history is not a list", raw)
    return raw


def _row_to_record(conn, row):
    """Reconstruct a full in-memory record (including state_history) from
    a `positions` row plus its `position_events` rows -- the canonical
    read path (CODEX-028)."""
    record = {col: row[col] for col in _POSITION_COLUMNS}
    events = conn.execute(
        "SELECT state, reason, occurred_at FROM position_events WHERE position_id = ? ORDER BY event_id ASC",
        (row["position_id"],),
    ).fetchall()
    record["state_history"] = [
        {"state": e["state"], "at": e["occurred_at"], "reason": e["reason"]} for e in events
    ]
    return _validate_or_fail_closed(row["position_id"], record)


def _load_all_from_db(conn):
    rows = conn.execute("SELECT * FROM positions").fetchall()
    return {row["position_id"]: _row_to_record(conn, row) for row in rows}


def _write_position_row(conn, record):
    """UPSERT the scalar fields of `record` into `positions`."""
    now = now_iso()
    conn.execute(
        "INSERT INTO positions "
        "(position_id, client_order_id, broker_order_id, strategy_id, strategy_version, "
        " symbol, state, requested_qty, filled_qty, remaining_qty, average_fill_price, "
        " stop_price, target_1_price, target_2_price, realized_pnl, unrealized_pnl, "
        " entry_time, last_reconciled_at, exit_reason, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(position_id) DO UPDATE SET "
        "client_order_id=excluded.client_order_id, broker_order_id=excluded.broker_order_id, "
        "strategy_id=excluded.strategy_id, strategy_version=excluded.strategy_version, "
        "symbol=excluded.symbol, state=excluded.state, requested_qty=excluded.requested_qty, "
        "filled_qty=excluded.filled_qty, remaining_qty=excluded.remaining_qty, "
        "average_fill_price=excluded.average_fill_price, stop_price=excluded.stop_price, "
        "target_1_price=excluded.target_1_price, target_2_price=excluded.target_2_price, "
        "realized_pnl=excluded.realized_pnl, unrealized_pnl=excluded.unrealized_pnl, "
        "entry_time=excluded.entry_time, last_reconciled_at=excluded.last_reconciled_at, "
        "exit_reason=excluded.exit_reason, updated_at=excluded.updated_at",
        (
            record["position_id"], record["client_order_id"], record["broker_order_id"],
            record["strategy_id"], record["strategy_version"], record["symbol"], record["state"],
            record["requested_qty"], record["filled_qty"], record["remaining_qty"],
            record["average_fill_price"], record["stop_price"], record["target_1_price"],
            record["target_2_price"], record["realized_pnl"], record["unrealized_pnl"],
            record["entry_time"], record["last_reconciled_at"], record["exit_reason"], now,
        ),
    )


def _insert_new_events(conn, position_id, new_events):
    for event in new_events:
        conn.execute(
            "INSERT INTO position_events (position_id, state, reason, occurred_at) VALUES (?, ?, ?, ?)",
            (position_id, event["state"], event.get("reason"), event["at"]),
        )


def _write_projection_best_effort(conn, position_id):
    """Regenerate the full POSITION_STORE.json projection from SQLite and
    write it atomically. Best-effort: any failure here is caught, recorded
    via positions.projection_status, and never raised -- SQLite has
    already committed by the time this runs, so a projection failure must
    never look like (or cause) a trading-state rollback (CODEX-028
    requirement #5)."""
    try:
        payload = {"positions": _load_all_from_db(conn)}
        _atomic_write(_resolve_store_path(), payload)
        status = "OK"
    except Exception:
        status = "FAILED"
    try:
        conn.execute(
            "UPDATE positions SET projection_status = ?, projection_updated_at = ? WHERE position_id = ?",
            (status, now_iso(), position_id),
        )
        conn.commit()
    except Exception:
        pass  # projection bookkeeping itself is also best-effort
    return status


def regenerate_projection():
    """Rebuild POSITION_STORE.json from scratch from SQLite -- the
    projection must always be reconstructable from the canonical store
    (CODEX-028 requirement #10), independent of whatever the file
    currently contains (or fails to)."""
    conn = _open_db_or_raise()
    try:
        payload = {"positions": _load_all_from_db(conn)}
        _atomic_write(_resolve_store_path(), payload)
        conn.execute(
            "UPDATE positions SET projection_status = 'OK', projection_updated_at = ?",
            (now_iso(),),
        )
        conn.commit()
        return payload
    finally:
        conn.close()


def load_position(position_id):
    """Return the record for `position_id`, or None if it has never been
    created. A corrupted record for an id that DOES exist is returned as a
    RECOVERY_REQUIRED record, never as None (None must mean "never
    existed", not "exists but unreadable")."""
    try:
        conn = _open_db_or_raise()
    except PositionStoreCorruptedError as exc:
        return _fail_closed_record(position_id, f"position store is corrupted: {exc}")
    try:
        row = conn.execute("SELECT * FROM positions WHERE position_id = ?", (position_id,)).fetchone()
        if row is None:
            return None
        return _row_to_record(conn, row)
    except sqlite3.DatabaseError as exc:
        return _fail_closed_record(position_id, f"position store read failed: {exc}")
    finally:
        conn.close()


def load_all():
    """Return {position_id: record} for every position in the store, each
    individually validated/fail-closed. One corrupted *record* never hides
    or invalidates the others (see _validate_or_fail_closed).

    A corrupted *database* (CODEX-025/CODEX-028) is a different failure
    mode and must not be conflated with "no positions": raises
    PositionStoreCorruptedError rather than returning {}, since an empty
    dict here is indistinguishable from a legitimately fresh install and
    would cause restart recovery to skip broker reconciliation for
    positions that may actually still be open.
    """
    conn = _open_db_or_raise()
    try:
        return _load_all_from_db(conn)
    except sqlite3.DatabaseError as exc:
        raise PositionStoreCorruptedError(f"Position store could not be read: {exc}") from exc
    finally:
        conn.close()


def load_non_terminal():
    """Positions whose state is not in states.TERMINAL_STATES -- exactly
    the set restart recovery needs to act on."""
    return {
        position_id: record
        for position_id, record in load_all().items()
        if record["state"] in states.NON_TERMINAL_STATES
    }


def create_position(strategy_id, strategy_version, symbol, client_order_id, requested_qty,
                     lock_timeout=LOCK_TIMEOUT_SECONDS):
    """Create a new position record in SETUP_DETECTED. Returns the record."""
    position_id = new_position_id()
    record = {
        "position_id": position_id,
        "client_order_id": client_order_id,
        "broker_order_id": None,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "symbol": symbol,
        "state": states.SETUP_DETECTED,
        "state_history": [{"state": states.SETUP_DETECTED, "at": now_iso(), "reason": "created"}],
        "requested_qty": requested_qty,
        "filled_qty": 0,
        "remaining_qty": 0,
        "average_fill_price": None,
        "stop_price": None,
        "target_1_price": None,
        "target_2_price": None,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "entry_time": None,
        "last_reconciled_at": None,
        "exit_reason": None,
    }
    with _store_lock(lock_timeout):
        conn = _open_db_or_raise()
        try:
            _write_position_row(conn, record)
            _insert_new_events(conn, position_id, record["state_history"])
            conn.commit()
            _write_projection_best_effort(conn, position_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return record


def save_position(record, lock_timeout=LOCK_TIMEOUT_SECONDS):
    """Persist `record` as-is (caller is responsible for state-transition
    validation before calling this -- see positions/lifecycle.py). Requires
    record["position_id"] to already exist in the store."""
    position_id = record["position_id"]
    with _store_lock(lock_timeout):
        conn = _open_db_or_raise()
        try:
            existing = conn.execute(
                "SELECT position_id FROM positions WHERE position_id = ?", (position_id,)
            ).fetchone()
            if existing is None:
                raise PositionStoreError(f"Cannot save unknown position_id {position_id!r}")
            already_persisted = conn.execute(
                "SELECT COUNT(*) AS n FROM position_events WHERE position_id = ?", (position_id,)
            ).fetchone()["n"]
            _write_position_row(conn, record)
            _insert_new_events(conn, position_id, record["state_history"][already_persisted:])
            conn.commit()
            _write_projection_best_effort(conn, position_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return record


class PositionAlreadyHandled(Exception):
    """Raised by locked_position() callers (by convention, not by the
    context manager itself -- see below) when the position's freshly-read
    state is no longer one they're willing to act on, e.g. an exit was
    already submitted by another caller that won the lock first."""


@contextmanager
def locked_position(position_id, lock_timeout=LOCK_TIMEOUT_SECONDS, conn=None):
    """Hold the position store's lock across an entire read-decide-act-write
    span and yield the freshly-read, validated record as a mutable dict.

    This is the one true fix for duplicate-exit prevention: a naive
    "read state, decide, call the broker, then save" sequence is racy if
    the lock is only held during the final save -- two near-simultaneous
    callers could both read the same pre-exit state, both decide to
    submit, and both actually call the broker before either gets to
    record the outcome. Here, the lock is acquired *before* the state is
    even read, and is not released until the `with` block exits, so a
    second concurrent caller for the same position_id blocks (up to
    lock_timeout) until the first caller's entire decide-act-write
    sequence (including any broker call the caller makes inside the
    `with` block) has completed and been persisted.

    CODEX-028: `conn`, if supplied, is reused for this block's SQLite
    writes (and committed once when the block exits normally) instead of
    opening a separate connection -- this is how positions/lifecycle.py's
    exit-intent reservation/reconciliation calls (state_store/
    exit_intent_ledger.py, called with commit=False) end up committed in
    the SAME SQLite transaction as this block's position/position_events
    writes, closing the "SQLite intent committed but position write lost"
    gap CODEX-028 exists to fix. A self-opened connection (conn=None,
    the common case) is committed and closed the same way; the only
    difference is ownership of when to close it.

    The caller is responsible for: checking record["state"] is one they
    expect to act on (raising/no-op otherwise), doing whatever broker
    call or other side effect is needed, mutating `record` in place
    (including calling `positions.states.validate_transition(old, new)`
    itself before setting record["state"] = new -- this context manager
    does not validate the transition for you), and appending to
    record["state_history"] if it does change state. On successful exit
    (no exception raised inside the `with` block, including a `return`
    from inside it -- Python still runs __exit__ normally), the mutated
    record's SQLite writes are committed while the lock is still held,
    and a best-effort JSON projection is written after that commit. On
    exception, the SQLite transaction is rolled back and nothing is
    persisted -- the store is left exactly as it was before the `with`
    block, so a broker call that raised partway through never gets
    silently recorded as if it had happened.

    Raises PositionStoreError if the store is corrupted or position_id is
    unknown to it.
    """
    owns_conn = conn is None
    with _store_lock(lock_timeout):
        active_conn = conn if conn is not None else _open_db_or_raise()
        try:
            row = active_conn.execute(
                "SELECT * FROM positions WHERE position_id = ?", (position_id,)
            ).fetchone()
            if row is None:
                raise PositionStoreError(f"Cannot lock unknown position_id {position_id!r}")
            record = _row_to_record(active_conn, row)
            previous_event_count = len(record["state_history"])
            yield record
            _write_position_row(active_conn, record)
            _insert_new_events(active_conn, position_id, record["state_history"][previous_event_count:])
            active_conn.commit()
            _write_projection_best_effort(active_conn, position_id)
        except Exception:
            active_conn.rollback()
            raise
        finally:
            if owns_conn:
                active_conn.close()
