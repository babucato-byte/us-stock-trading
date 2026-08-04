"""One place that decides whether a reconciliation snapshot may be
relied on, used by everything that needs the answer.

Codex reproduced this: the Shadow timer approval script checked that
`checked_at` was PRESENT and never looked at what it said. A snapshot 30
days old, clean and mismatch-free, armed the timer -- so a recurring
Shadow evaluation would have run indefinitely on an account
reconciliation from a month earlier.

"A reconciliation ran" and "a reconciliation ran recently enough to mean
anything" are different claims, and only the second one is safe to build
on. Every rejection below is fail-closed and carries a reason code, so
the operator is told which of them applied rather than just "no".

Two callers share this module and therefore share its policy exactly:

    scripts/enable_oracle_shadow_timer.sh   before arming the timer
    us-stock-trading-shadow.service         ExecStartPre, before EVERY run

The second matters as much as the first: arming the timer once against a
fresh snapshot says nothing about the state an hour later, and without a
per-run check a stalled reconciliation would go unnoticed while Shadow
kept evaluating.
"""

import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Reconciliation runs every 2 minutes on the Oracle host
# (us-stock-trading-reconcile.timer, OnUnitActiveSec=2min) and Shadow
# every 5, so 15 minutes tolerates several consecutive missed runs
# without ever tolerating a stopped reconciler.
DEFAULT_MAX_AGE_SECONDS = 900
MAX_ALLOWED_MAX_AGE_SECONDS = 3600

# Clock skew between the writer and the reader. Small on purpose: this
# exists to absorb NTP jitter, not to make "the future" acceptable.
DEFAULT_MAX_FUTURE_SKEW_SECONDS = 30
MAX_ALLOWED_FUTURE_SKEW_SECONDS = 300

ENV_MAX_AGE = "SHADOW_RECONCILIATION_MAX_AGE_SECONDS"
ENV_MAX_FUTURE_SKEW = "SHADOW_RECONCILIATION_MAX_FUTURE_SKEW_SECONDS"

REASON_SNAPSHOT_MISSING = "RECONCILIATION_SNAPSHOT_MISSING"
REASON_SNAPSHOT_INVALID = "RECONCILIATION_SNAPSHOT_INVALID"
REASON_TIMESTAMP_INVALID = "RECONCILIATION_TIMESTAMP_INVALID"
REASON_TIMEZONE_MISSING = "RECONCILIATION_TIMESTAMP_TIMEZONE_MISSING"
REASON_SNAPSHOT_STALE = "RECONCILIATION_SNAPSHOT_STALE"
REASON_SNAPSHOT_FROM_FUTURE = "RECONCILIATION_SNAPSHOT_FROM_FUTURE"
REASON_NOT_CLEAN = "RECONCILIATION_NOT_CLEAN"
REASON_UNKNOWN_PRESENT = "RECONCILIATION_UNKNOWN_PRESENT"
REASON_HALT_ACTIVE = "RECONCILIATION_HALT_ACTIVE"
REASON_CONFIG_INVALID = "RECONCILIATION_FRESHNESS_CONFIG_INVALID"
REASON_SCHEMA_INVALID = "RECONCILIATION_SNAPSHOT_SCHEMA_INVALID"
REASON_REQUIRED_FIELD_MISSING = "RECONCILIATION_REQUIRED_FIELD_MISSING"
REASON_FIELD_TYPE_INVALID = "RECONCILIATION_FIELD_TYPE_INVALID"
REASON_FIELD_VALUE_INVALID = "RECONCILIATION_FIELD_VALUE_INVALID"
REASON_SCHEMA_VERSION_UNSUPPORTED = "RECONCILIATION_SCHEMA_VERSION_UNSUPPORTED"

# The only snapshot shape this code accepts. Bumping it is a deliberate
# change on both sides: the writer stamps it and the reader refuses
# anything else, so a snapshot written by an older release is rejected
# rather than half-understood.
SCHEMA_VERSION = 1

# field -> exact type. EXACT: `isinstance(True, int)` is True in Python,
# so an isinstance check would accept `{"mismatch_count": true}` as a
# count. Every check below is `type(value) is expected`.
REQUIRED_FIELDS = (
    ("schema_version", int),
    ("checked_at", str),
    ("clean", bool),
    ("mismatch_count", int),
    ("unknown_count", int),
    ("halt", bool),
)
NON_NEGATIVE_FIELDS = ("mismatch_count", "unknown_count")


