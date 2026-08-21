"""S2's open positions. Reads and writes; decides nothing.

Modelled on `s1_live.position_store`, and on the same guarantees rather
than the same columns:

    open_position()      refuses without an ACTUAL fill price
    observe()            ratchets the volume peak UP only
    latch_pending_exit() first reason wins
    mark_exit_submitted() one-way, so a second SELL cannot be decided
    close_position()     records how it ended

The peak ratchet is the one that carries real weight. A peak that
followed volume down would pin the decay ratio at 1.0 forever and S2
would never exit on the condition it is built around -- and the failure
would be invisible, because "no exit signal" and "the signal can never
fire" produce identical logs.

Entry price is the broker's average fill
----------------------------------------
`open_position()` will not accept an intended limit price. The
catastrophic cap is measured from entry, so an intended-price entry puts
the stop wrong by exactly the slippage -- in the direction that makes it
looser, every time.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TABLE = "s2_positions"
STRATEGY_ID = "S2_VOLUME_ACCUMULATION_V1"

OPEN = "OPEN"
EXIT_PENDING = "EXIT_PENDING"
EXIT_SUBMITTED = "EXIT_SUBMITTED"
CLOSED = "CLOSED"

#: Statuses that still represent something held at the broker.
LIVE_STATUSES = (OPEN, EXIT_PENDING, EXIT_SUBMITTED)


class S2PositionError(Exception):
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


def open_position(conn, *, symbol, quantity, average_fill_price, venue=None,
                  entry_session=None, entry_order_id=None,
                  entry_volume_multiple=None, baseline_volume=None,
                  effective_stop=None, hard_stop=None, now=None,
                  position_id=None) -> str:
    """Record a filled S2 entry. Returns the position id.

    Refuses an unusable fill price rather than storing one: every stop
    this position will ever have is measured from it, and a position
    whose entry is wrong is worse than no position, because it looks
    correct.
    """
    price = _finite(average_fill_price)
    if price is None or price <= 0:
        raise S2PositionError(
            f"refusing to open {symbol}: average fill price {average_fill_price!r} "
            "is not a usable price; every stop is measured from it")
    try:
        qty = int(quantity)
    except (TypeError, ValueError):
        qty = 0
    if qty < 1:
        raise S2PositionError(f"refusing to open {symbol}: quantity {quantity!r}")

    identifier = position_id or f"s2pos_{uuid.uuid4().hex[:16]}"
    stamp = _now(now)
    conn.execute(
        f"""INSERT INTO {TABLE} (
                position_id, strategy_id, symbol, venue, quantity,
                entry_price, entry_time, entry_session, entry_order_id,
                entry_volume_multiple, baseline_volume,
                peak_volume_multiple, price_at_volume_peak,
                effective_stop, hard_stop, status, exit_submitted,
                created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
        (identifier, STRATEGY_ID, str(symbol).upper(), venue, qty, price,
         stamp, entry_session, entry_order_id,
         _finite(entry_volume_multiple), _finite(baseline_volume),
         # The entry multiple IS the first peak. Leaving it NULL would
         # make the first observation set a peak from a later, lower
         # reading and call the position undecayed forever.
         _finite(entry_volume_multiple), None,
         _finite(effective_stop), _finite(hard_stop), OPEN, stamp, stamp))
    conn.commit()
    logger.info("S2 position opened: %s %s qty=%d @ %.4f", identifier,
                symbol, qty, price)
    return identifier


def load_live(conn) -> List[Tuple[str, Dict[str, Any]]]:
    """Every position still held, EXIT_PENDING first.

    Pending exits lead for the reason S1's store orders them that way: a
    position whose exit was decided but not yet submitted is the one a
    tick must not run out of time before reaching.
    """
    rows = conn.execute(
        f"""SELECT * FROM {TABLE}
            WHERE status IN ({','.join('?' * len(LIVE_STATUSES))})
            ORDER BY CASE status WHEN 'EXIT_PENDING' THEN 0 ELSE 1 END,
                     created_at""",
        LIVE_STATUSES).fetchall()
    return [(row["position_id"], dict(row)) for row in rows]


def load_by_symbol(conn, symbol) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        f"""SELECT * FROM {TABLE} WHERE symbol = ? AND status != ?""",
        (str(symbol).upper(), CLOSED)).fetchone()
    return dict(row) if row else None


def open_count(conn) -> int:
    """How many S2 positions are held. For the position limit."""
    row = conn.execute(
        f"""SELECT COUNT(*) AS n FROM {TABLE} WHERE status != ?""",
        (CLOSED,)).fetchone()
    return int(row["n"] if row else 0)


