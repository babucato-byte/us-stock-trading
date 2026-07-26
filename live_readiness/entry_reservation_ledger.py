"""CODEX-031: durable, authoritative tracking of live-entry budget/count
consumption -- the record live_readiness/order_gateway.py reads from
instead of trusting whatever a caller's LiveEntryContext claims.

Before this module, `LiveEntryContext.max_order_notional_krw`,
`available_cash_krw`, `max_daily_loss_krw`, `max_position_count`,
`current_open_position_count`, `max_daily_entries`, and
`today_entry_count` were all plain fields a caller supplied directly --
nothing independently verified them against reality. A caller (or a bug)
could self-report `available_cash_krw=3_000_000` and the gateway would
approve a 2,997,000 KRW order against a pilot whose real total budget is
30,000 KRW, because nothing tracked how much of that 30,000 KRW ceiling
was already committed or reserved.

This ledger is that tracking: every live-entry attempt that passes the
gateway's other checks (allow-list, symbol match, FX rate) reserves its
notional here BEFORE the broker is ever called (mirroring
state_store/exit_intent_ledger.py's "durable before broker call"
pattern), and the gateway computes "how much of the 30,000 KRW pilot
budget is still available right now" by summing this table's rows for
the current trading day -- never from caller input.

Lifecycle: RESERVED (budget held, broker not yet called) -> COMMITTED
(broker accepted, `position_id` set once positions/lifecycle.py creates
the record) -> RELEASED (either the broker rejected the order, so the
hold never should have counted, or -- checked at snapshot time via a
join against the canonical `positions` table, see build_snapshot() --
the position it funded has since reached a terminal state and its
capital is free again). There is no explicit "close" transition for the
RELEASED state on position-closure: rather than requiring every exit
path in positions/lifecycle.py to remember to call back into this
ledger, build_snapshot() itself excludes a COMMITTED reservation whose
linked position is already terminal, using the SAME canonical SQLite
positions table CODEX-028 made authoritative -- one source of truth,
checked at read time, instead of two sources that must be kept in sync.
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
STATE_RELEASED = "RELEASED"

VALID_STATES = {STATE_RESERVED, STATE_COMMITTED, STATE_RELEASED}

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
    would exceed the pilot ceiling."""
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
    in UTC -- the cutoff build_snapshot() uses to reset the daily entry
    count/budget each day. Using the Eastern calendar date (not UTC's)
    matches every other day-boundary decision in this codebase
    (market_hours.py, EOD cutoffs)."""
    now = now or datetime.now(timezone.utc)
    eastern_now = now.astimezone(EASTERN)
    day_start_eastern = eastern_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start_eastern.astimezone(timezone.utc)


def reserve(conn, symbol, notional_krw, *, commit=True):
    """Durably reserve `notional_krw` of the pilot budget for a live
    entry attempt, BEFORE the broker is ever called. Returns the new
    reservation_id. Caller must hold reservation_lock() across the
    read-snapshot-then-reserve span."""
    if notional_krw <= 0:
        raise EntryReservationError(f"notional_krw must be positive, got {notional_krw!r}")
    reservation_id = f"liveentry_{uuid.uuid4().hex[:16]}"
    now = _now_iso()
    conn.execute(
        "INSERT INTO live_entry_reservations "
        "(reservation_id, symbol, notional_krw, state, position_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, NULL, ?, ?)",
        (reservation_id, symbol, notional_krw, STATE_RESERVED, now, now),
    )
    if commit:
        conn.commit()
    return reservation_id


def mark_committed(conn, reservation_id, *, position_id=None, commit=True):
    """The broker accepted the order. `position_id`, if already known,
    links this reservation to the position it funded so build_snapshot()
    can later tell whether that position has since closed."""
    conn.execute(
        "UPDATE live_entry_reservations SET state = ?, position_id = ?, updated_at = ? "
        "WHERE reservation_id = ?",
        (STATE_COMMITTED, position_id, _now_iso(), reservation_id),
    )
    if commit:
        conn.commit()


def mark_released(conn, reservation_id, *, commit=True):
    """The broker rejected the order (or the reservation was abandoned
    before ever reaching the broker) -- release the budget hold
    immediately rather than leaving it counted until the daily reset."""
    conn.execute(
        "UPDATE live_entry_reservations SET state = ?, updated_at = ? WHERE reservation_id = ?",
        (STATE_RELEASED, _now_iso(), reservation_id),
    )
    if commit:
        conn.commit()


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


class LiveRiskSnapshot:
    def __init__(self, active_notional_krw, active_position_count, today_entry_count):
        self.active_notional_krw = active_notional_krw
        self.active_position_count = active_position_count
        self.today_entry_count = today_entry_count


def build_snapshot(conn, *, now=None):
    """Compute the authoritative "how much of the pilot budget/today's
    entry count/currently-open position count is already spoken for"
    snapshot, entirely from durable state -- never from caller input.
    Must be called while holding reservation_lock() for the result to be
    trustworthy at the moment a new reservation is about to be made.

    Three genuinely different concepts, each scoped differently:

    - active_notional_krw: CUMULATIVE sum of every non-RELEASED
      reservation ever made (not day-scoped). PILOT_TOTAL_BUDGET_KRW is
      a fixed lifetime allocation for the pilot ("30,000원은 총 테스트
      예산" -- see docs/live_review/LIMITED_LIVE_30K_KRW_PLAYBOOK.md),
      not a daily or per-position rolling limit -- it is never released
      just because a funded position later closed.
    - active_position_count: count of RESERVED/COMMITTED reservations
      whose funded position (if linked via link_position()) is not yet
      terminal -- genuinely "concurrently open right now", NOT day-scoped
      (a position opened yesterday and still open today still counts).
    - today_entry_count: count of reservations created since the start
      of the current US-Eastern trading day, regardless of state (a
      released/rejected attempt still used up one of today's entry
      attempts -- CODEX-031 explicitly requires "당일 entry history",
      not just currently-open positions).
    """
    now = now or datetime.now(timezone.utc)
    day_start = trading_day_start_utc(now).isoformat()

    today_entry_count = conn.execute(
        "SELECT COUNT(*) AS n FROM live_entry_reservations WHERE created_at >= ?",
        (day_start,),
    ).fetchone()["n"]

    active_notional_krw = conn.execute(
        "SELECT COALESCE(SUM(notional_krw), 0) AS total FROM live_entry_reservations WHERE state != ?",
        (STATE_RELEASED,),
    ).fetchone()["total"]

    open_rows = conn.execute(
        "SELECT position_id, state FROM live_entry_reservations WHERE state IN (?, ?)",
        (STATE_RESERVED, STATE_COMMITTED),
    ).fetchall()
    active_position_count = 0
    for row in open_rows:
        if row["state"] == STATE_COMMITTED and row["position_id"]:
            pos = conn.execute(
                "SELECT state FROM positions WHERE position_id = ?", (row["position_id"],)
            ).fetchone()
            if pos is not None and pos["state"] in _TERMINAL_POSITION_STATES():
                continue  # funded position already closed -- no longer concurrently open
        active_position_count += 1

    return LiveRiskSnapshot(active_notional_krw, active_position_count, today_entry_count)


def _TERMINAL_POSITION_STATES():
    from positions import states
    return states.TERMINAL_STATES
