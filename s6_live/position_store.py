"""S6's positions. Reads and writes; decides nothing.

SUBMITTED is a state, not a gap
-------------------------------
S1 and S2 record a position only once a fill arrives, so an order that
was sent and never confirmed exists nowhere: reconciliation sees a broker
position it cannot attribute, and the strategy sees nothing at all. S6
records the submission first. An ambiguous BUY is then a row somebody can
find, which is the difference between a recoverable state and a lost one.

A SUBMITTED row has no entry price by construction. `open_from_fill()` is
the only thing that sets one, and the schema's CHECK refuses an OPEN row
without it -- the structural stop is compared against the entry, so an
intended-price entry would misplace every later decision by the slippage.

The unique index covers SUBMITTED too. A second BUY into a name whose
first order is still unconfirmed is exactly the duplicate an ambiguous
submission invites, and the storage layer refuses it rather than relying
on a gate that may itself be mid-restart.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TABLE = "s6_positions"
STRATEGY_ID = "S6_ORB_BREAKOUT_V1"

SUBMITTED = "SUBMITTED"
OPEN = "OPEN"
EXIT_PENDING = "EXIT_PENDING"
EXIT_SUBMITTED = "EXIT_SUBMITTED"
CLOSED = "CLOSED"

#: Everything still live at the broker, submission included.
LIVE_STATUSES = (SUBMITTED, OPEN, EXIT_PENDING, EXIT_SUBMITTED)
#: Statuses that represent shares actually held.
HELD_STATUSES = (OPEN, EXIT_PENDING, EXIT_SUBMITTED)


class S6PositionError(Exception):
    """A write that would have stored something untrue."""


def _now(now=None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat()


def _finite(value) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number or number in (
        float("inf"), float("-inf")) else number


def record_submission(conn, *, symbol, variant=None, entry_session=None,
                      client_order_id=None, range_minutes=None,
                      range_high=None, range_low=None, entry_vwap=None,
                      entry_ema9=None, entry_ema21=None,
                      entry_volume_expansion=None, now=None,
                      position_id=None) -> str:
    """Record that a BUY was SENT. Not that it filled.

    Written before the broker answers, so an ambiguous submission leaves
    a row rather than nothing. The position holds no shares yet and says
    so: no entry price, no quantity.
    """
    identifier = position_id or f"s6pos_{uuid.uuid4().hex[:16]}"
    stamp = _now(now)
    conn.execute(
        f"""INSERT INTO {TABLE} (
                position_id, strategy_id, variant, symbol, entry_session,
                client_order_id, range_minutes, range_high, range_low,
                entry_vwap, entry_ema9, entry_ema21, entry_volume_expansion,
                peak_volume_expansion, status, exit_submitted,
                submitted_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
        (identifier, STRATEGY_ID, variant, str(symbol).upper(), entry_session,
         client_order_id, range_minutes, _finite(range_high),
         _finite(range_low), _finite(entry_vwap), _finite(entry_ema9),
         _finite(entry_ema21), _finite(entry_volume_expansion),
         # The entry expansion IS the first peak; leaving it NULL would
         # let a later, lower reading set the peak and call the position
         # undecayed forever.
         _finite(entry_volume_expansion), SUBMITTED, stamp, stamp, stamp))
    conn.commit()
    logger.info("S6 submission recorded: %s %s", identifier, symbol)
    return identifier


def open_from_fill(conn, position_id, *, quantity, average_fill_price,
                   venue=None, entry_order_id=None, now=None) -> bool:
    """Promote SUBMITTED to OPEN using the broker's ACTUAL fill.

    Refuses an unusable price rather than storing one. Every later
    decision -- the structural stop above all -- is measured from this,
    and a position whose entry is wrong is worse than no position because
    it looks correct.
    """
    price = _finite(average_fill_price)
    if price is None or price <= 0:
        raise S6PositionError(
            f"refusing to open {position_id}: fill price "
            f"{average_fill_price!r} is not usable; the structural stop is "
            "compared against it")
    try:
        qty = int(quantity)
    except (TypeError, ValueError):
        qty = 0
    if qty < 1:
        raise S6PositionError(
            f"refusing to open {position_id}: quantity {quantity!r}")

    stamp = _now(now)
    changed = conn.execute(
        f"""UPDATE {TABLE}
            SET status = ?, quantity = ?, entry_price = ?, entry_time = ?,
                venue = COALESCE(?, venue),
                entry_order_id = COALESCE(?, entry_order_id),
                peak_price = ?, trough_price = ?, updated_at = ?
            WHERE position_id = ? AND status = ?""",
        # The entry IS the first peak AND the first trough. Leaving either
        # NULL would let the first observation set it unopposed, so a
        # position that only ever went up would report having gone
        # against us, and one that only fell would report no give-back.
        (OPEN, qty, price, stamp, venue, entry_order_id, price, price, stamp,
         position_id, SUBMITTED)).rowcount
    conn.commit()
    if changed:
        logger.info("S6 position opened: %s qty=%d @ %.4f", position_id,
                    qty, price)
    return bool(changed)


