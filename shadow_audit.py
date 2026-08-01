"""CODEX-048: the durable Shadow Mode AUDIT EVENT store.

`shadow_mode.py` records one structured JSONL row per buy-entry attempt
(spec §5's per-attempt record). That is a report, not an audit trail: it
has one row per attempt, is written only where a caller remembered to
call it, and a torn line is silently unreadable. This module is the
audit trail proper -- one row per EVALUATION STEP, tied together by a
`shadow_run_id`, stored in SQLite (`shadow_audit_events`, migration 9)
rather than a flat file, which gives us for free the four properties
Codex found missing:

  - cross-process concurrent writes (SQLite's own write lock + this
    codebase's existing busy_timeout), not "hope the flock held";
  - durability -- a committed transaction is fsynced by SQLite itself,
    so a crash cannot lose an already-recorded event;
  - retention (`purge_old_events()` + SHADOW_AUDIT_RETENTION_DAYS)
    instead of unbounded file growth with only date-based rotation;
  - no silent corruption -- there is no "malformed line" failure mode to
    skip past in the first place.

Every path that can block, approve, or fail MUST emit a terminal event
(`SHADOW_COMPLETED` or `SHADOW_ERROR`) for its run, so an operator
auditing a trading day never finds a run that just stops mid-way.

Sensitive values are redacted at THIS boundary (execution/
secret_redaction.py), independently of whatever the caller did, so a
payload carrying an account number or token can never be persisted here
even if a future call site forgets.
"""

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from execution.secret_redaction import redact_text, redact_value

DEFAULT_RETENTION_DAYS = 30

# -- event vocabulary ---------------------------------------------------
SIGNAL_RECEIVED = "SIGNAL_RECEIVED"
CONFIG_BLOCKED = "CONFIG_BLOCKED"
SIGNAL_EXPIRED = "SIGNAL_EXPIRED"
INSTRUMENT_BLOCKED = "INSTRUMENT_BLOCKED"
PRICE_DEVIATION_BLOCKED = "PRICE_DEVIATION_BLOCKED"
CASH_BLOCKED = "CASH_BLOCKED"
RECONCILIATION_BLOCKED = "RECONCILIATION_BLOCKED"
UNKNOWN_ORDER_BLOCKED = "UNKNOWN_ORDER_BLOCKED"
DUPLICATE_BLOCKED = "DUPLICATE_BLOCKED"
HALT_BLOCKED = "HALT_BLOCKED"
GATE_REJECTED = "GATE_REJECTED"
GATE_APPROVED = "GATE_APPROVED"
EXECUTION_PLANNED = "EXECUTION_PLANNED"
SHADOW_COMPLETED = "SHADOW_COMPLETED"
SHADOW_ERROR = "SHADOW_ERROR"

EVENT_TYPES = frozenset({
    SIGNAL_RECEIVED, CONFIG_BLOCKED, SIGNAL_EXPIRED, INSTRUMENT_BLOCKED,
    PRICE_DEVIATION_BLOCKED, CASH_BLOCKED, RECONCILIATION_BLOCKED, UNKNOWN_ORDER_BLOCKED,
    DUPLICATE_BLOCKED, HALT_BLOCKED, GATE_REJECTED, GATE_APPROVED, EXECUTION_PLANNED,
    SHADOW_COMPLETED, SHADOW_ERROR,
})

TERMINAL_EVENT_TYPES = frozenset({SHADOW_COMPLETED, SHADOW_ERROR})

# Results, deliberately few: an auditor filters on these, and a long tail
# of near-synonyms would make "was anything blocked today?" unanswerable.
RESULT_APPROVED = "APPROVED"
RESULT_BLOCKED = "BLOCKED"
RESULT_ERROR = "ERROR"
RESULT_INFO = "INFO"

# CODEX-044/047 reason codes the Execution Engine raises, mapped to the
# audit event that describes them. Anything unmapped is a gate rejection.
REASON_CODE_TO_EVENT = {
    "DUPLICATE": DUPLICATE_BLOCKED,
    "RECONCILIATION_UNAVAILABLE": RECONCILIATION_BLOCKED,
    "RECONCILIATION_DIRTY": RECONCILIATION_BLOCKED,
    "UNKNOWN_ORDER": UNKNOWN_ORDER_BLOCKED,
    "HALT": HALT_BLOCKED,
    "GATE": GATE_REJECTED,
    "STATE_PERSISTENCE": SHADOW_ERROR,
}

# The Execution Engine reports a gate rejection as "GATE:<gate code>"
# (execution/order_gate.py's OrderGateBlockedError.code), so the audit
# trail can say WHICH check rejected the order rather than lumping every
# gate failure under one event type.
GATE_CODE_TO_EVENT = {
    "BROKER": CONFIG_BLOCKED,
    "LIVE_FLAG": CONFIG_BLOCKED,
    "ENTRY_DISABLED": CONFIG_BLOCKED,
    "COMMIT": CONFIG_BLOCKED,
    "ACCOUNT": CONFIG_BLOCKED,
    "SESSION": CONFIG_BLOCKED,
    "SIGNAL_EXPIRED": SIGNAL_EXPIRED,
    "PRICE_INVALID": PRICE_DEVIATION_BLOCKED,
    "PRICE_DEVIATION": PRICE_DEVIATION_BLOCKED,
    "CASH": CASH_BLOCKED,
    "OPEN_ORDER": DUPLICATE_BLOCKED,
    "DUPLICATE_SIGNAL": DUPLICATE_BLOCKED,
    "DUPLICATE_SELL": DUPLICATE_BLOCKED,
    "DUPLICATE_CANCEL": DUPLICATE_BLOCKED,
    "SYMBOL": INSTRUMENT_BLOCKED,
    "INSTRUMENT": INSTRUMENT_BLOCKED,
    "RECONCILIATION": RECONCILIATION_BLOCKED,
}


