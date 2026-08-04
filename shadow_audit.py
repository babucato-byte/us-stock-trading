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

Every path that can block, approve, or fail MUST emit EXACTLY ONE
terminal event (`SHADOW_COMPLETED`, `SHADOW_BLOCKED` or `SHADOW_ERROR`)
for its run, so an operator auditing a trading day never finds a run
that just stops mid-way, and never finds one with two contradictory
outcomes. `audit_integrity_report()` checks both halves of that
invariant.

The approval events are recorded BEFORE the transport call, not after
it: `GATE_APPROVED` and `EXECUTION_PLANNED` are written by
execution/execution_engine.py between the gate passing and the broker's
order-submission method being invoked. Recording them after the engine
returned -- as this module's callers originally did -- means a crash
during the broker call leaves an order that reached KIS with no audit
record of the approval that authorized it.

(The wording above deliberately avoids spelling out that call
expression: tests/test_execution_engine.py scans every non-test source
file for it and treats a match as an unsanctioned call site.)

Sensitive values are redacted at THIS boundary (execution/
secret_redaction.py), independently of whatever the caller did, so a
payload carrying an account number or token can never be persisted here
even if a future call site forgets.
"""

import json
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from execution.secret_redaction import redact_text, redact_value

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 30
# CODEX-048: a concurrent writer must WAIT for the SQLite write lock, not
# lose its event. busy_timeout covers the common case; these retries cover
# the residual "database is locked" that a busy handler does not.
WRITE_RETRIES = 5
WRITE_RETRY_BASE_SECONDS = 0.05

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
SHADOW_BLOCKED = "SHADOW_BLOCKED"
SHADOW_ERROR = "SHADOW_ERROR"
# An evaluation that stopped before the Order Gate ran. Records which
# pre-gate checks were reached and how far each got, so a candidate
# blocked early still leaves an answerable trail instead of a bare
# "hypothetical=None". It never carries a gate verdict -- the gate did
# not run, and inventing one would be worse than recording nothing.
HYPOTHETICAL_INCOMPLETE = "HYPOTHETICAL_INCOMPLETE"
# The KIS pipeline was not handed this candidate at all: its venue has
# no KIS order exchange code. It stays in the analysis output.
KIS_PIPELINE_EXCLUDED = "KIS_PIPELINE_EXCLUDED"
# The HALT state as read AT RUN TIME, not as recorded in a snapshot.
HALT_CHECKED = "HALT_CHECKED"
# A strategy exit that HALT stopped. Not terminal: the run still ends in
# exactly one SHADOW_COMPLETED/SHADOW_BLOCKED/SHADOW_ERROR.
EXIT_BLOCKED_HALT = "EXIT_BLOCKED_HALT"

EVENT_TYPES = frozenset({
    SIGNAL_RECEIVED, CONFIG_BLOCKED, SIGNAL_EXPIRED, INSTRUMENT_BLOCKED,
    PRICE_DEVIATION_BLOCKED, CASH_BLOCKED, RECONCILIATION_BLOCKED, UNKNOWN_ORDER_BLOCKED,
    DUPLICATE_BLOCKED, HALT_BLOCKED, GATE_REJECTED, GATE_APPROVED, EXECUTION_PLANNED,
    SHADOW_COMPLETED, SHADOW_BLOCKED, SHADOW_ERROR, HYPOTHETICAL_INCOMPLETE,
    KIS_PIPELINE_EXCLUDED, HALT_CHECKED, EXIT_BLOCKED_HALT,
})

# CODEX-048: every run ends in EXACTLY ONE of these -- not zero (an
# evaluation whose outcome was never recorded) and not two (an ambiguous
# audit trail an operator cannot reduce to a single answer).
TERMINAL_EVENT_TYPES = frozenset({SHADOW_COMPLETED, SHADOW_BLOCKED, SHADOW_ERROR})

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
    """Raised when an audit event could not be persisted.

    CODEX-048: callers on the order path must NOT swallow this. An
    evaluation whose audit trail cannot be written must fail the run and
    block the order -- an order placed with no durable record of the
    approval that authorized it is precisely the state this audit trail
    exists to make impossible. `handle_audit_failure()` below is the
    sanctioned response."""


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


def _insert_once(conn, values):
    """One explicit BEGIN IMMEDIATE / INSERT / COMMIT. Returns the new
    event_id, having CONFIRMED the row is committed rather than assuming
    the INSERT succeeded."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            "INSERT INTO shadow_audit_events "
            "(shadow_run_id, signal_id, internal_order_id, symbol, side, event_type, result, "
            "reason_code, payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        event_id = cursor.lastrowid
        if cursor.rowcount != 1 or not event_id:
            raise ShadowAuditError("shadow audit INSERT did not affect exactly one row")
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    # Commit confirmation: re-read the row through the same connection
    # AFTER commit. A commit that silently did not persist the row would
    # otherwise be indistinguishable from success.
    confirmed = conn.execute(
        "SELECT event_id FROM shadow_audit_events WHERE event_id = ?", (event_id,),
    ).fetchone()
    if confirmed is None:
        raise ShadowAuditError(f"shadow audit event {event_id} was not durable after commit")
    return event_id


