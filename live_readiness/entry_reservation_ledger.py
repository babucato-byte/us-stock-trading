"""CODEX-031/034: durable, authoritative tracking of live-entry cash
usage -- the record live_readiness/order_gateway.py reads from instead of
trusting whatever a caller's LiveEntryContext claims.

Before this module, `LiveEntryContext.max_order_notional_krw`,
`available_cash_krw`, `max_position_count`, `current_open_position_count`,
`max_daily_entries`, and `today_entry_count` were all plain fields a
caller supplied directly -- nothing independently verified them against
reality. A caller (or a bug) could self-report `available_cash_krw=
3_000_000` and the gateway would approve an order for nearly that whole
amount.

This ledger is that tracking: every live-entry attempt that passes the
gateway's other checks (allow-list, symbol match, FX rate, fresh cash
figure) reserves its notional here BEFORE the broker is ever called
(mirroring state_store/exit_intent_ledger.py's "durable before broker
call" pattern), and the gateway computes how much of the currently
available (percent-scaled) cash is already spoken for by summing this
table -- never from caller input.

Lifecycle (CODEX-034 adds SUBMISSION_UNKNOWN as an explicit third
non-terminal state, between RESERVED and the two terminal states):

    RESERVED (budget held, broker not yet called)
        -> COMMITTED (broker definitively accepted -- 2xx response)
        -> SUBMISSION_UNKNOWN (the broker call raised an AMBIGUOUS
           failure -- timeout, connection reset -- that does not prove
           the broker never received the order)
        -> RELEASED (the broker DEFINITIVELY rejected the order, or the
           reservation never even reached the broker call at all, e.g. a
           pre-network validation error)

SUBMISSION_UNKNOWN is NOT terminal and is NOT released automatically --
CODEX-034's whole point is that releasing an ambiguous reservation lets a
naive retry double-submit against the broker while the authoritative
snapshot under-counts real exposure by the first (possibly-live) order's
notional. A SUBMISSION_UNKNOWN reservation keeps counting against the
allocatable cash exactly like RESERVED/COMMITTED do, until an operator or
a future reconciliation pass (reconcile_by_client_order_id(), which looks
the order up at the broker by the client_order_id reserved before the
call) resolves it one way or the other -- see that function's docstring.

COMMITTED reservations are further linked (link_position()) to the
position_id positions/lifecycle.py creates once the broker's acceptance
is known, so build_snapshot() can tell whether the capital they funded is
still tied up in an open position (canonical SQLite, CODEX-028) or has
since been freed by that position closing.
"""

import fcntl
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from market_hours import EASTERN

STATE_RESERVED = "RESERVED"
STATE_COMMITTED = "COMMITTED"
STATE_SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
STATE_RELEASED = "RELEASED"

NON_TERMINAL_STATES = {STATE_RESERVED, STATE_COMMITTED, STATE_SUBMISSION_UNKNOWN}
TERMINAL_STATES = {STATE_RELEASED}
VALID_STATES = NON_TERMINAL_STATES | TERMINAL_STATES

BASE_DIR = Path(__file__).resolve().parent.parent
_LOCK_FILE = BASE_DIR / "LIVE_ENTRY_RESERVATION.lock"
LOCK_TIMEOUT_SECONDS = 5.0


class EntryReservationError(Exception):
    pass


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def reservation_lock(timeout=LOCK_TIMEOUT_SECONDS):
    """Process-level exclusive lock guarding the entire read-snapshot-
    then-reserve span, exactly like positions/store.py's `_store_lock` --
    without this, two concurrent live-entry attempts could each read the
    same "budget available" snapshot before either has reserved anything,
    and both pass their pre-checks even though their combined notional
    would exceed what's actually allocatable."""
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(_LOCK_FILE, "a+")
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise EntryReservationError(
                        f"Could not acquire live-entry reservation lock within {timeout}s"
                    )
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        finally:
            lock_file.close()


def trading_day_start_utc(now=None):
    """Start of the current US-Eastern trading day (00:00 ET), expressed
    in UTC -- the cutoff build_snapshot() uses to reset today_entry_count
    each day. Using the Eastern calendar date (not UTC's) matches every
    other day-boundary decision in this codebase (market_hours.py, EOD
    cutoffs)."""
    now = now or datetime.now(timezone.utc)
    eastern_now = now.astimezone(EASTERN)
    day_start_eastern = eastern_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start_eastern.astimezone(timezone.utc)


def reserve(conn, symbol, notional_krw, client_order_id, *, commit=True):
    """Durably reserve `notional_krw` of allocatable cash for a live
    entry attempt, BEFORE the broker is ever called. `client_order_id`
    (CODEX-034) is required and must be the exact id about to be sent to
    the broker, so an ambiguous-failure reservation can later be
    reconciled by looking that same order up. Returns the new
    reservation_id. Caller must hold reservation_lock() across the
    read-snapshot-then-reserve span."""
    if notional_krw <= 0:
        raise EntryReservationError(f"notional_krw must be positive, got {notional_krw!r}")
    if not client_order_id or not isinstance(client_order_id, str):
        raise EntryReservationError(f"client_order_id is required, got {client_order_id!r}")
    reservation_id = f"liveentry_{uuid.uuid4().hex[:16]}"
    now = _now_iso()
    conn.execute(
        "INSERT INTO live_entry_reservations "
        "(reservation_id, symbol, notional_krw, state, position_id, client_order_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
        (reservation_id, symbol, notional_krw, STATE_RESERVED, client_order_id, now, now),
    )
    if commit:
        conn.commit()
    return reservation_id


