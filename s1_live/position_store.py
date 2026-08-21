"""Durable S1 position state -- the half of the exit policy that must
survive a restart.

`s1_live/exit_policy.py` is a pure function. That purity is only safe if
the state it reads is faithful, because three of its inputs cannot be
recovered from the market:

    protective_floor_r   ratchets and never falls
    peak_r               what the floor is derived from
    sessions_held        what the time exit counts

A restart that recomputed these from the current price would silently
hand back every protective floor the position had earned. So they are
written here on every change, and read back on every tick.

What this module deliberately does NOT do
-----------------------------------------
It places no orders and imports nothing from `execution/`. It is a
record of what is true, and `s1_live/exit_runtime.py` is the only thing
that turns a record into an order. Keeping the two apart is what lets
the persistence tests run without a broker at all.
"""

import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from s1_live.exit_policy import S1PositionState

logger = logging.getLogger(__name__)

STATUS_OPEN = "OPEN"
STATUS_EXIT_PENDING = "EXIT_PENDING"
STATUS_EXIT_SUBMITTED = "EXIT_SUBMITTED"
STATUS_CLOSED = "CLOSED"

#: Statuses a position is still ours to manage in.
LIVE_STATUSES = (STATUS_OPEN, STATUS_EXIT_PENDING, STATUS_EXIT_SUBMITTED)


class S1PositionStoreError(Exception):
    """A position could not be durably recorded. Callers must fail closed:
    an unrecorded position is one nothing will ever exit."""


class DuplicateS1PositionError(S1PositionStoreError):
    """An open position already exists for this symbol."""


def _now_iso(now=None):
    if now is not None:
        return now.isoformat() if hasattr(now, "isoformat") else str(now)
    from state_store.db import now_iso
    return now_iso()


def _require_positive(name: str, value) -> float:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        raise S1PositionStoreError(f"{name} must be a number, got {value!r}")
    number = float(value)
    if number <= 0 or number != number or number in (float("inf"), float("-inf")):
        raise S1PositionStoreError(f"{name} must be positive and finite, got {value!r}")
    return number


def open_position(conn, *, symbol, strategy_id, signal_id, entry_price, quantity,
                  entry_order_id=None, now=None) -> str:
    """Record a filled S1 entry. `entry_price` MUST be the broker's actual
    average fill price -- see the schema comment. Returns position_id.

    Raises DuplicateS1PositionError if this symbol is already held, which
    the unique partial index enforces at the storage layer rather than
    trusting the gate above it.
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise S1PositionStoreError(f"symbol must be a non-empty string, got {symbol!r}")
    price = _require_positive("entry_price", entry_price)
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise S1PositionStoreError(
            f"quantity must be a positive int (fractional positions are never "
            f"allowed -- spec §19/§30), got {quantity!r}")

    position_id = f"s1pos_{uuid.uuid4().hex[:16]}"
    stamp = _now_iso(now)
    try:
        conn.execute(
            "INSERT INTO s1_positions "
            "(position_id, symbol, strategy_id, signal_id, entry_price, quantity, "
            " entry_order_id, sessions_held, peak_r, exit_submitted, status, "
            " opened_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0.0, 0, ?, ?, ?)",
            (position_id, symbol.strip().upper(), strategy_id, signal_id, price,
             quantity, entry_order_id, STATUS_OPEN, stamp, stamp),
        )
        conn.commit()
    except Exception as exc:  # sqlite3.IntegrityError and friends
        if "idx_s1_positions_open_symbol" in str(exc) or "UNIQUE" in str(exc).upper():
            raise DuplicateS1PositionError(
                f"an open S1 position already exists for {symbol!r}") from exc
        raise S1PositionStoreError(f"could not record S1 position for {symbol!r}: {exc}") from exc
    logger.info("S1 position opened: %s %s qty=%d @ %.4f (actual fill)",
                position_id, symbol, quantity, price)
    return position_id


def _row_to_state(row) -> S1PositionState:
    return S1PositionState(
        symbol=row["symbol"],
        entry_price=row["entry_price"],
        sessions_held=row["sessions_held"],
        protective_floor_r=row["protective_floor_r"],
        peak_r=row["peak_r"],
        exit_submitted=bool(row["exit_submitted"]),
    )


def get_row(conn, position_id) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM s1_positions WHERE position_id = ?", (position_id,)).fetchone()
    return dict(row) if row else None


def load_state(conn, position_id) -> Optional[S1PositionState]:
    """The exact S1PositionState the policy ran on before the restart."""
    row = get_row(conn, position_id)
    return _row_to_state(row) if row else None


def load_live(conn) -> List[Tuple[str, S1PositionState, Dict[str, Any]]]:
    """Every position still under management, EXIT_PENDING first.

    Ordering is not cosmetic: spec §9 requires a latched exit to be the
    first order placed in the next orderable session, so the caller
    iterating this list in order satisfies that without needing to know
    about it.
    """
    placeholders = ",".join("?" for _ in LIVE_STATUSES)
    rows = conn.execute(
        f"SELECT * FROM s1_positions WHERE status IN ({placeholders}) "
        "ORDER BY CASE status WHEN 'EXIT_PENDING' THEN 0 ELSE 1 END, opened_at ASC",
        LIVE_STATUSES,
    ).fetchall()
    return [(r["position_id"], _row_to_state(r), dict(r)) for r in rows]


def open_symbols(conn) -> set:
    """Symbols we already hold -- the entry gate's "no position" check."""
    placeholders = ",".join("?" for _ in LIVE_STATUSES)
    rows = conn.execute(
        f"SELECT symbol FROM s1_positions WHERE status IN ({placeholders})",
        LIVE_STATUSES).fetchall()
    return {r["symbol"] for r in rows}