def record_event(*, shadow_run_id, event_type, result, signal_id=None, internal_order_id=None,
                  symbol=None, side=None, reason_code=None, payload=None, now=None, conn=None):
    """Appends ONE audit event, durably.

    Safe to call concurrently from any number of processes: the insert
    runs inside its own `BEGIN IMMEDIATE` transaction (SQLite's write
    lock is taken up front), the connection carries a busy timeout, and a
    residual "database is locked" is retried with backoff rather than
    dropping the event. Raises ShadowAuditError if -- after those
    retries -- the event is not durably committed; callers on the order
    path must treat that as a hard block."""
    if event_type not in EVENT_TYPES:
        raise ShadowAuditError(f"unknown shadow audit event_type {event_type!r}")
    current = now or datetime.now(timezone.utc)
    values = (
        shadow_run_id, signal_id, internal_order_id, symbol, side, event_type, result,
        redact_text(reason_code), _encode_payload(payload), current.isoformat(),
    )
    owns_conn = conn is None
    conn = conn or _open_conn()
    try:
        last_error = None
        for attempt in range(WRITE_RETRIES):
            try:
                _insert_once(conn, values)
                last_error = None
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise ShadowAuditError(f"failed to persist shadow audit event: {exc}") from exc
                last_error = exc
                if attempt < WRITE_RETRIES - 1:
                    time.sleep(WRITE_RETRY_BASE_SECONDS * (2 ** attempt))
            except ShadowAuditError:
                raise
            except Exception as exc:
                raise ShadowAuditError(f"failed to persist shadow audit event: {exc}") from exc
        if last_error is not None:
            raise ShadowAuditError(
                f"failed to persist shadow audit event after {WRITE_RETRIES} attempts: {last_error}"
            ) from last_error
    finally:
        if owns_conn:
            conn.close()
    return ShadowAuditEvent(
        shadow_run_id=shadow_run_id, signal_id=signal_id, internal_order_id=internal_order_id,
        event_type=event_type, result=result, reason_code=reason_code, created_at=current,
        payload=payload or {}, symbol=symbol, side=side,
    )


class AuditInvariantError(ShadowAuditError):
    """Raised when a run is being finalized with a DIFFERENT terminal
    event than the one it already ended on. Two contradictory outcomes
    for one run is an audit trail an operator cannot reduce to a single
    answer, so it is surfaced rather than silently resolved."""


def _existing_terminal_events(conn, shadow_run_id):
    placeholders = ",".join("?" for _ in TERMINAL_EVENT_TYPES)
    rows = conn.execute(
        f"SELECT event_type FROM shadow_audit_events WHERE shadow_run_id = ? "
        f"AND event_type IN ({placeholders})",
        (shadow_run_id, *sorted(TERMINAL_EVENT_TYPES)),
    ).fetchall()
    return [row["event_type"] for row in rows]


