"""Multi-level kill switch state machine layered on top of kill_switch.py's
binary TRADING_HALTED / KILL_SWITCH file check.

States (most to least permissive):
  ACTIVE                -- normal operation, everything allowed.
  ENTRY_DISABLED        -- no new entries; exit/liquidation orders still allowed.
  ALL_TRADING_DISABLED  -- no entries, no exits; queries only.
  MANUAL_REVIEW         -- incident under human review; same order restrictions
                           as ALL_TRADING_DISABLED, but signals a human must
                           look at it before anything resumes.

Fail-closed by design (opposite of kill_switch.is_trading_halted()'s
fail-open default): a state file that is missing is ACTIVE (unchanged
behavior for callers who have never heard of this), but a state file that
exists and cannot be parsed is treated as the most conservative state,
never silently as ACTIVE.

Persistence: a single JSON file holding {"current": {...}, "history": [...]}
at the path returned by _resolve_state_path() (env-overridable, defaults
next to this module). Every activate()/release() call is atomic (temp file
+ os.replace) and appends a snapshot to history rather than mutating past
entries, so the audit trail on disk is a durable record of every transition
attempted -- including repeated activations of the same state.

Release back to ACTIVE only ever happens via an explicit release() call
naming the approving operator. expires_at on a record is stored for
informational/reporting purposes only; it is never consulted to
auto-reactivate -- an expired kill switch stays engaged until a human
releases it.
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "KILL_SWITCH_STATE.json"

ACTIVE = "ACTIVE"
ENTRY_DISABLED = "ENTRY_DISABLED"
ALL_TRADING_DISABLED = "ALL_TRADING_DISABLED"
MANUAL_REVIEW = "MANUAL_REVIEW"

VALID_STATES = {ACTIVE, ENTRY_DISABLED, ALL_TRADING_DISABLED, MANUAL_REVIEW}

# Fail-closed target when the state file exists but can't be trusted.
FAIL_CLOSED_STATE = MANUAL_REVIEW

_RECORD_FIELDS = [
    "state", "reason", "activated_at", "activated_by", "expires_at",
    "incident_id", "acknowledged_at", "released_at", "released_by",
]


class KillSwitchStateError(Exception):
    """Raised for invalid operations against the kill switch state machine."""


def _resolve_state_path():
    override = os.environ.get("KILL_SWITCH_STATE_FILE")
    return Path(override) if override else STATE_FILE


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _empty_record():
    return {field: None for field in _RECORD_FIELDS}


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


def _fail_closed_snapshot(reason):
    record = _empty_record()
    record["state"] = FAIL_CLOSED_STATE
    record["reason"] = reason
    record["activated_at"] = _now_iso()
    return {"current": record, "history": [record]}


def _load(path):
    """Return the persisted {"current": {...}, "history": [...]} payload.

    Absence of the file means ACTIVE (matches kill_switch.is_trading_halted()'s
    default-allow when unset). A file that exists but cannot be parsed, or is
    missing required structure, fails closed to FAIL_CLOSED_STATE instead of
    silently defaulting to ACTIVE.
    """
    if not path.exists():
        record = _empty_record()
        record["state"] = ACTIVE
        return {"current": record, "history": []}

    try:
        payload = json.loads(path.read_text())
        current = payload["current"]
        history = payload["history"]
        if current.get("state") not in VALID_STATES:
            raise ValueError(f"invalid state {current.get('state')!r}")
        if not isinstance(history, list):
            raise ValueError("history must be a list")
        return {"current": current, "history": history}
    except Exception as exc:
        return _fail_closed_snapshot(f"CORRUPTED_STATE_FILE: failed to parse {path}: {exc}")


def get_state():
    """Return the current state string. Always safe to call in any state."""
    return _load(_resolve_state_path())["current"]["state"]


def get_current_record():
    """Return a copy of the full current-state record."""
    return dict(_load(_resolve_state_path())["current"])


def get_history():
    """Return a copy of the full audit history (record snapshots, oldest first)."""
    return list(_load(_resolve_state_path())["history"])


def is_entry_allowed():
    """New entry orders are permitted only in ACTIVE."""
    return get_state() == ACTIVE


def is_liquidation_allowed():
    """Exit/auto-liquidation orders are permitted in ACTIVE and ENTRY_DISABLED."""
    return get_state() in (ACTIVE, ENTRY_DISABLED)


def activate(state, reason, activated_by, expires_at=None, incident_id=None):
    """Transition into `state`, recording an audit entry.

    Idempotent: re-activating the same state that is already current does
    not reset activated_at or otherwise corrupt the record -- it refreshes
    reason/expires_at/incident_id and still appends a fresh snapshot to the
    audit history so the repeat attempt itself is recorded.
    """
    if state not in VALID_STATES:
        raise KillSwitchStateError(f"Unknown kill switch state: {state!r}")
    if not reason or not activated_by:
        raise KillSwitchStateError("activate() requires both reason and activated_by")

    path = _resolve_state_path()
    payload = _load(path)
    current = payload["current"]
    history = payload["history"]

    if current.get("state") == state:
        record = dict(current)
        record["reason"] = reason
        record["activated_by"] = activated_by
        record["expires_at"] = expires_at
        record["incident_id"] = incident_id or current.get("incident_id")
    else:
        record = _empty_record()
        record["state"] = state
        record["reason"] = reason
        record["activated_at"] = _now_iso()
        record["activated_by"] = activated_by
        record["expires_at"] = expires_at
        record["incident_id"] = incident_id

    history.append(dict(record))
    _atomic_write(path, {"current": record, "history": history})
    return record


def release(released_by, reason=None):
    """Explicitly return to ACTIVE. Requires the approving operator's identity.

    Never automatic: expires_at on the current record is not consulted here,
    so an expired kill switch stays engaged until this is called directly.
    """
    if not released_by:
        raise KillSwitchStateError("release() requires released_by (explicit operator approval)")

    path = _resolve_state_path()
    payload = _load(path)
    current = payload["current"]
    history = payload["history"]

    if current.get("state") == ACTIVE:
        return dict(current)

    record = _empty_record()
    record["state"] = ACTIVE
    record["reason"] = reason
    record["activated_at"] = _now_iso()
    record["activated_by"] = released_by
    record["released_at"] = _now_iso()
    record["released_by"] = released_by

    history.append(dict(record))
    _atomic_write(path, {"current": record, "history": history})
    return record
