"""A symbol this strategy already sold today may not be bought again.

The trade this exists for
-------------------------
DT, 2026-08-26. S6 bought at 50.79, sold at 50.87 on RANGE_REENTRY --
its own exit rule saying the breakout had failed -- and ninety-five
minutes later the same scanner cycle ranked DT fourth again and S6
bought it back at 52.75. Nothing was broken: every gate passed, the
candidate was fresh, the cash was there. The system simply had no
opinion about buying back what it had just decided to leave.

Derived, never stored
---------------------
There is no blacklist table to populate and no daily job to clear one.
The answer is computed from what actually happened: a CLOSED position in
the strategy's own book whose exit landed on today's US trading day. A
list of symbols would have to be written correctly, cleared correctly,
and would be wrong whenever either failed. History is already correct.

The day boundary is the US Eastern trading day (`us_trading_day`), the
same one every other per-day limit uses -- not UTC, not KST. The block
therefore lifts by itself at the next trading day.

What is NOT an exit
-------------------
A CLOSED row is not proof of a sale. Two closures record something else
entirely and must never trigger the block:

  RELEASED_WRONGLY_ATTRIBUTED  an ownership repair -- the strategy never
                               owned the shares, so it never sold them
  BUY_NEVER_FILLED             an entry that was abandoned; there was no
                               position and so no exit

Treating either as "sold today" would block a symbol the strategy has
never actually traded.

The reason alone is not enough
------------------------------
On 2026-08-31 RBLX was blocked from re-entry having never owned a share.
Its BUY was ACCEPTED, filled nothing, hit its TTL and was cancelled, and
the row closed as BUY_FILL_TTL_EXPIRED -- a reason not on the list
above, so the block applied to a trade that never happened.

Adding that reason to the list would be wrong. `entry_timeout` raises
the same reason for a PARTIALLY filled order it cancels
(ACTION_PARTIAL_CANCELLED), and those shares are real: exempting the
reason would let a symbol the strategy genuinely holds be bought again
the same day.

So the question is not what the row was called. It is whether any
shares ever changed hands -- a row that closed holding nothing is not a
completed trade whatever its reason, and a row that held something is,
whatever its reason.

Scope
-----
Per (strategy, symbol). A different strategy holding the same symbol is
a DIFFERENT question -- ownership -- and `reconciliation/ownership.py`
answers it and fails closed on a conflict. This module never bypasses
that: it is an additional refusal, never a permission.
"""

import logging
from typing import Optional

from config import strategy_registry
from market_hours import us_trading_day

logger = logging.getLogger(__name__)

SAME_DAY_REENTRY_BLOCK = "SAME_DAY_REENTRY_BLOCK"

#: Raised when the history cannot be read. Never "no exits found".
REENTRY_STATE_UNKNOWN = "REENTRY_STATE_UNKNOWN"

#: Closure reasons that record something other than a sale. See above.
NON_TRADE_EXIT_REASONS = frozenset({
    "RELEASED_WRONGLY_ATTRIBUTED",
    "BUY_NEVER_FILLED",
})


class ReentryStateUnavailable(Exception):
    """The exit history could not be read, so the block cannot be
    evaluated. Callers must refuse the entry -- an unreadable history is
    not an empty one."""

    def __init__(self, message, *, reason_code=REENTRY_STATE_UNKNOWN):
        super().__init__(message)
        self.reason_code = reason_code


def _trading_day_of(stamp) -> Optional[str]:
    """The US trading day an ISO timestamp falls in."""
    if not stamp:
        return None
    from datetime import datetime, timezone

    text = str(stamp)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        # A stored date with no time is already the day.
        return text[:10] or None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return us_trading_day(moment)


