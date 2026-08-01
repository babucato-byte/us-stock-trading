"""Shadow Mode structured record persistence (spec §5 of the sell-
lifecycle wiring directive): a durable, auditable JSONL log of every
buy-entry ATTEMPT this pipeline makes, whether it results in a
submitted order or a block -- distinct from (and not replaced by)
`KIS_LIVE_ORDER_ENABLED=false` simply preventing the broker call. Every
required field is recorded regardless of outcome, so an operator can
reconstruct exactly what the pipeline would have done for any given
trading day without re-running it.

One JSON object per line (JSONL, not a single JSON array) so a crash
mid-write never corrupts previously-recorded rows and the file can be
tailed/appended incrementally -- the same shape convention
`order_intent_ledger.py`/`exit_intent_ledger.py` already use for their
own append-only records in this codebase.

CODEX-review MEDIUM finding: locking + rotation. persist() takes an
flock (mirroring execution/idempotency.py's single_run_lock() pattern)
around the append so two concurrent writers (this pipeline's buy cycle
and kis_position_manager.py's sell/exit tick both call shadow_mode.
persist()) can never interleave partial writes into the same line.
Without an explicit SHADOW_MODE_LOG_FILE override (the escape hatch
every test in this suite uses to isolate its own file), the default
path rotates to one file PER CALENDAR DAY (`shadow-YYYY-MM-DD.jsonl`)
so the log never grows into a single unbounded file across the life of
a deployment.
"""

import fcntl
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from execution.secret_redaction import redact_text, redact_value

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_LOG_FILE = BASE_DIR / "SHADOW_MODE_LOG.jsonl"

# CODEX-048: size-based rotation on top of the per-day file, plus a
# retention window, so a single high-volume day cannot grow one file
# without bound and old days do not accumulate forever. Both are
# env-overridable with conservative defaults.
DEFAULT_MAX_FILE_MB = 50
DEFAULT_RETENTION_DAYS = 30
_ROTATED_SUFFIX_PATTERN = re.compile(r"\.(\d+)\.jsonl$")


def max_file_bytes():
    raw = os.environ.get("SHADOW_AUDIT_MAX_FILE_MB")
    try:
        value = float(raw) if raw is not None else DEFAULT_MAX_FILE_MB
    except (TypeError, ValueError):
        value = DEFAULT_MAX_FILE_MB
    if value <= 0:
        value = DEFAULT_MAX_FILE_MB
    return int(value * 1024 * 1024)


def retention_days():
    raw = os.environ.get("SHADOW_AUDIT_RETENTION_DAYS")
    try:
        value = int(raw) if raw is not None else DEFAULT_RETENTION_DAYS
    except (TypeError, ValueError):
        value = DEFAULT_RETENTION_DAYS
    return value if value > 0 else DEFAULT_RETENTION_DAYS


def _resolve_log_path(*, for_date=None):
    """Explicit SHADOW_MODE_LOG_FILE always wins (test isolation and any
    operator override use this) -- no rotation applied to it, matching
    the "an explicit path override means exactly that path" convention
    already used throughout this codebase's env-driven state files.
    Without an override, rotates to a per-calendar-day file."""
    override = os.environ.get("SHADOW_MODE_LOG_FILE")
    if override:
        return Path(override)
    day = for_date or datetime.now(timezone.utc).date()
    return BASE_DIR / f"shadow-{day.isoformat()}.jsonl"


def _lock_path_for(target):
    return target.with_name(target.name + ".lock")


class ShadowModeError(Exception):
    """Raised only for a write failure (disk full, permissions, etc) --
    never for a "normal" blocked/rejected attempt, which is exactly what
    this module exists to record, not treat as an error."""


@dataclass(frozen=True)
class ShadowModeRecord:
    signal_id: str
    strategy_id: str
    strategy_version: str
    code_commit: str
    symbol: str
    side: str
    alpaca_signal_price: Optional[float]
    kis_validation_price: Optional[float]
    price_difference_percent: Optional[float]
    planned_quantity: Optional[int]
    planned_limit_price: Optional[float]
    stop_price: Optional[float]
    target_price: Optional[float]
    risk_gate_result: str
    rejection_reason: Optional[str]
    account_available_usd: Optional[float]
    existing_position_quantity: Optional[int]
    existing_open_order: bool
    created_at: str