def apply_fill(conn, position_id, *, filled_quantity, average_fill_price,
               venue=None, entry_order_id=None, now=None) -> bool:
    """Idempotent fill application, including partials.

    A partial fill opens the position at the quantity actually filled --
    a position of one share is a real position. A LATER fill for the same
    order raises the quantity and re-averages the price; applying the
    same fill twice does not, because the cumulative filled quantity is
    what is compared, not the delta.
    """
    row = load(conn, position_id)
    if row is None:
        return False
    try:
        cumulative = int(filled_quantity)
    except (TypeError, ValueError):
        return False
    if cumulative < 1:
        return False

    if row["status"] == SUBMITTED:
        return open_from_fill(conn, position_id, quantity=cumulative,
                              average_fill_price=average_fill_price,
                              venue=venue, entry_order_id=entry_order_id,
                              now=now)

    if row["status"] not in HELD_STATUSES:
        return False
    held = int(row["quantity"] or 0)
    if cumulative <= held:
        # Already applied. Re-applying would double a position that the
        # broker reports once -- the failure a retried sync invites.
        return False
    price = _finite(average_fill_price)
    if price is None or price <= 0:
        return False
    conn.execute(
        f"""UPDATE {TABLE} SET quantity = ?, entry_price = ?, updated_at = ?
            WHERE position_id = ?""",
        (cumulative, price, _now(now), position_id))
    conn.commit()
    return True


def load(conn, position_id) -> Optional[Dict[str, Any]]:
    row = conn.execute(f"SELECT * FROM {TABLE} WHERE position_id = ?",
                       (position_id,)).fetchone()
    return dict(row) if row else None


def load_by_symbol(conn, symbol) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        f"SELECT * FROM {TABLE} WHERE symbol = ? AND status != ?",
        (str(symbol).upper(), CLOSED)).fetchone()
    return dict(row) if row else None


def load_live(conn) -> List[Tuple[str, Dict[str, Any]]]:
    """Positions holding shares, EXIT_PENDING first.

    SUBMITTED rows are excluded: there is nothing to exit from an order
    that has not filled, and evaluating one would compare a stop against
    an entry price that does not exist yet.
    """
    rows = conn.execute(
        f"""SELECT * FROM {TABLE}
            WHERE status IN ({','.join('?' * len(HELD_STATUSES))})
            ORDER BY CASE status WHEN 'EXIT_PENDING' THEN 0 ELSE 1 END,
                     created_at""", HELD_STATUSES).fetchall()
    return [(r["position_id"], dict(r)) for r in rows]


def load_unconfirmed(conn) -> List[Dict[str, Any]]:
    """SUBMITTED rows -- orders sent whose outcome is unknown.

    What a restart reads to find out what it may have been in the middle
    of. Without this the order is invisible and a second BUY looks safe.
    """
    rows = conn.execute(f"SELECT * FROM {TABLE} WHERE status = ?",
                        (SUBMITTED,)).fetchall()
    return [dict(r) for r in rows]


def open_count(conn) -> int:
    """Positions counting against the limit -- SUBMITTED included.

    An order in flight may become a position at any moment, so excluding
    it would let a second entry through in exactly the window where the
    first is unconfirmed.
    """
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM {TABLE} WHERE status != ?",
        (CLOSED,)).fetchone()
    return int(row["n"] if row else 0)


def observe(conn, position_id, *, price=None, volume_expansion=None,
            now=None) -> None:
    """Ratchet the peaks UP only, and the trough DOWN only.

    A lower reading is decay, not a new peak. A peak that followed the
    market down would hold the decay ratio at 1.0 forever, and the
    give-back check against `peak_price` would never fire -- both failing
    silently, because a signal that cannot fire looks like one that has
    not.

    `trough_price` is the same ratchet in the other direction and is read
    by NOTHING that decides: no exit condition, no score, no threshold
    consults it. It exists because MAE -- how far a trade went against us
    before it resolved -- cannot be recovered from a closed row, and it
    is the figure that says whether the structural stop was ever actually
    threatened.
    """
    row = load(conn, position_id)
    if row is None:
        return
    stamp = _now(now)
    updates, params = [], []
    new_price = _finite(price)
    if new_price is not None:
        peak = _finite(row.get("peak_price"))
        if peak is None or new_price > peak:
            updates.append("peak_price = ?")
            params.append(new_price)
        trough = _finite(row.get("trough_price"))
        if trough is None or new_price < trough:
            updates.append("trough_price = ?")
            params.append(new_price)
    new_exp = _finite(volume_expansion)
    if new_exp is not None:
        peak = _finite(row.get("peak_volume_expansion"))
        if peak is None or new_exp > peak:
            updates.append("peak_volume_expansion = ?")
            params.append(new_exp)
    if not updates:
        return
    conn.execute(f"UPDATE {TABLE} SET {', '.join(updates)}, updated_at = ? "
                 "WHERE position_id = ?", (*params, stamp, position_id))
    conn.commit()


