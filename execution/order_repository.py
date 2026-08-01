"""CODEX-047: the ONLY module allowed to change a KIS order's durable
state.

Before this module, `execution/idempotency.py::update_status()` took any
status string and issued a bare

    UPDATE kis_order_idempotency SET status = ? WHERE internal_order_id = ?

with no read of the current state, no expected-state predicate, no
rowcount check, and no durable record of the transition. Every safety
property the pure `execution/order_state_machine.py` graph provides was
therefore advisory: a caller that skipped `transition()` (the cancel
path and the UNKNOWN-reconciliation path both did) wrote whatever it
liked, and two concurrent writers silently overwrote each other's more
recent truth.

This module replaces that with compare-and-set:

    UPDATE kis_order_idempotency
       SET status = ?, version = version + 1, updated_at = ?
     WHERE internal_order_id = ? AND status = ? AND version = ?

If that does not affect exactly one row, the transition FAILED
(`OrderStateConflictError`) -- the caller's belief about the order's
state or version was wrong, so it must re-read reality rather than
retry a transport call. The row update and the append to
`order_state_events` happen inside ONE `BEGIN IMMEDIATE` transaction,
so a state and its event are always both written or both rolled back,
and `BEGIN IMMEDIATE` takes SQLite's write lock up front so two
concurrent writers serialize rather than racing between read and write.

Legality is enforced here too, not just by convention: every transition
runs through `order_state_machine.transition()` first (or, for the one
path that is allowed to leave UNKNOWN, through `reconcile_unknown()`
with `via_reconciliation=True`). There is no code path in this codebase
that can write an order status without passing one of those two.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from execution.order_state_machine import (
    OrderStateTransitionError,
    reconcile_unknown,
    transition,
)


class OrderRepositoryError(Exception):
    """Raised for a structural misuse of this repository (unknown order
    id, a caller-owned transaction already in flight)."""


class OrderStateConflictError(OrderRepositoryError):
    """Raised when the compare-and-set predicate did not match exactly
    one row -- another writer changed the order's state or version
    first. Callers must NEVER respond to this by re-calling the broker:
    the correct response is to re-read the order's real current state
    (and, if it is ambiguous, to leave it UNKNOWN for reconciliation)."""


class OrderRepositoryPersistenceError(OrderRepositoryError):
    """CODEX-055: the durable write failed for an infrastructural reason
    (disk, corruption, lock, constraint). Raw sqlite3 exceptions do NOT
    cross this module's boundary -- callers that had to special-case
    every SQLite error class ended up special-casing none of them, so a
    raw sqlite3.Error escaped the engine's normalization, its operator
    alert and its terminal-audit handling entirely.

    Messages here deliberately carry NO SQL text, bound parameters,
    account numbers or broker payloads. The original exception is
    preserved by chaining (`raise ... from exc`) for the traceback."""


class OrderRepositoryTransactionError(OrderRepositoryPersistenceError):
    """The transaction itself could not be completed (COMMIT failed)."""


class OrderRepositoryRollbackError(OrderRepositoryTransactionError):
    """CODEX-056: COMMIT failed AND the follow-up ROLLBACK failed too, so
    the connection still held its write transaction. SQLite gives every
    other writer "database is locked" for as long as that lasts, which
    on this codebase's single state file means the Shadow audit trail and
    the reconciliation service are locked out as well.

    The connection has been CLOSED before this is raised. It must not be
    reused; the caller opens a fresh one."""


@dataclass(frozen=True)
class OrderRecord:
    internal_order_id: str
    state: str
    version: int
    broker_order_id: Optional[str]
    symbol: str
    side: str
    requested_quantity: Optional[float]


def _row_to_record(row):
    return OrderRecord(
        internal_order_id=row["internal_order_id"], state=row["status"], version=row["version"],
        broker_order_id=row["broker_order_id"], symbol=row["symbol"], side=row["side"],
        requested_quantity=row["requested_quantity"],
    )


def load(conn, order_id) -> Optional[OrderRecord]:
    row = conn.execute(
        "SELECT internal_order_id, status, version, broker_order_id, symbol, side, requested_quantity "
        "FROM kis_order_idempotency WHERE internal_order_id = ?",
        (order_id,),
    ).fetchone()
    return _row_to_record(row) if row is not None else None


def load_events(conn, order_id):
    """Append-only transition history for one order, oldest first."""
    return conn.execute(
        "SELECT from_state, to_state, event_type, payload, version, occurred_at "
        "FROM order_state_events WHERE internal_order_id = ? ORDER BY event_id",
        (order_id,),
    ).fetchall()


def append_creation_event(conn, *, order_id, state, event_type="ORDER_CREATED", payload=None, now=None):
    """Records the order's initial state as event version 0. Called by
    `idempotency.register()` inside the same transaction as the INSERT,
    so an order row without a creation event cannot exist."""
    current = now or datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO order_state_events "
        "(internal_order_id, from_state, to_state, event_type, payload, version, occurred_at) "
        "VALUES (?, NULL, ?, ?, ?, 0, ?)",
        (order_id, state, event_type, _encode_payload(payload), current.isoformat()),
    )


def _encode_payload(payload):
    if payload is None:
        return None
    from execution.secret_redaction import redact_value

    try:
        return json.dumps(redact_value(payload), default=str)
    except (TypeError, ValueError):
        return json.dumps({"unserializable_payload": True})


def invalidate_connection(conn):
    """CODEX-056: discard a connection whose transaction state can no
    longer be trusted. Closing is what actually releases SQLite's write
    lock, so this is the step that keeps later writers from blocking
    forever.

    A failure to close is swallowed -- there is nothing further this
    process can do about it, and masking the original rollback failure
    would lose the more useful information -- but it is always alerted,
    so the swallow is never silent."""
    try:
        conn.close()
        return True
    except Exception as exc:  # noqa: BLE001 -- confined to connection disposal
        _alert(
            "*Order state connection could not be closed after a failed rollback*\n"
            f"- error: {type(exc).__name__}\n"
            "- the process may hold a stale SQLite write lock; restart the service"
        )
        return False


def _alert(message):
    """Operator alert that never raises -- an alerting failure must not
    replace the persistence failure being reported."""
    import logging

    logger = logging.getLogger(__name__)
    logger.error(message)
    try:
        from operations import alerts

        alerts.send_alert(message)
    except Exception as exc:  # noqa: BLE001 -- alerting must not mask the finding
        logger.error("could not send operator alert: %s", exc)


def _abort_transaction(conn, *, stage, cause):
    """Roll back after a failed write. If the rollback ALSO fails, the
    connection is invalidated (closed) and OrderRepositoryRollbackError
    is raised, so the caller knows the connection is gone rather than
    reusing a permanently-locked one."""
    try:
        conn.rollback()
    except Exception as rollback_exc:  # noqa: BLE001 -- normalized below
        closed = invalidate_connection(conn)
        _alert(
            "*Order state transaction rollback failed*\n"
            f"- stage: {stage}\n"
            f"- rollback error: {type(rollback_exc).__name__}\n"
            f"- connection closed: {closed}\n"
            "- action: connection invalidated; manual reconciliation required"
        )
        raise OrderRepositoryRollbackError(
            f"transaction rollback failed during {stage}; the database connection was "
            "invalidated and must not be reused"
        ) from rollback_exc
    raise OrderRepositoryTransactionError(
        f"durable order state could not be written during {stage}"
    ) from cause


def compare_and_set_state(conn, *, order_id, expected_state, next_state, event_type,
                           event_payload=None, expected_version, broker_order_id=None,
                           via_reconciliation=False, now=None) -> OrderRecord:
    """Atomically move `order_id` from (`expected_state`,
    `expected_version`) to `next_state`, appending one
    `order_state_events` row in the same transaction. Returns the new
    OrderRecord.

    Raises OrderStateTransitionError if the transition is illegal (checked
    BEFORE any DB work), OrderStateConflictError if the compare-and-set
    predicate matched anything other than exactly one row, and
    OrderRepositoryError if the caller already holds an open transaction
    on this connection (which would silently widen this function's
    atomicity guarantee to work this module did not write)."""
    if via_reconciliation:
        if expected_state != "UNKNOWN":
            raise OrderStateTransitionError(
                f"via_reconciliation is only valid when leaving UNKNOWN, not {expected_state!r}"
            )
        reconcile_unknown(next_state)
    else:
        transition(expected_state, next_state)

    try:
        in_transaction = conn.in_transaction
    except Exception as exc:  # noqa: BLE001 -- e.g. an already-closed connection
        raise OrderRepositoryPersistenceError(
            "the order state connection is unusable"
        ) from exc
    if in_transaction:
        raise OrderRepositoryError(
            "compare_and_set_state() requires an idle connection -- it opens its own "
            "BEGIN IMMEDIATE transaction so the state change and its event commit together"
        )

    current = now or datetime.now(timezone.utc)
    timestamp = current.isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
    except Exception as exc:  # noqa: BLE001 -- normalized, never raw
        raise OrderRepositoryPersistenceError(
            "could not open a write transaction for the order state"
        ) from exc
    try:
        if broker_order_id is not None:
            cursor = conn.execute(
                "UPDATE kis_order_idempotency "
                "SET status = ?, broker_order_id = ?, version = version + 1, updated_at = ? "
                "WHERE internal_order_id = ? AND status = ? AND version = ?",
                (next_state, broker_order_id, timestamp, order_id, expected_state, expected_version),
            )
        else:
            cursor = conn.execute(
                "UPDATE kis_order_idempotency "
                "SET status = ?, version = version + 1, updated_at = ? "
                "WHERE internal_order_id = ? AND status = ? AND version = ?",
                (next_state, timestamp, order_id, expected_state, expected_version),
            )
        if cursor.rowcount != 1:
            raise OrderStateConflictError(
                f"compare-and-set failed for order {order_id!r}: expected state "
                f"{expected_state!r} at version {expected_version!r}, but {cursor.rowcount} rows "
                "matched -- the order's real state must be re-read, never re-submitted"
            )
        conn.execute(
            "INSERT INTO order_state_events "
            "(internal_order_id, from_state, to_state, event_type, payload, version, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (order_id, expected_state, next_state, event_type, _encode_payload(event_payload),
             expected_version + 1, timestamp),
        )
    except OrderStateConflictError:
        # A lost compare-and-set is a normal, expected outcome -- not an
        # infrastructure failure -- so it keeps its own type.
        try:
            conn.rollback()
        except Exception as rollback_exc:  # noqa: BLE001 -- normalized below
            closed = invalidate_connection(conn)
            _alert(
                "*Order state transaction rollback failed*\n"
                f"- stage: compare-and-set conflict\n"
                f"- rollback error: {type(rollback_exc).__name__}\n"
                f"- connection closed: {closed}"
            )
            raise OrderRepositoryRollbackError(
                "transaction rollback failed after a compare-and-set conflict; the database "
                "connection was invalidated and must not be reused"
            ) from rollback_exc
        raise
    except Exception as exc:  # noqa: BLE001 -- every write failure is normalized
        _abort_transaction(conn, stage="order state write", cause=exc)
    try:
        conn.commit()
    except Exception as exc:  # noqa: BLE001 -- normalized, never raw
        # CODEX-056: a failed COMMIT leaves the write transaction open,
        # which locks out every other writer -- including the Shadow
        # audit trail on the same file. Roll back, and if that fails too,
        # close the connection so the lock is actually released.
        _abort_transaction(conn, stage="commit", cause=exc)
    record = load(conn, order_id)
    if record is None:  # pragma: no cover -- the UPDATE above matched a row
        raise OrderRepositoryError(f"order {order_id!r} vanished during compare-and-set")
    return record


def advance(conn, record: OrderRecord, next_state, *, event_type, event_payload=None,
            broker_order_id=None, via_reconciliation=False, now=None) -> OrderRecord:
    """compare_and_set_state() using `record`'s own state/version as the
    expected values -- the shape every caller in execution_engine.py
    uses, so no caller ever hand-writes an expected state or version."""
    return compare_and_set_state(
        conn, order_id=record.internal_order_id, expected_state=record.state,
        next_state=next_state, event_type=event_type, event_payload=event_payload,
        expected_version=record.version, broker_order_id=broker_order_id,
        via_reconciliation=via_reconciliation, now=now,
    )