def live_count(conn) -> int:
    placeholders = ",".join("?" for _ in LIVE_STATUSES)
    return conn.execute(
        f"SELECT COUNT(*) AS n FROM s1_positions WHERE status IN ({placeholders})",
        LIVE_STATUSES).fetchone()["n"]


def _update(conn, position_id, fields: Dict[str, Any], now=None):
    fields = dict(fields)
    fields["updated_at"] = _now_iso(now)
    assignments = ", ".join(f"{k} = ?" for k in fields)
    cursor = conn.execute(
        f"UPDATE s1_positions SET {assignments} WHERE position_id = ?",
        (*fields.values(), position_id))
    if cursor.rowcount == 0:
        raise S1PositionStoreError(f"unknown S1 position: {position_id!r}")
    conn.commit()


def apply_ratchet(conn, position_id, *, new_protective_floor_r, peak_r, now=None):
    """Raise the protective floor. Refuses to lower it.

    The guard is here rather than only in the policy because this is the
    layer a bug, a replay or a stale caller would come through, and a
    floor that can go down is not a floor.
    """
    row = get_row(conn, position_id)
    if row is None:
        raise S1PositionStoreError(f"unknown S1 position: {position_id!r}")
    stored = row["protective_floor_r"]
    if stored is not None and new_protective_floor_r is not None \
            and new_protective_floor_r < stored:
        raise S1PositionStoreError(
            f"refusing to lower the protective floor for {position_id!r}: "
            f"stored {stored!r} -> requested {new_protective_floor_r!r}")
    _update(conn, position_id, {
        "protective_floor_r": new_protective_floor_r,
        "peak_r": max(float(peak_r or 0.0), float(row["peak_r"] or 0.0)),
    }, now=now)


def record_peak(conn, position_id, peak_r, now=None):
    """Persist a new high-water R. Never lowers the stored peak."""
    row = get_row(conn, position_id)
    if row is None:
        raise S1PositionStoreError(f"unknown S1 position: {position_id!r}")
    best = max(float(peak_r or 0.0), float(row["peak_r"] or 0.0))
    if best != float(row["peak_r"] or 0.0):
        _update(conn, position_id, {"peak_r": best}, now=now)


def advance_session(conn, position_id, session_date, now=None) -> int:
    """Count one trading session, at most once per calendar session.

    Idempotent on `session_date` because the exit runtime evaluates many
    times a session (spec §4: premarket, regular and after-hours). A
    naive increment per tick would trip the 10-session time exit within
    a single day.
    """
    row = get_row(conn, position_id)
    if row is None:
        raise S1PositionStoreError(f"unknown S1 position: {position_id!r}")
    session_date = str(session_date)
    if row["last_session_date"] == session_date:
        return row["sessions_held"]
    held = int(row["sessions_held"]) + 1
    _update(conn, position_id, {
        "sessions_held": held, "last_session_date": session_date}, now=now)
    return held


def latch_pending_exit(conn, position_id, reason, now=None):
    """Spec §9: an exit triggered in a session the broker will not accept
    an order in is latched, never discarded.

    The FIRST reason wins. A later tick that would have produced a
    different reason does not overwrite it, because the position is
    already leaving -- re-deciding would let a HOLD tick erase a
    triggered stop.
    """
    row = get_row(conn, position_id)
    if row is None:
        raise S1PositionStoreError(f"unknown S1 position: {position_id!r}")
    if row["pending_exit_reason"]:
        return
    _update(conn, position_id, {
        "status": STATUS_EXIT_PENDING,
        "pending_exit_reason": reason,
        "pending_exit_since": _now_iso(now),
    }, now=now)
    logger.warning("S1 exit latched as EXIT_PENDING for %s (%s) -- the current "
                   "session does not accept orders; it will be submitted first "
                   "in the next orderable session", position_id, reason)


def mark_exit_submitted(conn, position_id, reason, now=None):
    """The policy-layer half of duplicate-SELL prevention. Once set,
    `decide()` returns HOLD for this position forever after."""
    _update(conn, position_id, {
        "exit_submitted": 1, "status": STATUS_EXIT_SUBMITTED, "exit_reason": reason,
    }, now=now)


def close_position(conn, position_id, exit_reason=None, now=None):
    stamp = _now_iso(now)
    _update(conn, position_id, {
        "status": STATUS_CLOSED, "exit_submitted": 1,
        "exit_reason": exit_reason, "closed_at": stamp,
    }, now=now)
    logger.info("S1 position closed: %s (%s)", position_id, exit_reason)


def holdings(conn):
    """(symbol, venue, quantity) for every live S1 position.

    The shape reconciliation attribution reads. Added when S2 arrived and
    attribution reported "S1: none" while TX was plainly held -- the
    lookup is `hasattr(module, "holdings")`, so a missing function is
    silently an empty answer rather than an error. An attribution that
    can be wrong without saying so is worse than none.

    `s1_positions` has no venue column, so venue is None here. That is
    honest rather than convenient: the account store records no venue
    either, and inventing one would put a guess where the comparison
    expects a fact.
    """
    rows = conn.execute(
        "SELECT symbol, quantity FROM s1_positions WHERE status != 'CLOSED'"
    ).fetchall()
    return [(row["symbol"], None, int(row["quantity"])) for row in rows]