def finalize_audit_run(*, audit_run_id, terminal_event, internal_order_id=None, action=None,
                        symbol=None, side=None, reason_code=None, payload=None, now=None,
                        conn=None):
    """CODEX-053: the ONE way a run is ended.

    Every execution path -- buy, sell, cancel, success, block, error --
    finishes here, so "exactly one terminal event per run" is a property
    of a single function rather than of every call site remembering the
    rule.

    Re-finalizing with the SAME terminal event is an idempotent no-op
    (a retry, or a `finally` safety net running after the explicit
    handler already finished the run, must not create a second row).
    Re-finalizing with a DIFFERENT one raises AuditInvariantError and
    alerts: that means two code paths each believe they own the outcome,
    which is a defect worth surfacing, not papering over.

    The database enforces the same rule (migration 10's partial unique
    index), so a race between two finalizers cannot produce two terminal
    rows even though both passed the read below."""
    if terminal_event not in TERMINAL_EVENT_TYPES:
        raise ShadowAuditError(
            f"{terminal_event!r} is not a terminal event; expected one of "
            f"{sorted(TERMINAL_EVENT_TYPES)}"
        )
    owns_conn = conn is None
    conn = conn or _open_conn()
    try:
        existing = _existing_terminal_events(conn, audit_run_id)
        if existing:
            return _resolve_existing_terminal(audit_run_id, terminal_event, existing)
        try:
            record_event(
                shadow_run_id=audit_run_id, event_type=terminal_event,
                result=_result_for_terminal(terminal_event), symbol=symbol, side=side,
                internal_order_id=internal_order_id, reason_code=reason_code,
                payload=_with_action(payload, action), now=now, conn=conn,
            )
        except ShadowAuditError:
            # The DB index is the authority on "already finalized"; a
            # losing race looks like a persistence failure until we
            # re-read it.
            existing = _existing_terminal_events(conn, audit_run_id)
            if existing:
                return _resolve_existing_terminal(audit_run_id, terminal_event, existing)
            raise
        return terminal_event
    finally:
        if owns_conn:
            conn.close()


def _with_action(payload, action):
    if action is None:
        return payload
    merged = dict(payload or {})
    merged["action"] = action
    merged["terminal"] = True
    return merged


def _result_for_terminal(terminal_event):
    return {
        SHADOW_COMPLETED: RESULT_APPROVED,
        SHADOW_BLOCKED: RESULT_BLOCKED,
        SHADOW_ERROR: RESULT_ERROR,
    }[terminal_event]


def _resolve_existing_terminal(audit_run_id, terminal_event, existing):
    if all(event == terminal_event for event in existing):
        return terminal_event  # idempotent no-op
    message = (
        f"shadow run {audit_run_id} already ended on {sorted(set(existing))} but was "
        f"finalized again as {terminal_event}"
    )
    logger.error(message)
    try:
        from operations import alerts

        alerts.send_alert(f"*Shadow audit invariant violated*\n- {message}")
    except Exception as exc:  # noqa: BLE001 -- alerting must not mask the finding
        logger.error("could not alert on audit invariant violation: %s", exc)
    raise AuditInvariantError(message)


class ShadowAuditFailure(Exception):
    """Raised by handle_audit_failure() -- the caller-facing signal that
    an evaluation must be abandoned because its audit trail could not be
    written."""