def latch_pending_exit(conn, position_id, reason, *, now=None) -> bool:
    """The FIRST reason wins. A later tick does not relabel the exit."""
    stamp = _now(now)
    changed = conn.execute(
        f"""UPDATE {TABLE}
            SET status = ?, pending_exit_reason = ?, pending_exit_since = ?,
                updated_at = ?
            WHERE position_id = ? AND status = ?""",
        (EXIT_PENDING, reason, stamp, stamp, position_id, OPEN)).rowcount
    conn.commit()
    return bool(changed)


def mark_exit_submitted(conn, position_id, reason, *, now=None) -> bool:
    """One-way. The duplicate-SELL defence that survives a restart."""
    stamp = _now(now)
    changed = conn.execute(
        f"""UPDATE {TABLE}
            SET status = ?, exit_submitted = 1, exit_reason = ?, updated_at = ?
            WHERE position_id = ? AND exit_submitted = 0""",
        (EXIT_SUBMITTED, reason, stamp, position_id)).rowcount
    conn.commit()
    return bool(changed)


def close_position(conn, position_id, *, reason=None, exit_price=None,
                   now=None) -> bool:
    """Close a position, recording the SELL's actual average fill.

    `exit_price` is COALESCEd rather than overwritten so a second close
    cannot replace a real fill with a None. It is the same discipline
    `entry_price` gets on the buy side: realised P&L measured against an
    intended price is not realised P&L.
    """
    stamp = _now(now)
    changed = conn.execute(
        f"""UPDATE {TABLE}
            SET status = ?, exit_reason = COALESCE(?, exit_reason),
                exit_price = COALESCE(?, exit_price),
                closed_at = ?, updated_at = ?
            WHERE position_id = ? AND status != ?""",
        (CLOSED, reason, _finite(exit_price), stamp, stamp, position_id,
         CLOSED)).rowcount
    conn.commit()
    return bool(changed)


def abandon_submission(conn, position_id, *, reason, now=None) -> bool:
    """Close a SUBMITTED row that reconciliation proved never filled.

    Separate from `close_position` so the two cannot be confused in a
    log: one ends a position, this one records that there never was one.
    """
    stamp = _now(now)
    changed = conn.execute(
        f"""UPDATE {TABLE}
            SET status = ?, exit_reason = ?, closed_at = ?, updated_at = ?
            WHERE position_id = ? AND status = ?""",
        (CLOSED, reason, stamp, stamp, position_id, SUBMITTED)).rowcount
    conn.commit()
    return bool(changed)


def to_state(row: Dict[str, Any]):
    """A stored row as the pure state the policy takes."""
    from s6_live.exit_policy import S6PositionState

    return S6PositionState(
        symbol=row["symbol"],
        entry_price=_finite(row.get("entry_price")) or 0.0,
        variant=row.get("variant"),
        range_high=_finite(row.get("range_high")),
        range_low=_finite(row.get("range_low")),
        entry_volume_expansion=_finite(row.get("entry_volume_expansion")),
        peak_volume_expansion=_finite(row.get("peak_volume_expansion")),
        peak_price=_finite(row.get("peak_price")),
        exit_submitted=bool(row.get("exit_submitted")),
    )


def holdings(conn) -> List[Tuple[str, Optional[str], int]]:
    """(symbol, venue, quantity) for shares actually held.

    SUBMITTED is excluded here even though `open_count` includes it: the
    limit asks "could another position appear", reconciliation asks "what
    do we hold", and an unfilled order is a yes to the first and a no to
    the second.
    """
    rows = conn.execute(
        f"""SELECT symbol, venue, quantity FROM {TABLE}
            WHERE status IN ({','.join('?' * len(HELD_STATUSES))})""",
        HELD_STATUSES).fetchall()
    return [(r["symbol"], r["venue"], int(r["quantity"] or 0)) for r in rows]
