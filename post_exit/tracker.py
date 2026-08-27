"""Open a tracking record when a position closes, for any strategy.

One implementation, three call sites
------------------------------------
Each strategy's `position_store.close_position` calls `on_position_closed`
with its own table name. The row is read back here rather than passed in,
so a store that gains a column does not have to teach this module about
it, and every strategy is described the same way whatever its book looks
like.

Never fatal
-----------
A closed position is a finished trade. If tracking cannot be opened --
the table is missing, the row is unreadable, the clock is wrong -- the
trade is still closed and the caller must not see an error. Every public
function here returns rather than raises.

What is NOT tracked
-------------------
Closures that record something other than a sale: an ownership repair or
an abandoned entry. `execution/reentry_policy.NON_TRADE_EXIT_REASONS` is
the single list, shared with the re-entry block, so the two cannot
disagree about what counts as an exit.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import post_exit_policy
from execution.reentry_policy import NON_TRADE_EXIT_REASONS

logger = logging.getLogger(__name__)


def _now(now=None):
    return now or datetime.now(timezone.utc)


def _as_dt(stamp) -> Optional[datetime]:
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _tracking_end(exit_at: datetime, strategy_id) -> datetime:
    """Calendar-walked trading days, capped by the policy.

    Uses the market calendar rather than `+N days` so a Friday exit is
    not "tracked" across a weekend that has no prices in it.
    """
    from market_hours import is_market_day

    days = post_exit_policy.tracking_days_for(strategy_id)
    cursor = exit_at
    remaining = days
    # A generous but finite walk: 5 trading days can span a long
    # holiday weekend, never 30 calendar days.
    for _ in range(30):
        cursor = cursor + timedelta(days=1)
        if is_market_day(cursor.date()):
            remaining -= 1
            if remaining <= 0:
                break
    return cursor.replace(hour=21, minute=0, second=0, microsecond=0)


def _exit_timing(conn, data, position_id):
    """When the strategy decided, and when the broker would take it.

    DT made the distinction unavoidable: SESSION_EXIT fired at 23:45:10Z
    after the sell route had been unavailable for 1h45m. Recorded as one
    "exit time" that trade reads as a late RULE, which is the opposite of
    what happened. `route_wait_duration_seconds` isolates the part that
    belongs to the venue rather than to the strategy.

    Every field is best-effort: a trade that closed before these were
    recorded still gets its row, with Nones where the fact is unknown.
    """
    signal_at = data.get("pending_exit_since")
    signal_reason = data.get("pending_exit_reason") or data.get("exit_reason")
    submit_at = None
    try:
        row = conn.execute(
            "SELECT created_at FROM exit_intents WHERE position_id = ? "
            "AND state NOT IN ('ABORTED') ORDER BY created_at DESC LIMIT 1",
            (position_id,)).fetchone()
        if row:
            submit_at = row[0] if not hasattr(row, "keys") else row["created_at"]
    except Exception:  # noqa: BLE001
        logger.debug("exit intent timing unreadable for %s", position_id,
                     exc_info=True)
    filled_at = data.get("closed_at")

    def _gap(start, end):
        a, b = _as_dt(start), _as_dt(end)
        return (b - a).total_seconds() if a and b else None

    return {
        "exit_signal_time": signal_at,
        "exit_signal_reason": signal_reason,
        "exit_pending_since": signal_at,
        "sell_submit_time": submit_at,
        "actual_sell_time": filled_at,
        "signal_to_submit_seconds": _gap(signal_at, submit_at),
        "submit_to_fill_seconds": _gap(submit_at, filled_at),
        # The whole wait from decision to fill. When the signal fired
        # while no route existed, this is dominated by the venue.
        "route_wait_duration_seconds": _gap(signal_at, filled_at),
    }


def on_position_closed(conn, *, table, position_id, strategy_id,
                       now=None, note=None, scanner_id=None,
                       strategy_version=None):
    """Open a post-exit tracking row for a position that just closed.

    Returns the tracking_id, or None when nothing was recorded (which
    includes every non-trade closure). Never raises.
    """
    try:
        return _record(conn, table=table, position_id=position_id,
                       strategy_id=strategy_id, now=now, note=note,
                       scanner_id=scanner_id,
                       strategy_version=strategy_version)
    except Exception:  # noqa: BLE001 - research bookkeeping must never
        # fail a trade that has already completed.
        logger.warning("post-exit tracking could not be opened for %s/%s",
                       strategy_id, position_id, exc_info=True)
        return None


def _column_names(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _record(conn, *, table, position_id, strategy_id, now, note,
            scanner_id, strategy_version):
    columns = _column_names(conn, table)
    if not columns:
        return None
    row = conn.execute(
        f"SELECT * FROM {table} WHERE position_id = ?",  # noqa: S608
        (position_id,)).fetchone()
    if row is None:
        return None
    data = {key: row[key] for key in row.keys()} if hasattr(row, "keys") else {}

    exit_reason = data.get("exit_reason")
    if exit_reason in NON_TRADE_EXIT_REASONS:
        # Not a sale. Recording it would put a trade in the strategy's
        # research record that it never made.
        return None

    exit_price = data.get("exit_price")
    entry_price = data.get("entry_price")
    quantity = data.get("quantity")
    if exit_price is None or entry_price is None:
        # Without both, realised P&L and every return below it would be
        # invented. A trade we cannot describe is not tracked.
        logger.info("post-exit tracking skipped for %s: entry/exit price "
                    "missing", position_id)
        return None

    closed_at = data.get("closed_at")
    exit_at = _as_dt(closed_at) or _now(now)
    realized = (float(exit_price) - float(entry_price)) * float(quantity or 0)
    realized_pct = ((float(exit_price) / float(entry_price) - 1.0) * 100.0
                    if entry_price else None)

    current = _now(now)
    stamp = current.isoformat()
    tracking_id = f"pet_{uuid.uuid4().hex[:16]}"
    end_at = _tracking_end(exit_at, strategy_id)

    from market_hours import us_trading_day

    timing = _exit_timing(conn, data, position_id)
    conn.execute(
        "INSERT OR IGNORE INTO post_exit_tracking ("
        "tracking_id, strategy_id, strategy_version, scanner_id, position_id, "
        "symbol, venue, quantity, entry_time, entry_session, entry_price, "
        "exit_time, exit_session, exit_price, exit_reason, realized_pnl, "
        "realized_pnl_pct, trading_day, tracking_started_at, tracking_end_at, "
        "status, note, created_at, updated_at, "
        "exit_signal_time, exit_signal_reason, exit_pending_since, "
        "sell_submit_time, actual_sell_time, signal_to_submit_seconds, "
        "submit_to_fill_seconds, route_wait_duration_seconds) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
        "        ?,?,?,?,?,?,?,?)",
        (tracking_id, strategy_id, strategy_version, scanner_id, position_id,
         str(data.get("symbol") or "").upper(), data.get("venue"), quantity,
         data.get("entry_time") or data.get("opened_at"),
         data.get("entry_session"), entry_price,
         closed_at, data.get("exit_session"), exit_price, exit_reason,
         realized, realized_pct, us_trading_day(exit_at), stamp,
         end_at.isoformat(), post_exit_policy.STATUS_TRACKING, note,
         stamp, stamp,
         timing["exit_signal_time"], timing["exit_signal_reason"],
         timing["exit_pending_since"], timing["sell_submit_time"],
         timing["actual_sell_time"], timing["signal_to_submit_seconds"],
         timing["submit_to_fill_seconds"],
         timing["route_wait_duration_seconds"]))
    conn.commit()
    logger.info("post-exit tracking opened: %s %s %s (%s) until %s",
                tracking_id, strategy_id, data.get("symbol"), exit_reason,
                end_at.isoformat())
    return tracking_id


def annotate(conn, *, position_id, note) -> bool:
    """Attach a research note to a tracking row.

    Used for the one DT trade that predates the same-day re-entry block:
    it is a genuine trade and belongs in the statistics, but it is also
    the regression case, and a reader of the data should be able to see
    that without consulting a commit message. Never fatal.
    """
    try:
        changed = conn.execute(
            "UPDATE post_exit_tracking SET note = ?, updated_at = ? "
            "WHERE position_id = ?",
            (note, datetime.now(timezone.utc).isoformat(), position_id)).rowcount
        conn.commit()
        return bool(changed)
    except Exception:  # noqa: BLE001
        logger.warning("could not annotate post-exit tracking for %s",
                       position_id, exc_info=True)
        return False


def record_broker_fill_time(conn, *, position_id, broker_timestamp) -> bool:
    """Store KIS's own execution time next to the derived one.

    `actual_sell_time` comes from the position row's `closed_at`, which
    is stamped with the tick's clock. This is the broker's fact, kept
    verbatim so a later analysis can prefer it without this code having
    guessed at a date for it. Never fatal.
    """
    if not broker_timestamp:
        return False
    try:
        changed = conn.execute(
            "UPDATE post_exit_tracking SET broker_fill_timestamp = ?, "
            "updated_at = ? WHERE position_id = ?",
            (str(broker_timestamp), datetime.now(timezone.utc).isoformat(),
             position_id)).rowcount
        conn.commit()
        return bool(changed)
    except Exception:  # noqa: BLE001
        logger.warning("could not record the broker fill time for %s",
                       position_id, exc_info=True)
        return False


def due_for_observation(conn, *, now=None):
    """Tracking rows still inside their window."""
    current = _now(now)
    try:
        rows = conn.execute(
            "SELECT * FROM post_exit_tracking WHERE status = ? "
            "ORDER BY tracking_started_at",
            (post_exit_policy.STATUS_TRACKING,)).fetchall()
    except Exception:  # noqa: BLE001
        logger.warning("post-exit tracking rows unreadable", exc_info=True)
        return []
    return [r for r in rows if (_as_dt(r["tracking_end_at"]) or current) >= current]


def complete_expired(conn, *, now=None) -> int:
    """TRACKING -> COMPLETED once the window has passed.

    After this nothing observes the row again -- the point of the window
    being finite (config/post_exit_policy) is that a price far enough
    from the exit stops being evidence about the exit.
    """
    current = _now(now)
    try:
        rows = conn.execute(
            "SELECT tracking_id, tracking_end_at FROM post_exit_tracking "
            "WHERE status = ?", (post_exit_policy.STATUS_TRACKING,)).fetchall()
        expired = [r["tracking_id"] for r in rows
                   if (_as_dt(r["tracking_end_at"]) or current) < current]
        for tracking_id in expired:
            conn.execute(
                "UPDATE post_exit_tracking SET status = ?, updated_at = ? "
                "WHERE tracking_id = ?",
                (post_exit_policy.STATUS_COMPLETED, current.isoformat(),
                 tracking_id))
        conn.commit()
        return len(expired)
    except Exception:  # noqa: BLE001
        logger.warning("post-exit tracking could not be completed",
                       exc_info=True)
        return 0
