"""Atomic, lock-protected persistent store for position records (Stage 4,
roadmap Phase 5).

Same technique as kill_switch_state.py / order_intent_ledger.py / order
history: a single JSON file, written via temp-file + flush + fsync +
os.replace, guarded by an fcntl.flock. Unlike kill_switch_state.py (one
global record), this store holds many positions keyed by position_id in a
single file -- a scalping strategy with MAX_OPEN_POSITIONS=5 has at most a
handful of concurrently-open positions, so one small file with one lock is
simpler and safer than one file per position (no risk of two positions'
writes racing on unrelated files, no directory-listing needed to enumerate
"all open positions" for restart recovery).

A record that cannot be parsed, or is missing required fields, or holds a
state value states.py doesn't recognize, is never silently treated as
healthy -- load_position()/load_all() surface it with state forced to
states.FAIL_CLOSED_STATE (RECOVERY_REQUIRED) rather than raising and rather
than guessing. This mirrors kill_switch_state.py's `_fail_closed_snapshot`
pattern but is deliberately record-level (one bad position record does not
taint every other position in the same file), the same "don't let one
corrupted row block everything else" design scalping_watchlist/repository.py
uses for individual watchlist rows.
"""

import fcntl
import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from positions import states

BASE_DIR = Path(__file__).resolve().parent.parent
STORE_FILE = BASE_DIR / "POSITION_STORE.json"
LOCK_TIMEOUT_SECONDS = 5.0

# Fields every position record must have. Anything missing at load time is
# treated as corrupted (fail-closed), not silently defaulted.
_REQUIRED_FIELDS = [
    "position_id", "client_order_id", "broker_order_id", "strategy_id",
    "strategy_version", "symbol", "state", "state_history",
    "requested_qty", "filled_qty", "remaining_qty", "average_fill_price",
    "stop_price", "target_1_price", "target_2_price",
    "realized_pnl", "unrealized_pnl", "entry_time",
    "last_reconciled_at", "exit_reason",
]


class PositionStoreError(Exception):
    """Raised for lock/IO failures against the position store."""


def _resolve_store_path():
    override = os.environ.get("POSITION_STORE_FILE")
    return Path(override) if override else STORE_FILE


def _resolve_lock_path():
    return _resolve_store_path().with_suffix(".lock")


@contextmanager
def _store_lock(timeout=LOCK_TIMEOUT_SECONDS):
    """Process-level exclusive lock guarding read-modify-write of the
    position store file. A dead lock holder's flock is released by the
    kernel on process exit (same reasoning as kill_switch_state.py's
    _state_lock), so a stale .lock file left by a crashed process never
    blocks the next acquirer. Genuine contention that outlasts `timeout`
    fails closed: PositionStoreError is raised and the store file is left
    completely untouched."""
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


def _empty_file_payload():
    return {"positions": {}}


def _read_raw():
    """Read the raw {"positions": {...}} payload from disk. A missing file
    is legitimately "no positions yet" (fresh install / never traded) --
    that is NOT the same as a corrupted file and must not fail closed."""
    path = _resolve_store_path()
    if not path.exists():
        return _empty_file_payload()
    try:
        with open(path) as f:
            payload = json.load(f)
    except Exception:
        # The whole file is unreadable -- every position in it is now of
        # unknown truth. Surface as a special sentinel the callers below
        # convert into per-position RECOVERY_REQUIRED records, rather than
        # silently returning "no positions" (which would look like nothing
        # was ever open) or raising (which would make load_all() unusable
        # during exactly the restart-recovery scan that needs it most).
        return {"positions": {}, "_file_corrupted": True}
    if not isinstance(payload, dict) or not isinstance(payload.get("positions"), dict):
        return {"positions": {}, "_file_corrupted": True}
    return payload


def _fail_closed_record(position_id, reason, raw=None):
    """Build a RECOVERY_REQUIRED record for a position_id whose stored
    record could not be trusted. Preserves whatever raw fields *did* parse
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


def load_position(position_id):
    """Return the record for `position_id`, or None if it has never been
    created. A corrupted record for an id that DOES exist is returned as a
    RECOVERY_REQUIRED record, never as None (None must mean "never
    existed", not "exists but unreadable")."""
    payload = _read_raw()
    if payload.get("_file_corrupted"):
        # We don't know if position_id ever existed. Fail closed: report it
        # as needing recovery rather than silently reporting "not found".
        return _fail_closed_record(position_id, "position store file is corrupted")
    raw = payload["positions"].get(position_id)
    if raw is None:
        return None
    return _validate_or_fail_closed(position_id, raw)


def load_all():
    """Return {position_id: record} for every position in the store, each
    individually validated/fail-closed. One corrupted record never hides or
    invalidates the others."""
    payload = _read_raw()
    if payload.get("_file_corrupted"):
        return {}
    return {
        position_id: _validate_or_fail_closed(position_id, raw)
        for position_id, raw in payload["positions"].items()
    }


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
        payload = _read_raw()
        if payload.get("_file_corrupted"):
            raise PositionStoreError(
                "Refusing to create a new position: the position store file is corrupted. "
                "Resolve the existing file (manual recovery) before creating new positions."
            )
        payload["positions"][position_id] = record
        payload.pop("_file_corrupted", None)
        _atomic_write(_resolve_store_path(), payload)
    return record


def save_position(record, lock_timeout=LOCK_TIMEOUT_SECONDS):
    """Persist `record` as-is (caller is responsible for state-transition
    validation before calling this -- see positions/lifecycle.py). Requires
    record["position_id"] to already exist in the store."""
    position_id = record["position_id"]
    with _store_lock(lock_timeout):
        payload = _read_raw()
        if payload.get("_file_corrupted"):
            raise PositionStoreError(
                "Refusing to save: the position store file is corrupted and must be "
                "manually recovered first."
            )
        if position_id not in payload["positions"]:
            raise PositionStoreError(f"Cannot save unknown position_id {position_id!r}")
        payload["positions"][position_id] = record
        _atomic_write(_resolve_store_path(), payload)
    return record
