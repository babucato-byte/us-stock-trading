"""Self-monitoring for outbound Slack notifications.

Tracks whether Slack sends (slack_utils.send_slack_message /
send_slack_alert, or any callable with the same "message in, truthy success
out" shape) are actually landing, independent of the caller's own retry/error
handling. A Slack webhook can silently rot (revoked URL, network partition,
persistent 5xx) for a long time before anyone notices -- by definition, a
broken alert channel can't alert anyone that it's broken. This module keeps a
small persisted health record so an operator (or an automated guard) can ask
"is our alerting even working?" without depending on the very channel that
might be down, and escalates the kill switch if it's down for too long.

Status (persisted to a JSON file, path overridable via module attribute
STATE_FILE or the NOTIFICATION_HEALTH_STATE_FILE env var -- tests use
tmp_path):
    HEALTHY   -- most recent recorded send succeeded.
    DEGRADED  -- 1..(threshold - 1) consecutive failures.
    FAILED    -- consecutive failures have reached failure_threshold().
    UNKNOWN   -- no send has ever been recorded (no state file yet).

Once FAILED is reached, this module escalates kill_switch_state to
ENTRY_DISABLED (new entries blocked, exits/liquidation still allowed) so a
silently-broken alert channel cannot also mask silently-broken trading. It
never escalates past a more restrictive state a human/other subsystem has
already set (it only acts while the switch is still ACTIVE) and it never
calls release() itself -- recovery back to ACTIVE is always an explicit
operator decision, never automatic just because Slack started working again.

Every record_success()/record_failure() call also appends one line to a local
fallback log file (module attribute LOG_FILE / NOTIFICATION_HEALTH_LOG_FILE
env var) so the fact that a notification attempt happened -- and whether it
worked -- survives even a total Slack outage.
"""

import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import kill_switch_state as kss

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "NOTIFICATION_HEALTH_STATE.json"
LOG_FILE = BASE_DIR / "notification_health.log"
LOCK_TIMEOUT_SECONDS = 5.0

HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
FAILED = "FAILED"
UNKNOWN = "UNKNOWN"

VALID_STATUSES = {HEALTHY, DEGRADED, FAILED, UNKNOWN}

# Consecutive-failure count at which new entries get disabled. Overridable at
# runtime via the NOTIFICATION_HEALTH_FAILURE_THRESHOLD env var (see
# failure_threshold()) -- this constant is just the default.
DEFAULT_FAILURE_THRESHOLD = 5

_RECORD_FIELDS = [
    "consecutive_failures", "last_success_at", "last_failure_at",
    "last_status_code", "last_error_kind", "last_retry_result",
]


def _resolve_state_path():
    override = os.environ.get("NOTIFICATION_HEALTH_STATE_FILE")
    return Path(override) if override else STATE_FILE


def _resolve_log_path():
    override = os.environ.get("NOTIFICATION_HEALTH_LOG_FILE")
    return Path(override) if override else LOG_FILE


def _resolve_lock_path():
    # Derived from the state path (not a separate module constant) so tests
    # that point STATE_FILE at tmp_path automatically get an isolated lock
    # file too, with no separate monkeypatch of their own required.
    return _resolve_state_path().with_suffix(".lock")


@contextmanager
def _state_lock(timeout=LOCK_TIMEOUT_SECONDS):
    """Process-level exclusive lock (fcntl.flock) guarding read-modify-write
    of the health state file -- same technique as kill_switch_state.py /
    paper_strategy_order.py's order_history.csv / order_reconciliation.csv
    locks.

    Raises RuntimeError, without touching the state file, if the lock can't
    be acquired within `timeout`. record_success()/record_failure() catch
    this themselves (never raises is part of their contract -- a broken
    alert channel must not become a second outage) and fail closed instead
    of ever writing unsynchronized.
    """
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
                    raise RuntimeError(
                        f"Could not acquire notification health lock ({lock_path}) within {timeout}s"
                    )
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        finally:
            lock_file.close()


def failure_threshold():
    """Consecutive-failure count that triggers the kill-switch escalation.

    Read fresh on every call (never cached at import time) so it can be
    overridden per-test/per-deployment via the env var without a restart.
    """
    raw = os.environ.get("NOTIFICATION_HEALTH_FAILURE_THRESHOLD")
    if raw is None:
        return DEFAULT_FAILURE_THRESHOLD
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_FAILURE_THRESHOLD
    return value if value > 0 else DEFAULT_FAILURE_THRESHOLD


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _empty_record():
    record = {field: None for field in _RECORD_FIELDS}
    record["consecutive_failures"] = 0
    return record


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


def _load():
    """Return (record, existed). A missing or unparseable state file both
    come back as a fresh all-zero record with existed=False -- this module
    must never itself crash the caller just because its own state is gone."""
    path = _resolve_state_path()
    if not path.exists():
        return _empty_record(), False
    try:
        payload = json.loads(path.read_text())
        record = _empty_record()
        record.update({k: payload.get(k) for k in _RECORD_FIELDS if k in payload})
        if not isinstance(record.get("consecutive_failures"), int):
            record["consecutive_failures"] = 0
        return record, True
    except Exception:
        return _empty_record(), False


def _save(record):
    _atomic_write(_resolve_state_path(), record)


