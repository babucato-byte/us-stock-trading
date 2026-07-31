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
"""

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from execution.secret_redaction import redact_text, redact_value

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_LOG_FILE = BASE_DIR / "SHADOW_MODE_LOG.jsonl"


def _resolve_log_path():
    override = os.environ.get("SHADOW_MODE_LOG_FILE")
    return Path(override) if override else DEFAULT_LOG_FILE


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
    process restart simply appends to the same durable log.

    CODEX-050: every field goes through redact_value() (structural,
    key-name-based redaction -- a defense-in-depth layer in case a
    future field is ever a dict/nested structure carrying a secret key)
    and rejection_reason additionally through redact_text(), since it's
    free text built from an underlying exception message (e.g. an
    OrderGateBlockedError) that could otherwise carry an unmasked
    account number or similar into this durable, on-disk log."""
    target = path or _resolve_log_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = redact_value(asdict(record))
    if payload.get("rejection_reason") is not None:
        payload["rejection_reason"] = redact_text(payload["rejection_reason"])
    try:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")
    except OSError as exc:
        raise ShadowModeError(f"failed to persist Shadow Mode record: {exc}") from exc


def read_all(*, path=None):
    """Reads every recorded record back, for audit/reproduction. Skips
    (does not raise on) a malformed trailing line from a crash mid-write
    -- the durable prefix of good lines is never discarded."""
    target = path or _resolve_log_path()
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
