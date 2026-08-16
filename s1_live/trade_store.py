"""The S1 Limited Live trade ledger.

One row per S1 trade, from the entry attempt through to the exit and its
net result. Distinct from `orders`/`fills`, which record what the order
system did; this records what the experiment earned, and only this table
needs a scanner run id sitting next to a fee.

Unknown is not zero, and net P&L is not gross
---------------------------------------------
The rule this module exists to enforce: `net_pnl` may only be written
when every fee component is known. Overseas trading has real commission,
regulatory fees and FX cost, and none of them are established in this
codebase yet -- `backtest/config.py` carries `fee_per_share = 0.0` with
the comment "Alpaca is commission-free", which is true of Alpaca and
irrelevant to KIS.

So `record_fees()` refuses to compute a net figure while any component
is UNKNOWN. The alternative -- defaulting the unknown ones to 0 --
produces a number that looks like net profit, reads as authoritative in
a report, and is actually gross. A small account turning over quickly is
exactly where that error is largest, which is the thing this pilot is
meant to measure.

The broker outranks this table
------------------------------
`broker_order_id`, fill prices and fill timestamps come from KIS's own
order/execution endpoints. `apply_broker_fill()` overwrites local values
with reported ones and never the reverse. A locally computed number must
never win over a reported fill.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

FEES_UNKNOWN = "UNKNOWN"
FEES_REPORTED = "REPORTED"
FEES_PARTIAL = "PARTIAL"

#: The components that must all be known before a net figure exists.
FEE_COMPONENTS = ("commission", "regulatory_fees", "fx_cost")

TABLE = "s1_live_trades"


class TradeStoreError(Exception):
    """A trade record could not be written or read."""


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def new_trade_id() -> str:
    return f"s1trade-{uuid.uuid4().hex[:16]}"


def _num(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def open_trade(conn, *, source_signal_id, scanner_run_id, trading_day,
               allocation_version, scanner_score=None, candidate_rank=None,
               internal_order_id=None, entry_submitted_at=None,
               allocated_cash=None, account_cash_before=None,
               trade_id=None, now=None) -> str:
    """Record an entry attempt. Returns the trade id.

    Keyed uniquely on `source_signal_id`: one scanner observation may
    produce at most one trade. That uniqueness is a database constraint
    rather than a check, so two concurrent attempts on the same signal
    cannot both succeed.
    """
    identifier = trade_id or new_trade_id()
    stamp = now or _now_iso()
    try:
        conn.execute(
            f"INSERT INTO {TABLE} ("
            " trade_id, source_signal_id, scanner_run_id, scanner_score,"
            " candidate_rank, allocation_version, trading_day, internal_order_id,"
            " entry_submitted_at, allocated_cash, account_cash_before,"
            " fees_status, created_at, updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (identifier, str(source_signal_id), str(scanner_run_id),
             _num(scanner_score), candidate_rank, str(allocation_version),
             str(trading_day), internal_order_id, entry_submitted_at,
             _num(allocated_cash), _num(account_cash_before),
             FEES_UNKNOWN, stamp, stamp))
    except Exception as exc:  # noqa: BLE001 - surfaced as one error type
        raise TradeStoreError(f"could not open trade for {source_signal_id}: {exc}") from exc
    return identifier


def apply_broker_fill(conn, trade_id, *, side, broker_order_id=None,
                      filled_at=None, price=None, quantity=None, now=None) -> None:
    """Write what the BROKER reported. Overwrites local values.

    `side` is "entry" or "exit". Values that are None are left alone --
    a report that omits a field must not blank a field that was already
    established.
    """
    if side not in ("entry", "exit"):
        raise TradeStoreError(f"side must be 'entry' or 'exit', got {side!r}")
    columns, values = [], []
    if broker_order_id is not None:
        columns.append("broker_order_id = ?")
        values.append(str(broker_order_id))
    if filled_at is not None:
        columns.append(f"{side}_filled_at = ?")
        values.append(str(filled_at))
    if price is not None:
        columns.append(f"{side}_price = ?")
        values.append(_num(price))
    if quantity is not None and side == "entry":
        columns.append("qty = ?")
        values.append(int(quantity))
    if not columns:
        return
    columns.append("updated_at = ?")
    values.extend([now or _now_iso(), trade_id])
    try:
        conn.execute(f"UPDATE {TABLE} SET {', '.join(columns)} WHERE trade_id = ?", values)
    except Exception as exc:  # noqa: BLE001
        raise TradeStoreError(f"could not apply broker fill to {trade_id}: {exc}") from exc


def close_trade(conn, trade_id, *, exit_submitted_at=None, exit_reason=None,
                account_cash_after=None, now=None) -> None:
    try:
        conn.execute(
            f"UPDATE {TABLE} SET exit_submitted_at = COALESCE(?, exit_submitted_at),"
            " exit_reason = COALESCE(?, exit_reason),"
            " account_cash_after = COALESCE(?, account_cash_after),"
            " updated_at = ? WHERE trade_id = ?",
            (exit_submitted_at, exit_reason, _num(account_cash_after),
             now or _now_iso(), trade_id))
    except Exception as exc:  # noqa: BLE001
        raise TradeStoreError(f"could not close trade {trade_id}: {exc}") from exc


def compute_gross_pnl(entry_price, exit_price, qty) -> Optional[float]:
    entry, exit_, quantity = _num(entry_price), _num(exit_price), _num(qty)
    if entry is None or exit_ is None or quantity is None:
        return None
    return round((exit_ - entry) * quantity, 6)


def record_fees(conn, trade_id, *, commission=None, regulatory_fees=None,
                fx_cost=None, estimated_slippage=None, source=None,
                now=None) -> Dict[str, Any]:
    """Store whatever fee components are KNOWN, and derive net only if all are.

    Returns the resulting fee state. `net_pnl` stays NULL for as long as
    any component is unknown -- see the module docstring for why a
    zero-filled net figure is worse than no figure.
    """
    row = read_trade(conn, trade_id)
    if row is None:
        raise TradeStoreError(f"no such trade {trade_id}")

    values = {
        "commission": _num(commission) if commission is not None else _num(row.get("commission")),
        "regulatory_fees": (_num(regulatory_fees) if regulatory_fees is not None
                            else _num(row.get("regulatory_fees"))),
        "fx_cost": _num(fx_cost) if fx_cost is not None else _num(row.get("fx_cost")),
    }
    known = [name for name, value in values.items() if value is not None]
    if len(known) == len(FEE_COMPONENTS):
        status = FEES_REPORTED
        fees_total = round(sum(values.values()), 6)
    elif known:
        status, fees_total = FEES_PARTIAL, None
    else:
        status, fees_total = FEES_UNKNOWN, None

    gross = compute_gross_pnl(row.get("entry_price"), row.get("exit_price"),
                              row.get("qty"))
    net = round(gross - fees_total, 6) if (gross is not None and fees_total is not None) else None

    try:
        conn.execute(
            f"UPDATE {TABLE} SET commission = ?, regulatory_fees = ?, fx_cost = ?,"
            " fees_total = ?, estimated_slippage = COALESCE(?, estimated_slippage),"
            " gross_pnl = ?, net_pnl = ?, fees_status = ?, fees_source = COALESCE(?, fees_source),"
            " updated_at = ? WHERE trade_id = ?",
            (values["commission"], values["regulatory_fees"], values["fx_cost"],
             fees_total, _num(estimated_slippage), gross, net, status, source,
             now or _now_iso(), trade_id))
    except Exception as exc:  # noqa: BLE001
        raise TradeStoreError(f"could not record fees for {trade_id}: {exc}") from exc

    return {"fees_status": status, "fees_total": fees_total,
            "gross_pnl": gross, "net_pnl": net,
            "unknown_components": [name for name in FEE_COMPONENTS
                                   if values.get(name) is None]}


def read_trade(conn, trade_id) -> Optional[Dict[str, Any]]:
    try:
        cursor = conn.execute(f"SELECT * FROM {TABLE} WHERE trade_id = ?", (trade_id,))
        row = cursor.fetchone()
    except Exception as exc:  # noqa: BLE001
        raise TradeStoreError(f"could not read trade {trade_id}: {exc}") from exc
    return dict(row) if row is not None else None


def read_trades_for_day(conn, trading_day) -> List[Dict[str, Any]]:
    try:
        rows = conn.execute(
            f"SELECT * FROM {TABLE} WHERE trading_day = ? ORDER BY created_at",
            (str(trading_day),)).fetchall()
    except Exception as exc:  # noqa: BLE001
        raise TradeStoreError(f"could not read trades for {trading_day}: {exc}") from exc
    return [dict(row) for row in rows]


def day_summary(conn, trading_day) -> Dict[str, Any]:
    """Counts and money for one day, with unknowns kept visible.

    `net_pnl_usd` is None whenever ANY closed trade still has unknown
    fees. A day summary that silently omitted those trades would report
    a net figure for a subset while looking like it covered the day.
    """
    trades = read_trades_for_day(conn, trading_day)
    closed = [row for row in trades if row.get("exit_price") is not None]
    unknown = [row for row in closed if row.get("fees_status") != FEES_REPORTED]
    gross = [row["gross_pnl"] for row in closed if row.get("gross_pnl") is not None]
    wins = [value for value in gross if value > 0]
    return {
        "trading_day": str(trading_day),
        "trades": len(trades),
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(gross) - len(wins),
        "gross_pnl_usd": round(sum(gross), 6) if gross else None,
        "net_pnl_usd": (round(sum(row["net_pnl"] for row in closed), 6)
                        if closed and not unknown else None),
        "fees_unknown_trades": len(unknown),
        "net_pnl_blocked_reason": (
            f"{len(unknown)} closed trade(s) have unverified fees" if unknown else None),
    }


def as_json(row: Dict[str, Any]) -> str:
    return json.dumps(row, indent=2, sort_keys=True, default=str)