def _fallback_log(event, record, detail=None):
    """Append-only local log line, written unconditionally so a notification
    attempt (and its outcome) is on disk even if Slack itself is unreachable."""
    path = _resolve_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{_now_iso()} event={event} consecutive_failures={record.get('consecutive_failures', 0)}"
    if detail is not None:
        line += f" detail={detail}"
    with open(path, "a") as log_file:
        log_file.write(line + "\n")


def record_success(status_code=None, retry_result=None, lock_timeout=LOCK_TIMEOUT_SECONDS):
    """Record a successful Slack send. Resets the consecutive-failure streak.

    Locks -> rereads -> merges -> writes so a concurrent record_success()/
    record_failure() can't both read the same stale record and clobber each
    other's write (lost update). Never raises: on a lock timeout (or any
    other OS-level failure to acquire it) this leaves the state file
    completely untouched and returns the last-persisted record instead.
    """
    try:
        with _state_lock(timeout=lock_timeout):
            record, _ = _load()
            record["consecutive_failures"] = 0
            record["last_success_at"] = _now_iso()
            record["last_status_code"] = status_code
            record["last_error_kind"] = None
            record["last_retry_result"] = retry_result
            _save(record)
    except (RuntimeError, OSError) as exc:
        record, _ = _load()
        _fallback_log("success_lock_timeout", record, detail=str(exc))
        return record

    _fallback_log("success", record, detail=status_code)
    return record


def record_failure(error_kind=None, status_code=None, retry_result=None, lock_timeout=LOCK_TIMEOUT_SECONDS):
    """Record a failed Slack send attempt.

    Never raises -- a broken alert channel must not become a second outage
    on top of the first. Locks -> rereads -> merges -> writes (see
    record_success()); on a lock timeout the consecutive-failure streak is
    left untouched on disk (never incremented against stale data) and no
    kill-switch escalation is attempted for an update that didn't actually
    persist. Once a persisted streak reaches failure_threshold(), escalates
    the kill switch to ENTRY_DISABLED (see _escalate_kill_switch).
    """
    try:
        with _state_lock(timeout=lock_timeout):
            record, _ = _load()
            record["consecutive_failures"] = record.get("consecutive_failures", 0) + 1
            record["last_failure_at"] = _now_iso()
            record["last_status_code"] = status_code
            record["last_error_kind"] = error_kind
            record["last_retry_result"] = retry_result
            _save(record)
    except (RuntimeError, OSError) as exc:
        record, _ = _load()
        _fallback_log("failure_lock_timeout", record, detail=str(exc))
        return record

    _fallback_log("failure", record, detail=error_kind or status_code)

    if record["consecutive_failures"] >= failure_threshold():
        _escalate_kill_switch(record["consecutive_failures"])

    return record


def _escalate_kill_switch(consecutive_failures):
    """Disable new entries once Slack has been silently broken for too long.
    Exits/liquidation stay allowed (ENTRY_DISABLED, not a harsher state).

    Only acts while the switch is still ACTIVE, so this never overrides a
    more restrictive state a human or another subsystem already set.
    """
    try:
        if kss.get_state() != kss.ACTIVE:
            return
        kss.activate(
            kss.ENTRY_DISABLED,
            reason=f"notification_health: {consecutive_failures} consecutive Slack notification failures",
            activated_by="notification_health",
        )
    except Exception as exc:
        _fallback_log("kill_switch_escalation_failed", _load()[0], detail=str(exc))


def get_status():
    """Return one of HEALTHY / DEGRADED / FAILED / UNKNOWN."""
    record, existed = _load()
    if not existed:
        return UNKNOWN
    failures = record.get("consecutive_failures", 0)
    if failures <= 0:
        return HEALTHY
    if failures >= failure_threshold():
        return FAILED
    return DEGRADED


def get_record():
    """Return a copy of the persisted health record fields."""
    record, _ = _load()
    return dict(record)


def summarize():
    """Human-readable summary for an operator to read at a glance."""
    record, existed = _load()
    status = get_status()
    if not existed:
        return "Notification health: UNKNOWN (no Slack send has been recorded yet)"
    return "\n".join([
        f"Notification health: {status}",
        f"  consecutive_failures: {record.get('consecutive_failures', 0)} (threshold={failure_threshold()})",
        f"  last_success_at: {record.get('last_success_at')}",
        f"  last_failure_at: {record.get('last_failure_at')}",
        f"  last_status_code: {record.get('last_status_code')}",
        f"  last_error_kind: {record.get('last_error_kind')}",
        f"  last_retry_result: {record.get('last_retry_result')}",
    ])


def send_with_health_tracking(send_fn, message, retry_result=None):
    """Call send_fn(message) and record the outcome for health tracking.

    send_fn is expected to behave like slack_utils.send_slack_message /
    send_slack_alert: return a truthy value on success, a falsy value on a
    handled failure (e.g. non-200 response), or raise on a transport-level
    failure (timeout, connection error, invalid URL, ...).

    Never raises -- any exception from send_fn is caught, recorded as a
    failure, and swallowed. Returns True only when send_fn itself returned
    truthy; this return value is purely about notification delivery and must
    never be treated as, or allowed to influence, an order's own result.
    """
    try:
        result = send_fn(message)
    except Exception as exc:
        record_failure(error_kind=type(exc).__name__, retry_result=retry_result)
        return False

    if result:
        record_success(retry_result=retry_result)
        return True

    record_failure(error_kind="SEND_RETURNED_FALSY", retry_result=retry_result)
    return False
