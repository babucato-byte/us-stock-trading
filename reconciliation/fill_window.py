"""How far back KIS's fill history must be read.

The defect this closes
----------------------
Both readers of `get_fills()` asked for TODAY only:

    broker.get_fills(start_date=today, end_date=today)

An order that was accepted on one day and filled on that day is fine.
An order whose ledger row is still non-terminal the NEXT morning is not:
today's fill list cannot contain a fill from a previous session, so
`_check_open_orders` computes

    internal_live_ids - kis_open_ids - kis_fill_ids

as permanently non-empty. Reconciliation then reports "recorded
internally as live but KIS reports neither an open order nor any fill
for it" on every pass, forever, and every BUY -- for every strategy --
is blocked by a snapshot that can never come clean again. That is not a
hypothetical: order 0030469882 (TX, filled 1 @ 53.68 on 2026-08-18) put
the account in exactly that state and blocked 1,040 entry attempts.

The window is DERIVED, not guessed
----------------------------------
The correct start date is the oldest trading day this codebase still
believes has an order working at KIS. Anything older cannot be relevant,
because every other row is terminal; anything newer would reintroduce
the same blind spot. So the window is read from the ledger rather than
set to a comfortable-looking constant.

`MAX_LOOKBACK_DAYS` clamps it. KIS's overseas fill inquiry will not
serve an unbounded range, and a single corrupt `trading_date` must not
turn one read into an unbounded one. The clamp is a guard against a bad
row, not a policy about how far back a fill may count -- when it bites,
the window is still wide enough to cover any order young enough to
matter, and the mismatch stays visible rather than being silently
resolved.
"""

from datetime import datetime, timedelta, timezone

#: Idempotency statuses meaning "this codebase believes the order is
#: live at KIS right now". Kept in step with
#: reconciliation/snapshot.py's `_INTERNAL_LIVE_STATUSES`, which is the
#: check whose blind spot this module exists to remove.
LIVE_STATUSES = ("SUBMITTING", "ACCEPTED", "PARTIALLY_FILLED", "CANCEL_PENDING")

#: Upper bound on the window, in calendar days.
MAX_LOOKBACK_DAYS = 90


def _as_date(value):
    """A `trading_date` string as a date, or None if it is unusable.

    Unusable means "this row cannot narrow the window", never "this row
    does not exist" -- a malformed date is ignored for the purpose of
    choosing a start, and the clamp below still bounds the read.
    """
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def oldest_live_trading_date(conn):
    """The earliest trading day with an order still believed live at KIS.

    Only rows carrying a `broker_order_id` count. A SUBMITTING row with
    no broker id was never matched against KIS by `_check_open_orders`
    either -- its ambiguity is the UNKNOWN check's business, and widening
    the fill window for it would buy nothing.

    Returns None when nothing is live, which callers read as "today is
    enough".
    """
    placeholders = ",".join("?" * len(LIVE_STATUSES))
    try:
        rows = conn.execute(
            "SELECT trading_date FROM kis_order_idempotency "
            f"WHERE status IN ({placeholders}) "
            "AND broker_order_id IS NOT NULL AND broker_order_id != ''",
            LIVE_STATUSES,
        ).fetchall()
    except Exception:  # noqa: BLE001 -- an unreadable ledger must not
        # abort reconciliation; it falls back to the widest safe window,
        # which is the fail-closed direction: a read that is too wide
        # reports a mismatch, one that is too narrow hides it.
        return None
    dates = [d for d in (_as_date(row["trading_date"]) for row in rows) if d]
    return min(dates) if dates else None


def window(conn, *, now=None, max_lookback_days=MAX_LOOKBACK_DAYS):
    """`(start_date, end_date)` as KIS's `YYYYMMDD` strings.

    `end_date` is always today: a fill cannot be in the future, and the
    caller's clock is the same one every other decision in the pass uses.
    """
    current = now or datetime.now(timezone.utc)
    today = current.date()
    oldest = oldest_live_trading_date(conn)
    floor = today - timedelta(days=max_lookback_days)
    start = today if oldest is None else max(min(oldest, today), floor)
    return start.strftime("%Y%m%d"), today.strftime("%Y%m%d")


def read_fills(broker, conn, *, now=None, max_lookback_days=MAX_LOOKBACK_DAYS):
    """`broker.get_fills()` over the derived window.

    A single place both reconciliation readers call, so the window cannot
    drift back to today-only in one of them.
    """
    start, end = window(conn, now=now, max_lookback_days=max_lookback_days)
    return broker.get_fills(start_date=start, end_date=end)
