"""One S6 trade, end to end, from the records that already exist.

What this is for
----------------
The first real S6-R trade is the only one that gets read closely, and it
is read to answer questions that a P&L number cannot: did the candidate
that fired become the order that was sent, did the fill land where the
signal said, did the exit trigger for the reason the policy claims, and
did reconciliation agree at the end. Those answers live in four places --
the candidate file, `s6_positions`, the exit-intent ledger and the
account reconciler -- and stitching them by hand at the moment the first
trade lands is how the wrong conclusion gets drawn quickly.

Absent is ABSENT
----------------
Every stage that has not happened is None with a stated reason, never a
zero and never a carried-forward value. A position that has not exited
has no `holding_minutes`; writing the time since entry there would report
an open trade as a closed one, and it would average into the month-1
review as a real holding period.

Two figures are recorded, not derived
-------------------------------------
`mae_pct` reads `s6_positions.trough_price` and `realized_pnl` reads
`s6_positions.exit_price`. Both were added because they cannot be
recovered afterwards -- the bars are gone and the intended price is not
the fill. A trade that closed before those columns existed reports them
as None with that reason, rather than estimating them from what is left.

It decides nothing
------------------
Read-only over the database and the candidate files. No broker call, no
write, no order.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import s6_sessions

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "s6_trade_timeline_v1"

#: The lifecycle, in order. Printed in full every time -- a stage that
#: did not happen is a row saying so, because a timeline that omitted it
#: would let a skipped stage read as an unimportant one.
STAGES = (
    "CANDIDATE",
    "BUY_SUBMITTED",
    "BUY_FILL",
    "OPEN",
    "EXIT_SIGNAL",
    "SELL_SUBMITTED",
    "SELL_FILL",
    "CLOSED",
    "RECONCILIATION",
)

REACHED = "REACHED"
NOT_REACHED = "NOT_REACHED"
NOT_RECORDED = "NOT_RECORDED"


def _num(value) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number or number in (
        float("inf"), float("-inf")) else number


def _at(value) -> Optional[datetime]:
    from s1_live.freshness import as_utc

    return as_utc(value)


def _minutes(start, end) -> Optional[float]:
    a, b = _at(start), _at(end)
    if a is None or b is None:
        return None
    delta = (b - a).total_seconds() / 60.0
    return round(delta, 3) if delta >= 0 else None


def _pct(base, value) -> Optional[float]:
    base, value = _num(base), _num(value)
    if base is None or value is None or base <= 0:
        return None
    return round((value / base - 1.0) * 100.0, 6)


def first_trade(conn, *, variant=None) -> Optional[Dict[str, Any]]:
    """The earliest S6 position ever recorded, or None.

    Earliest by `submitted_at`, which is stamped before the broker
    answers -- so the first trade is the first one we SENT, not the first
    one that happened to fill. Those can differ, and when they do the
    difference is the interesting part.
    """
    from s6_live import position_store

    sql = f"SELECT * FROM {position_store.TABLE}"
    params: List[Any] = []
    if variant is not None:
        sql += " WHERE variant = ?"
        params.append(variant)
    sql += " ORDER BY submitted_at, position_id LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def build(conn, *, position_id=None, variant=None, now=None) -> Dict[str, Any]:
    """The timeline for one position -- by default the first S6 trade.

    Never raises. With no S6 position at all it returns a report saying
    exactly that, which is the honest answer today and the one the
    activation gate reads as NOT_MEASURED rather than as a pass.
    """
    from s6_live import position_store
    from scanners.publish import s6_snapshot

    moment = now or datetime.now(timezone.utc)
    report: Dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "generated_at": moment.isoformat(),
        "origin": s6_snapshot.origin(),
        "variant_filter": variant,
        "broker_submit_count": 0,
        "errors": [],
    }

    row = None
    try:
        row = (position_store.load(conn, position_id) if position_id
               else first_trade(conn, variant=variant))
    except Exception as exc:  # noqa: BLE001
        logger.warning("S6 trade timeline: position lookup failed",
                       exc_info=True)
        report["errors"].append(f"position lookup: {exc}")

    if row is None:
        report.update({
            "trade_found": False,
            "detail": ("no S6 position has ever been recorded"
                       if position_id is None else
                       f"no S6 position {position_id!r}"),
            "stages": [{"stage": stage, "status": NOT_REACHED, "at": None,
                        "detail": "no S6 trade exists yet"}
                       for stage in STAGES],
        })
        return report

    report["trade_found"] = True
    report.update(_identity(row))
    for name, step in (("stages", lambda: {"stages": _stages(conn, row)}),
                       ("metrics", lambda: _metrics(row)),
                       ("orders", lambda: _orders(conn, row)),
                       ("reconciliation", lambda: _reconciliation(conn, row))):
        try:
            report.update(step())
        except Exception as exc:  # noqa: BLE001 - a section that cannot be
            # built is recorded as missing, not as a crash.
            logger.warning("S6 trade timeline: %s failed", name, exc_info=True)
            report["errors"].append(f"{name}: {exc}")
    return report


def _identity(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "position_id": row.get("position_id"),
        "strategy_id": row.get("strategy_id"),
        "symbol": row.get("symbol"),
        "variant": row.get("variant"),
        "entry_session": row.get("entry_session"),
        "venue": row.get("venue"),
        "status": row.get("status"),
        "quantity": row.get("quantity"),
    }


def _stage(name, status, at=None, detail="") -> Dict[str, Any]:
    return {"stage": name, "status": status, "at": at, "detail": detail}


def _stages(conn, row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Each lifecycle stage, with the timestamp that proves it happened."""
    status = str(row.get("status") or "")
    entry_price = _num(row.get("entry_price"))
    held = status in ("OPEN", "EXIT_PENDING", "EXIT_SUBMITTED")
    closed = status == "CLOSED"
    filled = entry_price is not None

    stages = [
        _stage("CANDIDATE", REACHED if row.get("range_high") is not None
               else NOT_RECORDED,
               row.get("submitted_at"),
               f"range {row.get('range_low')}..{row.get('range_high')} "
               f"over {row.get('range_minutes')}m"
               if row.get("range_high") is not None
               else "the position carries no range; it did not come from a "
                    "published S6 candidate row"),
        _stage("BUY_SUBMITTED", REACHED, row.get("submitted_at"),
               f"client_order_id={row.get('client_order_id')}"),
        _stage("BUY_FILL", REACHED if filled else NOT_REACHED,
               row.get("entry_time"),
               f"{row.get('quantity')} @ {entry_price}" if filled
               else "the BUY has not been confirmed filled"),
        _stage("OPEN", REACHED if (filled and (held or closed)) else NOT_REACHED,
               row.get("entry_time"),
               f"status={status}"),
        _stage("EXIT_SIGNAL",
               REACHED if row.get("pending_exit_reason") or row.get("exit_reason")
               else NOT_REACHED,
               row.get("pending_exit_since"),
               str(row.get("pending_exit_reason") or row.get("exit_reason") or
                   "no exit has triggered")),
        _stage("SELL_SUBMITTED",
               REACHED if row.get("exit_submitted") else NOT_REACHED,
               row.get("updated_at") if row.get("exit_submitted") else None,
               "exit_submitted=1" if row.get("exit_submitted")
               else "no SELL has been sent"),
        _stage("SELL_FILL",
               REACHED if (closed and _num(row.get("exit_price")) is not None)
               else (NOT_RECORDED if closed else NOT_REACHED),
               row.get("closed_at"),
               f"@ {_num(row.get('exit_price'))}"
               if _num(row.get("exit_price")) is not None
               else ("the position closed before exit_price was recorded"
                     if closed else "no SELL fill yet")),
        _stage("CLOSED", REACHED if closed else NOT_REACHED,
               row.get("closed_at"),
               str(row.get("exit_reason") or "")),
    ]
    stages.append(_reconciliation_stage(conn, row))
    return stages