class ShadowAuditError(Exception):
    """Raised only when an audit event could not be persisted. Callers
    must NOT swallow this on the order path: an evaluation whose audit
    trail cannot be written is exactly the case an operator must hear
    about."""


@dataclass(frozen=True)
class ShadowAuditEvent:
    shadow_run_id: str
    signal_id: Optional[str]
    internal_order_id: Optional[str]
    event_type: str
    result: str
    reason_code: Optional[str]
    created_at: datetime
    payload: dict
    symbol: Optional[str] = None
    side: Optional[str] = None


def new_run_id():
    return uuid.uuid4().hex


def retention_days():
    raw = os.environ.get("SHADOW_AUDIT_RETENTION_DAYS")
    if raw is None:
        return DEFAULT_RETENTION_DAYS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS
    return value if value > 0 else DEFAULT_RETENTION_DAYS


def _open_conn():
    from state_store import db as state_db

    return state_db.open_db()


def _encode_payload(payload):
    if not payload:
        return None
    try:
        return json.dumps(redact_value(payload), default=str)
    except (TypeError, ValueError):
        return json.dumps({"unserializable_payload": True})


def record_event(*, shadow_run_id, event_type, result, signal_id=None, internal_order_id=None,
                  symbol=None, side=None, reason_code=None, payload=None, now=None, conn=None):
    """Appends ONE audit event. Safe to call concurrently from any number
    of processes: SQLite serializes the write and the whole insert is a
    single committed transaction, so there is no partial-row state a
    reader could observe."""
    if event_type not in EVENT_TYPES:
        raise ShadowAuditError(f"unknown shadow audit event_type {event_type!r}")
    current = now or datetime.now(timezone.utc)
    owns_conn = conn is None
    conn = conn or _open_conn()
    try:
        conn.execute(
            "INSERT INTO shadow_audit_events "
            "(shadow_run_id, signal_id, internal_order_id, symbol, side, event_type, result, "
            "reason_code, payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (shadow_run_id, signal_id, internal_order_id, symbol, side, event_type, result,
             redact_text(reason_code), _encode_payload(payload), current.isoformat()),
        )
        conn.commit()
    except Exception as exc:
        raise ShadowAuditError(f"failed to persist shadow audit event: {exc}") from exc
    finally:
        if owns_conn:
            conn.close()
    return ShadowAuditEvent(
        shadow_run_id=shadow_run_id, signal_id=signal_id, internal_order_id=internal_order_id,
        event_type=event_type, result=result, reason_code=reason_code, created_at=current,
        payload=payload or {}, symbol=symbol, side=side,
    )


def record_block(*, shadow_run_id, event_type, reason_code=None, **kwargs):
    return record_event(
        shadow_run_id=shadow_run_id, event_type=event_type, result=RESULT_BLOCKED,
        reason_code=reason_code, **kwargs,
    )


def event_type_for_reason_code(reason_code):
    if reason_code and reason_code.startswith("GATE:"):
        return GATE_CODE_TO_EVENT.get(reason_code.split(":", 1)[1], GATE_REJECTED)
    return REASON_CODE_TO_EVENT.get(reason_code, GATE_REJECTED)


def read_events(*, shadow_run_id=None, conn=None):
    owns_conn = conn is None
    conn = conn or _open_conn()
    try:
        if shadow_run_id is None:
            rows = conn.execute(
                "SELECT * FROM shadow_audit_events ORDER BY event_id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM shadow_audit_events WHERE shadow_run_id = ? ORDER BY event_id",
                (shadow_run_id,),
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        if owns_conn:
            conn.close()


def runs_without_terminal_event(*, conn=None):
    """Audit completeness check: every shadow_run_id must end in
    SHADOW_COMPLETED or SHADOW_ERROR. Any run listed here is a run whose
    outcome was never recorded -- the exact gap CODEX-048 flagged."""
    owns_conn = conn is None
    conn = conn or _open_conn()
    try:
        placeholders = ",".join("?" for _ in TERMINAL_EVENT_TYPES)
        rows = conn.execute(
            "SELECT DISTINCT shadow_run_id FROM shadow_audit_events WHERE shadow_run_id NOT IN ("
            f"SELECT shadow_run_id FROM shadow_audit_events WHERE event_type IN ({placeholders}))",
            tuple(sorted(TERMINAL_EVENT_TYPES)),
        ).fetchall()
        return [row["shadow_run_id"] for row in rows]
    finally:
        if owns_conn:
            conn.close()


def purge_old_events(*, days=None, now=None, conn=None):
    """Retention: deletes events older than `days` (default
    SHADOW_AUDIT_RETENTION_DAYS, itself defaulting to 30). Returns the
    number of rows deleted. Intended to run from the reconciliation
    service's daily tick, never inline on the order path."""
    limit_days = days if days is not None else retention_days()
    current = now or datetime.now(timezone.utc)
    cutoff = (current - timedelta(days=limit_days)).isoformat()
    owns_conn = conn is None
    conn = conn or _open_conn()
    try:
        cursor = conn.execute("DELETE FROM shadow_audit_events WHERE created_at < ?", (cutoff,))
        conn.commit()
        return cursor.rowcount
    finally:
        if owns_conn:
            conn.close()