def build_record(
    *, signal_id, strategy_id, strategy_version, code_commit, symbol, side="buy",
    alpaca_signal_price=None, kis_validation_price=None, price_difference_percent=None,
    planned_quantity=None, planned_limit_price=None, stop_price=None, target_price=None,
    risk_gate_result, rejection_reason=None, account_available_usd=None,
    existing_position_quantity=None, existing_open_order=False, now=None,
):
    current = now or datetime.now(timezone.utc)
    return ShadowModeRecord(
        signal_id=signal_id, strategy_id=strategy_id, strategy_version=strategy_version,
        code_commit=code_commit, symbol=symbol, side=side,
        alpaca_signal_price=alpaca_signal_price, kis_validation_price=kis_validation_price,
        price_difference_percent=price_difference_percent, planned_quantity=planned_quantity,
        planned_limit_price=planned_limit_price, stop_price=stop_price, target_price=target_price,
        risk_gate_result=risk_gate_result, rejection_reason=rejection_reason,
        account_available_usd=account_available_usd,
        existing_position_quantity=existing_position_quantity,
        existing_open_order=existing_open_order, created_at=current.isoformat(),
    )


def persist(record: ShadowModeRecord, *, path=None):
    """Appends one JSON line. Never overwrites/truncates -- a fresh
    process restart simply appends to the same durable log. Takes an
    flock on a sibling `.lock` file for the duration of the append so
    two concurrent writers (the buy pipeline and the sell/exit tick)
    can never interleave partial lines.

    CODEX-050: every field goes through redact_value() (structural,
    key-name-based redaction -- a defense-in-depth layer in case a
    future field is ever a dict/nested structure carrying a secret key)
    and rejection_reason additionally through redact_text(), since it's
    free text built from an underlying exception message (e.g. an
    OrderGateBlockedError) that could otherwise carry an unmasked
    account number or similar into this durable, on-disk log."""
    if path is not None:
        target = path
    else:
        for_date = None
        if record.created_at:
            try:
                for_date = datetime.fromisoformat(record.created_at).date()
            except ValueError:
                for_date = None
        target = _resolve_log_path(for_date=for_date)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = redact_value(asdict(record))
    if payload.get("rejection_reason") is not None:
        payload["rejection_reason"] = redact_text(payload["rejection_reason"])
    line = json.dumps(payload) + "\n"
    lock_path = _lock_path_for(target)
    try:
        with open(lock_path, "a+") as lock_fh:
            # The rotation check happens INSIDE the same exclusive lock as
            # the append (CODEX-048): a rotation that raced an append could
            # otherwise move the file out from under a writer mid-line.
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                _rotate_if_oversized(target)
                with open(target, "a", encoding="utf-8") as fh:
                    fh.write(line)
                    # flush + fsync: without these a process/host crash can
                    # lose an already-"written" audit record that never left
                    # the OS page cache.
                    fh.flush()
                    os.fsync(fh.fileno())
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
    except OSError as exc:
        raise ShadowModeError(f"failed to persist Shadow Mode record: {exc}") from exc


def _rotate_if_oversized(target):
    """Renames `target` to `<name>.N.jsonl` once it exceeds the size
    limit. Caller MUST already hold the sibling lock file's flock."""
    try:
        size = target.stat().st_size
    except OSError:
        return
    if size < max_file_bytes():
        return
    index = 1
    while True:
        rotated = target.with_name(f"{target.stem}.{index}.jsonl")
        if not rotated.exists():
            break
        index += 1
    try:
        target.rename(rotated)
    except OSError as exc:  # pragma: no cover -- disk/permission failure
        raise ShadowModeError(f"failed to rotate Shadow Mode log: {exc}") from exc