class SnapshotUnusable(Exception):
    """The snapshot may not be relied on. Carries the reason code and a
    short, redacted detail -- never a path, an account or a raw body."""

    def __init__(self, message, *, reason_code, detail=None):
        super().__init__(message)
        self.reason_code = reason_code
        self.detail = detail


@dataclass(frozen=True)
class Freshness:
    checked_at: datetime
    age_seconds: float
    max_age_seconds: int
    max_future_skew_seconds: int
    clean: bool
    mismatch_count: int
    unknown_count: int
    halt: bool

    def as_log_fields(self):
        """Exactly what an operator needs, and nothing that identifies an
        account or carries a secret."""
        return {
            "snapshot_age_seconds": round(self.age_seconds, 1),
            "max_age_seconds": self.max_age_seconds,
            "future_skew_seconds": self.max_future_skew_seconds,
            "clean": self.clean,
            "mismatch_count": self.mismatch_count,
            "unknown_count": self.unknown_count,
            "halt": self.halt,
        }


def _json_type_name(value):
    """What the field actually was, in JSON terms, for the log line."""
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "integer"
    if type(value) is float:
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


_EXPECTED_NAME = {bool: "boolean", int: "integer", str: "string"}


def validate_schema(data):
    """Strict. No coercion, no defaults, no truthiness.

    Codex got a snapshot with `"clean": "false"` and `"mismatch_count":
    "0"` approved, because the reader did `bool(...)` and `int(...)` on
    whatever was there -- and `bool("false")` is True. A missing
    `mismatch_count` defaulted to 0, which reads as "no mismatches" when
    it actually means "nobody said".

    Returns the validated dict; raises SnapshotUnusable otherwise.
    """
    if not isinstance(data, dict):
        raise SnapshotUnusable(
            "the reconciliation snapshot is not a JSON object",
            reason_code=REASON_SCHEMA_INVALID, detail=f"actual={_json_type_name(data)}")

    # Version first: a different schema means the field checks below are
    # not the right ones to apply.
    if "schema_version" not in data:
        raise SnapshotUnusable(
            "the reconciliation snapshot has no schema_version",
            reason_code=REASON_REQUIRED_FIELD_MISSING, detail="field=schema_version")
    version = data["schema_version"]
    if type(version) is not int:
        raise SnapshotUnusable(
            "schema_version is not an integer",
            reason_code=REASON_FIELD_TYPE_INVALID,
            detail=f"field=schema_version expected=integer actual={_json_type_name(version)}")
    if version != SCHEMA_VERSION:
        raise SnapshotUnusable(
            f"unsupported reconciliation snapshot schema_version {version}",
            reason_code=REASON_SCHEMA_VERSION_UNSUPPORTED,
            detail=f"field=schema_version expected={SCHEMA_VERSION} actual={version}")

    for field, expected in REQUIRED_FIELDS:
        if field not in data:
            raise SnapshotUnusable(
                f"the reconciliation snapshot has no {field}",
                reason_code=REASON_REQUIRED_FIELD_MISSING, detail=f"field={field}")
        value = data[field]
        if type(value) is not expected:
            raise SnapshotUnusable(
                f"{field} is not a JSON {_EXPECTED_NAME[expected]}",
                reason_code=REASON_FIELD_TYPE_INVALID,
                detail=(f"field={field} expected={_EXPECTED_NAME[expected]} "
                        f"actual={_json_type_name(value)}"))

    for field in NON_NEGATIVE_FIELDS:
        if data[field] < 0:
            raise SnapshotUnusable(
                f"{field} is negative",
                reason_code=REASON_FIELD_VALUE_INVALID,
                detail=f"field={field} value_invalid=negative")
    return data