def observe(conn, position_id, *, volume_multiple=None, price=None,
            decayed=None, now=None) -> None:
    """Update the tracked volume history. Ratchets the peak UP only.

    A lower reading is decay, not a new peak. Letting the peak follow
    volume down would hold the decay ratio at 1.0 forever, so S2 would
    never exit on the condition it exists for -- and nothing would look
    wrong, because a signal that can never fire and a signal that has not
    fired produce the same empty log.
    """
    row = conn.execute(
        f"SELECT peak_volume_multiple, decay_since FROM {TABLE} "
        "WHERE position_id = ?", (position_id,)).fetchone()
    if row is None:
        return
    stamp = _now(now)
    multiple = _finite(volume_multiple)
    peak = _finite(row["peak_volume_multiple"])

    if multiple is not None and (peak is None or multiple > peak):
        conn.execute(
            f"UPDATE {TABLE} SET peak_volume_multiple = ?, "
            "price_at_volume_peak = COALESCE(?, price_at_volume_peak), "
            "updated_at = ? WHERE position_id = ?",
            (multiple, _finite(price), stamp, position_id))

    if decayed is True and row["decay_since"] is None:
        conn.execute(f"UPDATE {TABLE} SET decay_since = ?, updated_at = ? "
                     "WHERE position_id = ?", (stamp, stamp, position_id))
    elif decayed is False and row["decay_since"] is not None:
        # Volume recovered, so the window it was timing stopped being
        # true and restarts rather than continuing.
        conn.execute(f"UPDATE {TABLE} SET decay_since = NULL, updated_at = ? "
                     "WHERE position_id = ?", (stamp, position_id))
    conn.commit()


def latch_pending_exit(conn, position_id, reason, *, now=None) -> bool:
    """Record that an exit was decided. The FIRST reason wins.

    A later, different reason does not overwrite it: the reason the
    position is leaving is the one that fired, and letting a subsequent
    tick relabel it would make the exit study measure whichever condition
    happened to be evaluated last.
    """
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
    """One-way. Once submitted, `decide()` returns HOLD forever after.

    This is the first of the three duplicate-SELL defences, and the only
    one that survives a process restart.
    """
    stamp = _now(now)
    changed = conn.execute(
        f"""UPDATE {TABLE}
            SET status = ?, exit_submitted = 1, exit_reason = ?, updated_at = ?
            WHERE position_id = ? AND exit_submitted = 0""",
        (EXIT_SUBMITTED, reason, stamp, position_id)).rowcount
    conn.commit()
    return bool(changed)


def close_position(conn, position_id, *, reason=None, now=None) -> bool:
    stamp = _now(now)
    changed = conn.execute(
        f"""UPDATE {TABLE}
            SET status = ?, exit_reason = COALESCE(?, exit_reason),
                closed_at = ?, updated_at = ?
            WHERE position_id = ? AND status != ?""",
        (CLOSED, reason, stamp, stamp, position_id, CLOSED)).rowcount
    conn.commit()
    return bool(changed)


def to_state(row: Dict[str, Any]):
    """A stored row as the pure `S2PositionState` the policy takes."""
    from s2_live.exit_policy import S2PositionState

    decay_since = row.get("decay_since")
    if isinstance(decay_since, str) and decay_since:
        try:
            decay_since = datetime.fromisoformat(decay_since)
        except ValueError:
            decay_since = None
    return S2PositionState(
        symbol=row["symbol"],
        entry_price=row["entry_price"],
        entry_volume_multiple=_finite(row.get("entry_volume_multiple")),
        baseline_volume=_finite(row.get("baseline_volume")),
        peak_volume_multiple=_finite(row.get("peak_volume_multiple")),
        price_at_volume_peak=_finite(row.get("price_at_volume_peak")),
        decay_since=decay_since or None,
        exit_submitted=bool(row.get("exit_submitted")),
    )


def holdings(conn) -> List[Tuple[str, str, int]]:
    """(symbol, venue, quantity) for every live S2 position.

    The shape reconciliation compares against the broker. Venue travels
    because KIS answers a NASD request with NYSE rows, so the symbol
    alone is not an identity -- the correction TX needed.
    """
    rows = conn.execute(
        f"""SELECT symbol, venue, quantity FROM {TABLE} WHERE status != ?""",
        (CLOSED,)).fetchall()
    return [(r["symbol"], r["venue"], int(r["quantity"])) for r in rows]