def _reconciliation_stage(conn, row) -> Dict[str, Any]:
    """Does the account agree this position exists (or does not)?

    Answered from the internal attribution alone: comparing against the
    broker needs an account read, and a timeline is not a place to make
    one. What it can say is whether the position is ATTRIBUTABLE -- which
    is the half that was missing when a broker position could not be
    matched to any strategy.
    """
    try:
        from reconciliation import internal_holdings

        holdings = internal_holdings.strategy_holdings(conn).get(
            s6_sessions.STRATEGY_ID) or []
    except Exception as exc:  # noqa: BLE001
        return _stage("RECONCILIATION", NOT_RECORDED, None,
                      f"attribution unavailable: {exc}")

    symbol = str(row.get("symbol") or "").upper()
    mine = [h for h in holdings if str(h[0]).upper() == symbol]
    if str(row.get("status")) == "CLOSED":
        return _stage("RECONCILIATION",
                      REACHED if not mine else NOT_RECORDED,
                      row.get("closed_at"),
                      "closed and no longer claimed by S6" if not mine
                      else f"CLOSED but still attributed: {mine}")
    if mine:
        return _stage("RECONCILIATION", REACHED, row.get("updated_at"),
                      f"attributed to S6: {mine}")
    return _stage("RECONCILIATION", NOT_REACHED, None,
                  "the position holds no shares yet, so it is not "
                  "attributable")