def _bounded_int_env(name, default, *, maximum):
    """A misconfigured bound is a configuration error, not a licence to
    fall back to something permissive. Absent is the only tolerated
    'unset', and the value actually used is reported by the caller."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    text = raw.strip()
    try:
        value = int(text)
    except ValueError:
        raise SnapshotUnusable(
            f"{name} must be a whole number of seconds",
            reason_code=REASON_CONFIG_INVALID, detail=f"{name}=not_an_integer")
    if value <= 0:
        raise SnapshotUnusable(
            f"{name} must be greater than zero",
            reason_code=REASON_CONFIG_INVALID, detail=f"{name}<=0")
    if value > maximum:
        raise SnapshotUnusable(
            f"{name} must not exceed {maximum} seconds",
            reason_code=REASON_CONFIG_INVALID, detail=f"{name}>{maximum}")
    return value


def max_age_seconds():
    return _bounded_int_env(ENV_MAX_AGE, DEFAULT_MAX_AGE_SECONDS,
                            maximum=MAX_ALLOWED_MAX_AGE_SECONDS)


def max_future_skew_seconds():
    return _bounded_int_env(ENV_MAX_FUTURE_SKEW, DEFAULT_MAX_FUTURE_SKEW_SECONDS,
                            maximum=MAX_ALLOWED_FUTURE_SKEW_SECONDS)


def snapshot_path():
    raw = os.environ.get("RECONCILIATION_STATE_FILE", "").strip()
    if raw:
        return Path(raw)
    from reconciliation import reconciliation_state

    return reconciliation_state.DEFAULT_STATE_FILE


def _read_snapshot(path):
    """Reads the file only after establishing it is a plain regular file
    this process may trust. A symlink here could point anywhere, and
    following one would make the answer depend on a target nobody
    reviewed."""
    try:
        info = os.lstat(str(path))
    except FileNotFoundError:
        raise SnapshotUnusable(
            "no reconciliation snapshot on disk",
            reason_code=REASON_SNAPSHOT_MISSING, detail="absent")
    except OSError as exc:
        raise SnapshotUnusable(
            "the reconciliation snapshot could not be inspected",
            reason_code=REASON_SNAPSHOT_INVALID, detail=type(exc).__name__)

    if stat.S_ISLNK(info.st_mode):
        raise SnapshotUnusable(
            "the reconciliation snapshot is a symlink",
            reason_code=REASON_SNAPSHOT_INVALID, detail="symlink")
    if not stat.S_ISREG(info.st_mode):
        raise SnapshotUnusable(
            "the reconciliation snapshot is not a regular file",
            reason_code=REASON_SNAPSHOT_INVALID, detail="non_regular_file")
    if info.st_uid != os.getuid() and os.getuid() != 0:
        raise SnapshotUnusable(
            "the reconciliation snapshot is owned by another user",
            reason_code=REASON_SNAPSHOT_INVALID, detail="unexpected_owner")
    if info.st_mode & stat.S_IWOTH:
        raise SnapshotUnusable(
            "the reconciliation snapshot is world-writable",
            reason_code=REASON_SNAPSHOT_INVALID, detail="world_writable")

    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise SnapshotUnusable(
            "the reconciliation snapshot could not be read",
            reason_code=REASON_SNAPSHOT_INVALID, detail=type(exc).__name__)

    try:
        data = json.loads(raw)
    except ValueError:
        # A partial write reads as invalid JSON, which is exactly the
        # answer we want: unusable, not "assume the old value".
        raise SnapshotUnusable(
            "the reconciliation snapshot is not valid JSON",
            reason_code=REASON_SNAPSHOT_INVALID, detail="malformed_json")
    if not isinstance(data, dict):
        raise SnapshotUnusable(
            "the reconciliation snapshot is not a JSON object",
            reason_code=REASON_SNAPSHOT_INVALID, detail=type(data).__name__)
    return data


def _parse_checked_at(value):
    if value is None:
        raise SnapshotUnusable(
            "the reconciliation snapshot has no checked_at",
            reason_code=REASON_TIMESTAMP_INVALID, detail="missing")
    if not isinstance(value, str) or not value.strip():
        raise SnapshotUnusable(
            "checked_at is not an ISO-8601 string",
            reason_code=REASON_TIMESTAMP_INVALID, detail=type(value).__name__)
    text = value.strip()
    # `datetime.fromisoformat` gained "Z" support only in 3.11.
    normalised = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError:
        raise SnapshotUnusable(
            "checked_at is not a parseable ISO-8601 timestamp",
            reason_code=REASON_TIMESTAMP_INVALID, detail="unparseable")
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        # Applying the server's local zone would silently shift the age
        # by the offset, which is how a stale snapshot could read fresh.
        raise SnapshotUnusable(
            "checked_at has no timezone; it must be explicit (Z or an offset)",
            reason_code=REASON_TIMEZONE_MISSING, detail="naive")
    return parsed.astimezone(timezone.utc)


def evaluate(*, path=None, now=None, require_unknown_zero=False,
             require_halt_clear=False):
    """Returns a Freshness for a snapshot that may be relied on, or
    raises SnapshotUnusable.

    `now` is WALL time in UTC: the snapshot records an ISO timestamp, so
    the only meaningful comparison is against the wall clock. A monotonic
    reading would be a different quantity entirely.
    """
    target = Path(path) if path else snapshot_path()
    limit = max_age_seconds()
    skew = max_future_skew_seconds()
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    data = validate_schema(_read_snapshot(target))
    checked_at = _parse_checked_at(data["checked_at"])
    age = (current - checked_at).total_seconds()

    if age < -skew:
        raise SnapshotUnusable(
            f"the reconciliation snapshot is {abs(age):.0f}s in the future",
            reason_code=REASON_SNAPSHOT_FROM_FUTURE,
            detail=f"future_by={abs(age):.0f}s skew={skew}s")
    if age > limit:
        raise SnapshotUnusable(
            f"the reconciliation snapshot is {age:.0f}s old (limit {limit}s)",
            reason_code=REASON_SNAPSHOT_STALE,
            detail=f"age={age:.0f}s max_age={limit}s")

    # Types are already guaranteed exact, so these are state decisions,
    # reported separately from a schema fault.
    clean = data["clean"]
    mismatch_count = data["mismatch_count"]
    if clean is not True or mismatch_count > 0:
        raise SnapshotUnusable(
            f"the last reconciliation was not clean ({mismatch_count} mismatch(es))",
            reason_code=REASON_NOT_CLEAN,
            detail=f"clean={clean} mismatch_count={mismatch_count}")
    if data["unknown_count"] > 0:
        raise SnapshotUnusable(
            f"{data['unknown_count']} order(s) were UNKNOWN at reconciliation",
            reason_code=REASON_UNKNOWN_PRESENT,
            detail=f"unknown_count={data['unknown_count']}")
    if data["halt"] is not False:
        raise SnapshotUnusable(
            "HALT was set at reconciliation",
            reason_code=REASON_HALT_ACTIVE, detail="snapshot_halt=true")

    # The snapshot records the state at reconciliation time; these two
    # re-read it NOW, which is stricter, not a substitute.
    if require_unknown_zero:
        _require_no_unknown_orders()
    if require_halt_clear:
        _require_halt_clear()

    # Negative-but-tolerated skew counts as age zero, never as "fresher
    # than possible".
    return Freshness(checked_at=checked_at, age_seconds=max(age, 0.0),
                     max_age_seconds=limit, max_future_skew_seconds=skew,
                     clean=clean, mismatch_count=mismatch_count,
                     unknown_count=data["unknown_count"], halt=data["halt"])


def _require_no_unknown_orders():
    try:
        from state_store import db as state_db

        conn = state_db.open_db()
        try:
            outstanding = conn.execute(
                "select count(*) from orders where status = 'UNKNOWN'").fetchone()[0]
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 -- unreadable is not "none"
        raise SnapshotUnusable(
            "the order state could not be read",
            reason_code=REASON_UNKNOWN_PRESENT, detail=type(exc).__name__)
    if outstanding:
        raise SnapshotUnusable(
            f"{outstanding} order(s) are in UNKNOWN state",
            reason_code=REASON_UNKNOWN_PRESENT, detail=f"unknown={outstanding}")


def _require_halt_clear():
    try:
        from operations import kill_switch

        halted = kill_switch.is_halted()
    except Exception as exc:  # noqa: BLE001 -- kill_switch fails closed to halted
        raise SnapshotUnusable(
            "the HALT state could not be read",
            reason_code=REASON_HALT_ACTIVE, detail=type(exc).__name__)
    if halted:
        raise SnapshotUnusable(
            "HALT is set", reason_code=REASON_HALT_ACTIVE, detail="halted")