def purge_old_files(*, days=None, now=None, base_dir=None):
    """Retention for the rotated per-day files. Returns the list of files
    deleted. Never touches an explicit SHADOW_MODE_LOG_FILE override (an
    operator-chosen path is the operator's to manage)."""
    limit_days = days if days is not None else retention_days()
    current = now or datetime.now(timezone.utc)
    cutoff = (current - timedelta(days=limit_days)).date()
    directory = Path(base_dir) if base_dir else BASE_DIR
    deleted = []
    for path in sorted(directory.glob("shadow-*.jsonl")):
        stem = path.name[len("shadow-"):].split(".")[0]
        try:
            file_date = datetime.strptime(stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date < cutoff:
            try:
                path.unlink()
                deleted.append(path)
            except OSError:  # pragma: no cover -- permission failure
                continue
    return deleted


def _read_file(target, corruption=None):
    """`corruption` (optional list) collects `(path, line_number)` for
    every unparseable line. CODEX-048: a torn line is still skipped (the
    durable prefix of good rows must never be discarded), but it is no
    longer INVISIBLE -- read_all() logs it and read_all_with_integrity()
    returns it, so an audit can tell "no such record" apart from "that
    record's line was corrupted"."""
    if not target.exists():
        return []
    records = []
    for line_number, line in enumerate(open(target, encoding="utf-8"), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            if corruption is not None:
                corruption.append((str(target), line_number))
            continue
    return records


def read_all(*, path=None, date=None):
    """Reads every recorded record back, for audit/reproduction. Skips
    (does not raise on) a malformed trailing line from a crash mid-write
    -- the durable prefix of good lines is never discarded.

    Without `path` and without SHADOW_MODE_LOG_FILE set, reads across
    EVERY rotated `shadow-*.jsonl` file (chronological by filename) so
    a full audit still sees every day's records; pass `date` to read
    just one day's file."""
    records, corruption = read_all_with_integrity(path=path, date=date)
    if corruption:
        logger.error(
            "Shadow Mode log corruption: %d unreadable line(s) skipped: %s",
            len(corruption), corruption,
        )
    return records


def verify_log_integrity(*, path=None, date=None, alert=True):
    """CODEX-048: corruption must be REPORTED, not merely skipped.

    Returns the list of `(file, line_number)` pairs that could not be
    parsed, and (by default) raises an operational alert for them. The
    reconciliation service calls this every pass, so a torn line becomes
    an operator-visible event rather than a silently shorter audit log.
    """
    _records, corruption = read_all_with_integrity(path=path, date=date)
    if corruption and alert:
        message = (
            "*Shadow Mode log corruption*\n"
            f"- unreadable lines: {len(corruption)}\n- locations: {corruption[:10]}"
        )
        logger.error(message)
        try:
            from operations import alerts

            alerts.send_alert(message)
        except Exception as exc:  # noqa: BLE001 -- alerting must not mask the finding
            logger.error("could not alert on Shadow Mode log corruption: %s", exc)
    return corruption


def read_all_strict(*, path=None, date=None):
    """read_all() that REFUSES to return a silently-shortened log. Use
    this wherever the count of records is itself the evidence (audit,
    reconciliation), rather than a best-effort listing."""
    records, corruption = read_all_with_integrity(path=path, date=date)
    if corruption:
        raise ShadowModeError(
            f"Shadow Mode log has {len(corruption)} unreadable line(s): {corruption[:10]}"
        )
    return records


def read_all_with_integrity(*, path=None, date=None):
    """Returns `(records, corruption)` where `corruption` is a list of
    `(file, line_number)` for every unparseable line encountered. This is
    the audit-grade reader: a caller that must prove the log is intact
    checks `corruption == []`."""
    corruption = []
    if path is not None:
        return _read_file(path, corruption), corruption
    override = os.environ.get("SHADOW_MODE_LOG_FILE")
    if override:
        return _read_file(Path(override), corruption), corruption
    if date is not None:
        return _read_file(_resolve_log_path(for_date=date), corruption), corruption
    records = []
    for rotated_file in sorted(BASE_DIR.glob("shadow-*.jsonl")):
        records.extend(_read_file(rotated_file, corruption))
    return records, corruption