def _transition(conn, reservation_id, new_state, *, position_id=None, commit=True):
    existing = get_by_id(conn, reservation_id)
    if existing is None:
        raise EntryReservationError(f"Unknown reservation_id: {reservation_id!r}")
    if existing["state"] in TERMINAL_STATES:
        raise EntryReservationError(
            f"Cannot transition reservation {reservation_id!r} out of terminal state {existing['state']!r}"
        )
    fields = ["state = ?", "updated_at = ?"]
    values = [new_state, _now_iso()]
    if position_id is not None:
        fields.append("position_id = ?")
        values.append(position_id)
    values.append(reservation_id)
    conn.execute(f"UPDATE live_entry_reservations SET {', '.join(fields)} WHERE reservation_id = ?", values)
    if commit:
        conn.commit()


def mark_committed(conn, reservation_id, *, position_id=None, commit=True):
    """The broker definitively accepted the order (2xx response).
    `position_id`, if already known, links this reservation to the
    position it funded so build_snapshot() can later tell whether that
    position has since closed."""
    _transition(conn, reservation_id, STATE_COMMITTED, position_id=position_id, commit=commit)


def mark_submission_unknown(conn, reservation_id, *, commit=True):
    """CODEX-034: the broker call raised an AMBIGUOUS failure (timeout,
    connection reset) that does not prove the order was never received.
    Deliberately NOT released -- stays counted against allocatable cash
    until reconcile_by_client_order_id() (or an operator) resolves it.
    Never auto-retried from this state by this module."""
    _transition(conn, reservation_id, STATE_SUBMISSION_UNKNOWN, commit=commit)


def mark_released(conn, reservation_id, *, commit=True):
    """The broker DEFINITIVELY rejected the order (an HTTP error response
    was actually received), or the reservation never reached the broker
    call at all (a pre-network validation/safety-gate failure) -- release
    the cash hold immediately. CODEX-034: never call this for an
    ambiguous failure -- use mark_submission_unknown() instead."""
    _transition(conn, reservation_id, STATE_RELEASED, commit=commit)


def link_position(conn, reservation_id, position_id, *, commit=True):
    """Attach a position_id to an already-COMMITTED reservation once
    positions/lifecycle.py has created the record (submit_order() itself
    doesn't know the position_id yet -- it's created by the caller after
    submit_order() returns)."""
    conn.execute(
        "UPDATE live_entry_reservations SET position_id = ?, updated_at = ? WHERE reservation_id = ?",
        (position_id, _now_iso(), reservation_id),
    )
    if commit:
        conn.commit()


def get_by_id(conn, reservation_id):
    row = conn.execute(
        "SELECT * FROM live_entry_reservations WHERE reservation_id = ?", (reservation_id,)
    ).fetchone()
    return dict(row) if row else None


def get_by_client_order_id(conn, client_order_id):
    row = conn.execute(
        "SELECT * FROM live_entry_reservations WHERE client_order_id = ?", (client_order_id,)
    ).fetchone()
    return dict(row) if row else None


def reconcile_by_client_order_id(conn, client_order_id, broker):
    """CODEX-034's required restart/retry reconciliation path: resolve an
    already-reserved (RESERVED/SUBMISSION_UNKNOWN) reservation against
    the broker's own current view of that order -- NEVER submits a new
    order itself. Returns the (possibly unchanged) reservation dict, or
    None if there is no such reservation at all.

    - Broker lookup raises, or reports no such order -> left exactly as
      SUBMISSION_UNKNOWN (never auto-released, never auto-resubmitted --
      an operator or a later reconciliation attempt must resolve it).
    - Broker reports accepted/new/pending_*/partially_filled/filled
      (i.e. anything positions/order_status.py does NOT classify as a
      definitive non-existence) -> mark_committed(), linking whatever
      position_id the caller already knows (if any).
    - Broker explicitly reports the order as rejected/canceled/expired
      -> mark_released().
    """
    from positions import order_status

    reservation = get_by_client_order_id(conn, client_order_id)
    if reservation is None:
        return None
    if reservation["state"] not in NON_TERMINAL_STATES:
        return reservation  # already resolved

    try:
        broker_order = broker.get_order_by_client_order_id(client_order_id)
    except Exception:
        return reservation  # lookup failure -- never treat as resolved

    if broker_order is None:
        return reservation  # broker has never heard of it -- stays SUBMISSION_UNKNOWN

    order_info = order_status.extract_order_info(broker_order)
    status = order_info.get("status")
    if isinstance(status, str) and status.lower() in {"rejected", "canceled", "cancelled", "expired"}:
        mark_released(conn, reservation["reservation_id"])
    else:
        mark_committed(conn, reservation["reservation_id"], position_id=reservation.get("position_id"))
    return get_by_id(conn, reservation["reservation_id"])


