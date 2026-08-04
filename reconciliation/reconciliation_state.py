"""CODEX-044: durable record of "when did a real internal-vs-KIS
reconciliation last run, and was it clean" -- the actual fact
`kis_live_trading.py`/`brokers/kis_broker_adapter.py` must query for
`reconciliation_ok`, replacing the previous `reconciliation_ok=True`
constant Codex flagged as a bypass rather than a safety check.

Fail-closed on every axis: no recorded result, a corrupted state file,
a result older than `max_age_seconds`, or a recorded mismatch all
resolve to `is_current_and_clean() == False` -- there is no scenario
where a missing/stale/dirty reconciliation reads as "OK".
"""

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_STATE_FILE = BASE_DIR / "RECONCILIATION_STATE.json"
# How stale a recorded reconciliation is allowed to be before the buy/sell
# gates must treat it as "no current result" (fail-closed). Reconciliation
# runs every tick of kis_position_manager.sync_kis_fills_and_manage_exits(),
# so this only needs to tolerate one missed tick, not a long outage.
DEFAULT_MAX_AGE_SECONDS = 300


def _resolve_state_path():
    override = os.environ.get("RECONCILIATION_STATE_FILE")
    return Path(override) if override else DEFAULT_STATE_FILE


class ReconciliationStateError(Exception):
    """Raised only for a write failure. A read failure (missing/
    corrupted file) is NOT raised -- it fails closed via
    is_current_and_clean() returning False, exactly like a definitively
    dirty reconciliation would."""


@dataclass(frozen=True)
class ReconciliationRecord:
    clean: bool
    mismatch_count: int
    checked_at: datetime


def record_result(*, clean: bool, mismatch_count: int, now=None, path=None):
    """Writes the snapshot ATOMICALLY.

    It used to truncate the file and write in place, so a crash or a
    concurrent read could observe a half-written document. A reader
    seeing that gets invalid JSON -- which `reconciliation/freshness.py`
    correctly refuses -- but the previous, perfectly good snapshot is
    gone by then, so one unlucky moment turned into a stopped Shadow.
    With a temp file plus os.replace() a reader sees either the whole
    old document or the whole new one, never a partial one.
    """
    current = now or datetime.now(timezone.utc)
    target = path or _resolve_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"clean": bool(clean), "mismatch_count": int(mismatch_count),
               "checked_at": current.isoformat()}
    temp_path = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, target)
        # The rename itself must be durable, or a crash could resurrect
        # the previous snapshot while this process believes the new one
        # is live -- and "believes it is fresh" is the whole point here.
        dir_fd = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise ReconciliationStateError(f"failed to persist reconciliation result: {exc}") from exc


def _load(path=None) -> Optional[ReconciliationRecord]:
    target = path or _resolve_state_path()
    if not target.exists():
        return None
    try:
        with open(target, encoding="utf-8") as fh:
            data = json.load(fh)
        checked_at = datetime.fromisoformat(data["checked_at"])
        if checked_at.tzinfo is None or checked_at.tzinfo.utcoffset(checked_at) is None:
            # A naive timestamp used to reach is_current_and_clean() and
            # raise TypeError there, out of a fail-closed check and into
            # the caller. Refusing it here keeps the contract: anything
            # not definitively usable reads as "no current result".
            return None
        return ReconciliationRecord(
            clean=bool(data["clean"]), mismatch_count=int(data["mismatch_count"]),
            checked_at=checked_at,
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def is_current_and_clean(*, max_age_seconds, now=None, path=None) -> bool:
    """Fail-closed: returns False for anything other than "a reconciliation
    ran within max_age_seconds and found zero mismatches"."""
    record = _load(path=path)
    if record is None:
        return False
    if not record.clean or record.mismatch_count > 0:
        return False
    current = now or datetime.now(timezone.utc)
    age_seconds = (current - record.checked_at).total_seconds()
    if age_seconds < 0 or age_seconds > max_age_seconds:
        return False
    return True


def get_last_result(*, path=None) -> Optional[ReconciliationRecord]:
    return _load(path=path)
