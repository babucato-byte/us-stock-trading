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
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from execution.secret_redaction import redact_text, redact_value

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_LOG_FILE = BASE_DIR / "SHADOW_MODE_LOG.jsonl"


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
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                with open(target, "a", encoding="utf-8") as fh:
                    fh.write(line)
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
    except OSError as exc:
        raise ShadowModeError(f"failed to persist Shadow Mode record: {exc}") from exc


def _read_file(target):
    if not target.exists():
        return []
    records = []
    with open(target, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
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
    if path is not None:
        return _read_file(path)
    override = os.environ.get("SHADOW_MODE_LOG_FILE")
    if override:
        return _read_file(Path(override))
    if date is not None:
        return _read_file(_resolve_log_path(for_date=date))
    records = []
    for rotated_file in sorted(BASE_DIR.glob("shadow-*.jsonl")):
        records.extend(_read_file(rotated_file))
    return records