class LiveRiskSnapshot:
    def __init__(self, pending_buy_reservations_krw, unknown_submission_reservations_krw,
                 current_open_position_cost_krw, active_position_count, today_entry_count):
        self.pending_buy_reservations_krw = pending_buy_reservations_krw
        self.unknown_submission_reservations_krw = unknown_submission_reservations_krw
        self.current_open_position_cost_krw = current_open_position_cost_krw
        self.active_position_count = active_position_count
        self.today_entry_count = today_entry_count

    @property
    def total_committed_krw(self):
        """Sum of all three deduction categories -- the amount already
        "spoken for" against allocatable cash, regardless of category."""
        return (
            self.pending_buy_reservations_krw
            + self.unknown_submission_reservations_krw
            + self.current_open_position_cost_krw
        )


def build_snapshot(conn, *, now=None):
    """Compute the authoritative "how much cash is already spoken for /
    how many entries has today used / how many positions are concurrently
    open" snapshot, entirely from durable state -- never from caller
    input. Must be called while holding reservation_lock() for the
    result to be trustworthy at the moment a new reservation is about to
    be made.

    Three separate deduction categories, matching the formula in
    live_readiness/order_gateway.py::validate_and_size_live_entry():

    - pending_buy_reservations_krw: sum of RESERVED reservations (broker
      not yet called, or the call is in flight) -- NOT day-scoped, since
      an order legitimately reserved yesterday and still pending today
      must still be deducted.
    - unknown_submission_reservations_krw (CODEX-034): sum of
      SUBMISSION_UNKNOWN reservations -- the broker call's outcome is
      ambiguous, so this capital must be treated as still potentially
      committed until reconciled.
    - current_open_position_cost_krw: sum of COMMITTED reservations whose
      linked position (if any) is not yet terminal in the canonical
      SQLite `positions` table (CODEX-028) -- capital tied up in a
      currently-open position. A COMMITTED reservation whose position has
      since closed is excluded (its capital is reflected in the broker's
      own reported current_available_cash_krw once settled).
    - today_entry_count: count of reservations created since the start of
      the current US-Eastern trading day, regardless of state (a
      released/rejected attempt still used up one of today's entry
      attempts).
    - active_position_count: count of RESERVED/SUBMISSION_UNKNOWN/
      COMMITTED-with-open-position reservations -- genuinely "concurrently
      open or in flight right now", NOT day-scoped.
    """
    now = now or datetime.now(timezone.utc)
    day_start = trading_day_start_utc(now).isoformat()

    today_entry_count = conn.execute(
        "SELECT COUNT(*) AS n FROM live_entry_reservations WHERE created_at >= ?",
        (day_start,),
    ).fetchone()["n"]

    pending_buy_reservations_krw = conn.execute(
        "SELECT COALESCE(SUM(notional_krw), 0) AS total FROM live_entry_reservations WHERE state = ?",
        (STATE_RESERVED,),
    ).fetchone()["total"]

    unknown_submission_reservations_krw = conn.execute(
        "SELECT COALESCE(SUM(notional_krw), 0) AS total FROM live_entry_reservations WHERE state = ?",
        (STATE_SUBMISSION_UNKNOWN,),
    ).fetchone()["total"]

    committed_rows = conn.execute(
        "SELECT notional_krw, position_id FROM live_entry_reservations WHERE state = ?",
        (STATE_COMMITTED,),
    ).fetchall()
    current_open_position_cost_krw = 0.0
    open_committed_count = 0
    for row in committed_rows:
        if row["position_id"]:
            pos = conn.execute(
                "SELECT state FROM positions WHERE position_id = ?", (row["position_id"],)
            ).fetchone()
            if pos is not None and pos["state"] in _TERMINAL_POSITION_STATES():
                continue  # funded position already closed -- no longer open exposure
        current_open_position_cost_krw += row["notional_krw"]
        open_committed_count += 1

    reserved_and_unknown_count = conn.execute(
        "SELECT COUNT(*) AS n FROM live_entry_reservations WHERE state IN (?, ?)",
        (STATE_RESERVED, STATE_SUBMISSION_UNKNOWN),
    ).fetchone()["n"]
    active_position_count = reserved_and_unknown_count + open_committed_count

    return LiveRiskSnapshot(
        pending_buy_reservations_krw=pending_buy_reservations_krw,
        unknown_submission_reservations_krw=unknown_submission_reservations_krw,
        current_open_position_cost_krw=current_open_position_cost_krw,
        active_position_count=active_position_count,
        today_entry_count=today_entry_count,
    )


def _TERMINAL_POSITION_STATES():
    from positions import states
    return states.TERMINAL_STATES