def handle_audit_failure(exc, *, shadow_run_id, symbol=None, side=None, stage="unknown"):
    """The sanctioned response to a ShadowAuditError on the order path
    (CODEX-048's audit-failure policy):

        1. try ONCE more to record SHADOW_ERROR for this run, on a fresh
           connection (the original failure may have been connection- or
           transaction-scoped);
        2. alert the operator through the existing alert channel;
        3. raise ShadowAuditFailure so the caller blocks the evaluation.

    Never swallows. A `try/except: pass` around audit persistence on a
    live-order path is exactly the defect this replaces."""
    logger.error("shadow audit persistence failed at %s for run %s: %s", stage, shadow_run_id, exc)
    try:
        record_event(
            shadow_run_id=shadow_run_id, event_type=SHADOW_ERROR, result=RESULT_ERROR,
            symbol=symbol, side=side, reason_code="AUDIT_PERSISTENCE_FAILED",
            payload={"stage": stage, "error": str(exc)},
        )
    except Exception as retry_exc:  # noqa: BLE001 -- best-effort second chance
        logger.error("retrying SHADOW_ERROR also failed for run %s: %s", shadow_run_id, retry_exc)
    try:
        from operations import alerts

        alerts.send_alert(
            f"*Shadow audit persistence failed*\n- run: {shadow_run_id}\n- stage: {stage}\n"
            f"- symbol: {symbol}\n- action: evaluation blocked, no order submitted"
        )
    except Exception as alert_exc:  # noqa: BLE001 -- alerting must not mask the failure
        logger.error("could not alert on shadow audit failure: %s", alert_exc)
    raise ShadowAuditFailure(
        f"shadow audit could not be persisted at {stage} for run {shadow_run_id} -- "
        "evaluation blocked"
    ) from exc


def record_block(*, shadow_run_id, event_type, reason_code=None, **kwargs):
    return record_event(
        shadow_run_id=shadow_run_id, event_type=event_type, result=RESULT_BLOCKED,
        reason_code=reason_code, **kwargs,
    )


RESULT_TO_TERMINAL_EVENT = {
    RESULT_APPROVED: SHADOW_COMPLETED,
    RESULT_INFO: SHADOW_COMPLETED,
    RESULT_BLOCKED: SHADOW_BLOCKED,
    RESULT_ERROR: SHADOW_ERROR,
}


def terminal_event_for(result):
    """The ONE terminal event a run with this result must end on. An
    unrecognized result is treated as an error rather than silently
    reported as completed."""
    return RESULT_TO_TERMINAL_EVENT.get(result, SHADOW_ERROR)


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


def _terminal_counts(conn):
    placeholders = ",".join("?" for _ in TERMINAL_EVENT_TYPES)
    return conn.execute(
        "SELECT shadow_run_id, SUM(CASE WHEN event_type IN "
        f"({placeholders}) THEN 1 ELSE 0 END) AS terminal_count "
        "FROM shadow_audit_events GROUP BY shadow_run_id",
        tuple(sorted(TERMINAL_EVENT_TYPES)),
    ).fetchall()


def runs_without_terminal_event(*, conn=None):
    """Audit completeness check: every shadow_run_id must end in exactly
    one of SHADOW_COMPLETED / SHADOW_BLOCKED / SHADOW_ERROR. Any run
    listed here is a run whose outcome was never recorded."""
    owns_conn = conn is None
    conn = conn or _open_conn()
    try:
        return [row["shadow_run_id"] for row in _terminal_counts(conn) if row["terminal_count"] == 0]
    finally:
        if owns_conn:
            conn.close()


def runs_with_multiple_terminal_events(*, conn=None):
    """The other half of the same invariant: two terminal events for one
    run is an audit trail an operator cannot reduce to a single outcome,
    so it is a defect in its own right, not a harmless duplicate."""
    owns_conn = conn is None
    conn = conn or _open_conn()
    try:
        return [row["shadow_run_id"] for row in _terminal_counts(conn) if row["terminal_count"] > 1]
    finally:
        if owns_conn:
            conn.close()


def audit_integrity_report(*, conn=None):
    """Single call an operator/health check can make: both halves of the
    exactly-one-terminal-event invariant."""
    owns_conn = conn is None
    conn = conn or _open_conn()
    try:
        counts = _terminal_counts(conn)
        return {
            "runs_without_terminal_event": [r["shadow_run_id"] for r in counts if r["terminal_count"] == 0],
            "runs_with_multiple_terminal_events": [
                r["shadow_run_id"] for r in counts if r["terminal_count"] > 1
            ],
            "total_runs": len(counts),
        }
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