def exits_today(conn, *, strategy_id, trading_day=None, now=None):
    """Every genuine exit this strategy completed today, by symbol.

    Returns {SYMBOL: {"exit_price", "exit_reason", "position_id",
    "closed_at"}} for the most recent exit of each symbol.
    """
    slot = strategy_registry.slot_for(strategy_id)
    if slot is None:
        raise ReentryStateUnavailable(
            f"{strategy_id!r} is not a known live strategy, so its exit "
            "history cannot be scoped")
    table = strategy_registry.POSITION_TABLES.get(slot)
    if not table:
        raise ReentryStateUnavailable(
            f"no position table is registered for {slot}")

    day = trading_day or us_trading_day(now)
    try:
        rows = conn.execute(
            f"SELECT symbol, exit_reason, closed_at, position_id, quantity, "  # noqa: S608
            f"       {'exit_price' if _has_column(conn, table, 'exit_price') else 'NULL'} AS exit_price "
            f"FROM {table} WHERE status = 'CLOSED' AND closed_at IS NOT NULL "
            f"ORDER BY closed_at"
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 - unreadable is not empty
        raise ReentryStateUnavailable(
            f"{table} could not be read, so same-day exits for {slot} are "
            f"unknown: {type(exc).__name__}") from exc

    found = {}
    for row in rows:
        reason = (row["exit_reason"] if hasattr(row, "keys") else row[1])
        if reason in NON_TRADE_EXIT_REASONS:
            continue
        if not _held_shares(row):
            # Closed without ever holding anything -- an intent, not a
            # trade. See "The reason alone is not enough" above.
            continue
        symbol = str((row["symbol"] if hasattr(row, "keys") else row[0]) or "").upper()
        if not symbol:
            continue
        closed_at = row["closed_at"] if hasattr(row, "keys") else row[2]
        if _trading_day_of(closed_at) != day:
            continue
        found[symbol] = {
            "symbol": symbol,
            "exit_reason": reason,
            "closed_at": closed_at,
            "position_id": row["position_id"] if hasattr(row, "keys") else row[3],
            "exit_price": row["exit_price"] if hasattr(row, "keys") else row[4],
        }
    return found



def _held_shares(row) -> bool:
    """Did this position ever actually hold shares?

    A quantity that is NULL or zero means the entry never filled, so the
    close records an abandoned intent rather than a completed trade.
    Anything unreadable counts as HELD: the block exists to refuse, and
    a value we cannot interpret must not become permission.
    """
    try:
        quantity = row["quantity"] if hasattr(row, "keys") else None
    except (KeyError, IndexError):
        return True
    if quantity is None:
        return False
    try:
        return float(quantity) > 0
    except (TypeError, ValueError):
        return True


def _has_column(conn, table, column) -> bool:
    try:
        return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))
    except Exception:  # noqa: BLE001
        return False


def blocked_symbols(conn, *, strategy_id, trading_day=None, now=None):
    """The symbols this strategy may not buy again today."""
    return frozenset(exits_today(conn, strategy_id=strategy_id,
                                 trading_day=trading_day, now=now))


def record_block(conn, *, strategy_id, symbol, previous_exit, now=None,
                 candidate_rank=None, candidate_score=None,
                 candidate_price=None, trading_day=None, tracking_id=None):
    """Note that a BUY was refused, and everything needed to judge it later.

    Never fatal. This is research bookkeeping about a refusal that has
    already happened; failing to write it must not turn a clean block
    into an error.
    """
    from datetime import datetime, timezone

    current = now or datetime.now(timezone.utc)
    stamp = current.isoformat()
    previous_exit = previous_exit or {}
    try:
        conn.execute(
            "INSERT INTO reentry_blocks (strategy_id, symbol, trading_day, "
            "blocked_at, reason_code, candidate_rank, candidate_score, "
            "candidate_price, previous_exit_price, previous_exit_reason, "
            "previous_position_id, tracking_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (strategy_id, str(symbol or "").upper(),
             trading_day or us_trading_day(current), stamp,
             SAME_DAY_REENTRY_BLOCK, candidate_rank, candidate_score,
             candidate_price, previous_exit.get("exit_price"),
             previous_exit.get("exit_reason"), previous_exit.get("position_id"),
             tracking_id, stamp))
        conn.commit()
        return True
    except Exception:  # noqa: BLE001
        logger.warning("could not record the re-entry block for %s/%s",
                       strategy_id, symbol, exc_info=True)
        return False