def _metrics(row: Dict[str, Any]) -> Dict[str, Any]:
    """The numbers §5 asks for. None where the trade has not produced one."""
    entry = _num(row.get("entry_price"))
    exit_price = _num(row.get("exit_price"))
    peak = _num(row.get("peak_price"))
    trough = _num(row.get("trough_price"))
    quantity = row.get("quantity")
    closed = str(row.get("status")) == "CLOSED"

    realized = None
    if entry is not None and exit_price is not None and quantity:
        realized = round((exit_price - entry) * int(quantity), 6)

    return {
        "entry_price": entry,
        "entry_time": row.get("entry_time"),
        "range_high": _num(row.get("range_high")),
        "range_low": _num(row.get("range_low")),
        "range_minutes": row.get("range_minutes"),
        "structural_risk_pct": (
            round((entry - _num(row.get("range_low"))) / entry * 100.0, 6)
            if entry and _num(row.get("range_low")) is not None and entry > 0
            else None),
        "entry_vwap": _num(row.get("entry_vwap")),
        "entry_ema9": _num(row.get("entry_ema9")),
        "entry_ema21": _num(row.get("entry_ema21")),
        "entry_volume_expansion": _num(row.get("entry_volume_expansion")),
        "peak_volume_expansion": _num(row.get("peak_volume_expansion")),

        "peak_price": peak,
        "trough_price": trough,
        # Clamped in the favourable/adverse direction respectively: an
        # excursion that never went favourable is zero, not a negative
        # favourable one.
        "mfe_pct": (max(0.0, _pct(entry, peak) or 0.0)
                    if entry and peak is not None else None),
        "mae_pct": (min(0.0, _pct(entry, trough) or 0.0)
                    if entry and trough is not None else None),
        "mae_detail": (None if trough is not None else
                       "no trough was recorded for this position"),

        "exit_reason": row.get("exit_reason"),
        "pending_exit_reason": row.get("pending_exit_reason"),
        "exit_price": exit_price,
        "exit_time": row.get("closed_at"),
        "holding_minutes": (_minutes(row.get("entry_time"), row.get("closed_at"))
                            if closed else None),
        "holding_detail": (None if closed else
                           "the position is still open; no holding period "
                           "exists yet"),
        "realized_pnl": realized,
        "realized_pnl_pct": _pct(entry, exit_price),
        "realized_detail": (None if realized is not None else
                            "no realised P&L: the trade has no recorded "
                            "exit fill"),
    }


