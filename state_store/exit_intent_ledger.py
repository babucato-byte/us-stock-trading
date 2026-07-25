"""CODEX-024: durable exit-intent ledger.

A position exit is reserved here BEFORE any broker call is made, and the
reservation is committed to disk (SQLite) independently of
positions/store.py's JSON position record. This is what actually
survives a crash between "we decided to submit a sell" and "we know
whether the broker accepted/filled it": positions/store.py's
locked_position() already serializes concurrent in-process callers, but
if the process dies mid-broker-call, nothing about that attempt would
otherwise be recorded anywhere, and a naive restart would see the
position's last-known state (e.g. STOP_ACTIVE) and try to exit it again
-- exactly the CODEX-024 duplicate-sell bug.

State machine (mirrors order_intent_ledger.py's reserve/commit/abort
shape, adapted for exits' extra intermediate states):

    RESERVED -> SUBMITTED -> CONFIRMED
             -> SUBMISSION_UNKNOWN -> RECONCILIATION_REQUIRED -> CONFIRMED
             -> ABORTED (never reached the broker at all)

Only one non-terminal (RESERVED/SUBMITTED/SUBMISSION_UNKNOWN/
RECONCILIATION_REQUIRED) intent may exist per position_id at a time --
reserve() raises DuplicateExitIntentError if one is already active,
which is exactly the signal callers use to avoid a second broker call.
"""

import uuid

STATE_RESERVED = "RESERVED"
STATE_SUBMITTED = "SUBMITTED"
STATE_SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
STATE_RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
STATE_CONFIRMED = "CONFIRMED"
STATE_ABORTED = "ABORTED"

NON_TERMINAL_STATES = {STATE_RESERVED, STATE_SUBMITTED, STATE_SUBMISSION_UNKNOWN, STATE_RECONCILIATION_REQUIRED}
TERMINAL_STATES = {STATE_CONFIRMED, STATE_ABORTED}
VALID_STATES = NON_TERMINAL_STATES | TERMINAL_STATES


class ExitIntentError(Exception):
    pass


class DuplicateExitIntentError(ExitIntentError):
    """Raised by reserve() when a non-terminal intent already exists for
    this position_id -- the caller must not submit a new broker order and
    should instead resolve the existing intent."""


def _now_iso():
    from state_store.db import now_iso
    return now_iso()


def get_active_intent(conn, position_id):
    """Return the most recent non-terminal intent for `position_id`, or
    None if there isn't one."""
    placeholders = ",".join("?" for _ in NON_TERMINAL_STATES)
    row = conn.execute(
        f"SELECT * FROM exit_intents WHERE position_id = ? AND state IN ({placeholders}) "
        "ORDER BY created_at DESC LIMIT 1",
        (position_id, *NON_TERMINAL_STATES),
    ).fetchone()
    return dict(row) if row else None


def get_by_id(conn, intent_id):
    row = conn.execute("SELECT * FROM exit_intents WHERE intent_id = ?", (intent_id,)).fetchone()
    return dict(row) if row else None


def get_by_client_order_id(conn, client_order_id):
    row = conn.execute(
        "SELECT * FROM exit_intents WHERE client_order_id = ?", (client_order_id,)
    ).fetchone()
    return dict(row) if row else None


def reserve(conn, position_id, reason, requested_qty, client_order_id):
    """Durably reserve a new exit intent. Raises DuplicateExitIntentError
    without writing anything if an active (non-terminal) intent already
    exists for this position_id -- the caller must not call the broker in
    that case. Returns the new intent_id."""
    existing = get_active_intent(conn, position_id)
    if existing is not None:
        raise DuplicateExitIntentError(
            f"Active exit intent {existing['intent_id']!r} (state={existing['state']!r}) "
            f"already exists for position {position_id!r}"
        )
    intent_id = f"exitintent_{uuid.uuid4().hex[:16]}"
    now = _now_iso()
    conn.execute(
        "INSERT INTO exit_intents "
        "(intent_id, position_id, client_order_id, reason, requested_qty, "
        " confirmed_filled_qty, state, broker_order_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 0, ?, NULL, ?, ?)",
        (intent_id, position_id, client_order_id, reason, requested_qty, STATE_RESERVED, now, now),
    )
    conn.commit()
    return intent_id


def _transition(conn, intent_id, new_state, *, broker_order_id=None, confirmed_filled_qty=None):
    if new_state not in VALID_STATES:
        raise ExitIntentError(f"Unknown exit intent state: {new_state!r}")
    existing = get_by_id(conn, intent_id)
    if existing is None:
        raise ExitIntentError(f"Unknown exit intent: {intent_id!r}")
    if existing["state"] in TERMINAL_STATES:
        raise ExitIntentError(
            f"Cannot transition exit intent {intent_id!r} out of terminal state {existing['state']!r}"
        )
    fields = ["state = ?", "updated_at = ?"]
    values = [new_state, _now_iso()]
    if broker_order_id is not None:
        fields.append("broker_order_id = ?")
        values.append(broker_order_id)
    if confirmed_filled_qty is not None:
        fields.append("confirmed_filled_qty = ?")
        values.append(confirmed_filled_qty)
    values.append(intent_id)
    conn.execute(f"UPDATE exit_intents SET {', '.join(fields)} WHERE intent_id = ?", values)
    conn.commit()


def mark_submitted(conn, intent_id, broker_order_id=None):
    _transition(conn, intent_id, STATE_SUBMITTED, broker_order_id=broker_order_id)


def mark_submission_unknown(conn, intent_id):
    """The broker call raised/timed out -- we genuinely don't know if the
    order reached the broker. Never auto-retried from this state; only an
    explicit reconciliation call may move it forward."""
    _transition(conn, intent_id, STATE_SUBMISSION_UNKNOWN)


def mark_reconciliation_required(conn, intent_id):
    _transition(conn, intent_id, STATE_RECONCILIATION_REQUIRED)


def mark_confirmed(conn, intent_id, confirmed_filled_qty):
    _transition(conn, intent_id, STATE_CONFIRMED, confirmed_filled_qty=confirmed_filled_qty)


def update_progress(conn, intent_id, confirmed_filled_qty):
    """Record a partial-fill observation without closing the intent out --
    state stays whatever it already was (typically SUBMITTED). Used when a
    broker reports partially_filled: some quantity has genuinely traded
    and must be reflected, but the intent as a whole isn't done yet."""
    existing = get_by_id(conn, intent_id)
    if existing is None:
        raise ExitIntentError(f"Unknown exit intent: {intent_id!r}")
    if existing["state"] in TERMINAL_STATES:
        raise ExitIntentError(f"Cannot update progress on terminal exit intent {intent_id!r}")
    conn.execute(
        "UPDATE exit_intents SET confirmed_filled_qty = ?, updated_at = ? WHERE intent_id = ?",
        (confirmed_filled_qty, _now_iso(), intent_id),
    )
    conn.commit()


def mark_aborted(conn, intent_id):
    """The intent never reached the broker at all (e.g. it turned out
    there was nothing left to exit). Distinct from CONFIRMED so an
    aborted intent is never mistaken for "the broker confirmed 0 shares
    filled"."""
    _transition(conn, intent_id, STATE_ABORTED)