def _orders(conn, row: Dict[str, Any]) -> Dict[str, Any]:
    """Order identity on both sides, and the duplicate-SELL defence.

    The exit-intent ledger is the authority on the sell: it is what
    refuses a second order for a position that already has one live, so
    "was a duplicate possible" is answered by counting its rows rather
    than by trusting that nothing went wrong.
    """
    from state_store import exit_intent_ledger

    intents: List[Dict[str, Any]] = []
    try:
        rows = conn.execute(
            "SELECT * FROM exit_intents WHERE position_id = ? "
            "ORDER BY created_at", (row.get("position_id"),)).fetchall()
        intents = [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        return {"exit_intents": [], "duplicate_order_detected": None,
                "order_detail": f"exit-intent ledger unreadable: {exc}"}

    active = None
    try:
        active = exit_intent_ledger.get_active_intent(
            conn, row.get("position_id"))
    except Exception:  # noqa: BLE001
        active = None

    # Read from the ledger's own set rather than restated as "not
    # terminal": a state added there and not here would silently stop
    # counting, and the count is the duplicate check.
    live = [i for i in intents
            if str(i.get("state")) in exit_intent_ledger.NON_TERMINAL_STATES]
    return {
        "entry_client_order_id": row.get("client_order_id"),
        "entry_broker_order_id": row.get("entry_order_id"),
        "exit_client_order_ids": [i.get("client_order_id") for i in intents],
        "exit_broker_order_ids": [i.get("broker_order_id") for i in intents
                                  if i.get("broker_order_id")],
        "exit_intent_states": [i.get("state") for i in intents],
        "exit_intents": len(intents),
        "active_exit_intent": (dict(active) if active is not None else None),
        # More than one live intent for one position is the duplicate the
        # ledger exists to make impossible. Reported so the first trade
        # can confirm it stayed impossible, rather than assumed.
        "duplicate_order_detected": len(live) > 1,
        "order_detail": (f"{len(intents)} exit intent(s), {len(live)} not "
                         f"terminal"),
    }


def _reconciliation(conn, row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from reconciliation import internal_holdings

        return {"reconciliation_attribution":
                internal_holdings.attribution(conn)}
    except Exception as exc:  # noqa: BLE001
        return {"reconciliation_attribution": None,
                "reconciliation_detail": f"unavailable: {exc}"}


def _fmt(value, dash="-") -> str:
    return dash if value is None else str(value)


def format_report(report: Dict[str, Any]) -> str:
    if not report.get("trade_found"):
        return "\n".join([
            "S6 FIRST LIVE TRADE",
            "=" * 64,
            f"  generated : {_fmt(report.get('generated_at'))}",
            f"  origin    : {_fmt(report.get('origin'))}",
            "",
            f"  {report.get('detail')}",
            "",
            "  Every stage below is NOT_REACHED. This is the expected state",
            "  while S6 is DISCOVERY_ONLY -- the report exists now so that",
            "  the first trade is described by something that was written",
            "  before it happened.",
            "",
        ] + [f"    {stage['stage']:<16}: {stage['status']}"
             for stage in report.get("stages") or []]
            + ["", f"  broker submit count : {report.get('broker_submit_count', 0)}"])

    lines = [
        "S6 FIRST LIVE TRADE",
        "=" * 64,
        f"  generated   : {_fmt(report.get('generated_at'))}",
        f"  origin      : {_fmt(report.get('origin'))}",
        f"  position    : {_fmt(report.get('position_id'))}",
        f"  symbol      : {_fmt(report.get('symbol'))} "
        f"({_fmt(report.get('variant'))}, {_fmt(report.get('entry_session'))})",
        f"  status      : {_fmt(report.get('status'))} "
        f"qty={_fmt(report.get('quantity'))} venue={_fmt(report.get('venue'))}",
        "",
        "  timeline",
        "  " + "-" * 62,
    ]
    for stage in report.get("stages") or []:
        lines.append(f"    {stage['stage']:<16} {stage['status']:<12} "
                     f"{_fmt(stage.get('at'))}")
        if stage.get("detail"):
            lines.append(f"        {stage['detail']}")

    lines += [
        "",
        "  measures",
        "  " + "-" * 62,
    ]
    for key in ("entry_price", "exit_price", "realized_pnl",
                "realized_pnl_pct", "peak_price", "trough_price", "mfe_pct",
                "mae_pct", "holding_minutes", "structural_risk_pct",
                "range_high", "range_low", "exit_reason"):
        lines.append(f"    {key:<22}: {_fmt(report.get(key))}")
    for key in ("mae_detail", "holding_detail", "realized_detail"):
        if report.get(key):
            lines.append(f"      note: {report[key]}")

    lines += [
        "",
        "  orders",
        "  " + "-" * 62,
        f"    entry client order id : {_fmt(report.get('entry_client_order_id'))}",
        f"    entry broker order id : {_fmt(report.get('entry_broker_order_id'))}",
        f"    exit client order ids : {_fmt(report.get('exit_client_order_ids'))}",
        f"    exit broker order ids : {_fmt(report.get('exit_broker_order_ids'))}",
        f"    exit intent states    : {_fmt(report.get('exit_intent_states'))}",
        f"    duplicate detected    : {_fmt(report.get('duplicate_order_detected'))}",
        "",
        f"    reconciliation        : "
        f"{_fmt(report.get('reconciliation_attribution'))}",
        "",
        f"  broker submit count : {report.get('broker_submit_count', 0)}",
    ]
    for error in report.get("errors") or []:
        lines.append(f"  ERROR: {error}")
    return "\n".join(lines)
