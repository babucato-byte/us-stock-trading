"""What did KIS actually do with this order? One inquiry, one answer.

Why this exists as a shared module
----------------------------------
`kis_position_manager._find_kis_fill_for_order` already knew how to read
a KIS fill: sweep `get_fills()`, match on `ODNO`, sum `ft_ccld_qty`
across execution ROWS, and weight `ft_ccld_unpr3` by quantity. Every one
of those details is load-bearing and was learned the hard way -- CODEX-044
records that `ft_ccld_qty` is per-execution and not cumulative, so
"a fill row exists" is not "the order filled".

S6 needs the same read. Copying it would have made a second idea of what
a fill is, and the two would diverge exactly once, silently, on a partial.
So the parsing lives here and S1's helper delegates to it.

The distinction this adds
-------------------------
The original returned None for BOTH "no fill yet" and "the inquiry
failed". For S1's tick that was survivable -- it retries next minute.
For S6 it is not: `sync_buy_fills` ABANDONS a submission it believes
never filled, and an inquiry failure that read as "no fill" would abandon
a position the account is actually holding.

    NO_FILL     asked, answered, nothing filled
    PARTIAL     asked, answered, some filled
    FILLED      asked, answered, all filled
    UNKNOWN     could not ask, or the answer was unusable

UNKNOWN carries `filled_quantity=None`, never 0. Nothing downstream may
turn it into a state change.

Cumulative, never delta
-----------------------
`filled_quantity` is the CUMULATIVE quantity KIS reports for the order,
recomputed from scratch on every inquiry. The stores compare cumulative
against what they already hold, so re-reading the same fill is a no-op
and a restart cannot double a position. Emitting a delta would make
correctness depend on every report being seen exactly once.

Session-independent
-------------------
Nothing here knows about sessions or variants. The same inquiry answers
for a PREMARKET entry sold in REGULAR, because an order id is an order
id. A lookup keyed on entry variant would have to be re-verified per
session for no reason.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STATUS_NO_FILL = "NO_FILL"
STATUS_PARTIAL = "PARTIALLY_FILLED"
STATUS_FILLED = "FILLED"
STATUS_UNKNOWN = "UNKNOWN"

#: KIS's own field names, in the case variants the API has been observed
#: to return. Listed once so a rename is a one-line change rather than a
#: hunt through three modules.
FIELD_ORDER_ID = ("ODNO", "odno")
FIELD_EVENT_QTY = ("ft_ccld_qty", "FT_CCLD_QTY")
FIELD_EVENT_PRICE = ("ft_ccld_unpr3", "FT_CCLD_UNPR3")
FIELD_SYMBOL = ("pdno", "PDNO", "ovrs_pdno", "OVRS_PDNO")
FIELD_ORDERED_QTY = ("ft_ord_qty", "FT_ORD_QTY", "ord_qty", "ORD_QTY")
FIELD_TIME = ("ccld_tm", "CCLD_TM", "ord_tmd", "ORD_TMD")
FIELD_EXCHANGE = ("ovrs_excg_cd", "OVRS_EXCG_CD")


@dataclass(frozen=True)
class FillReport:
    """One order's state at the broker. Every field is observed."""

    order_id: Optional[str]
    status: str
    symbol: Optional[str] = None
    side: Optional[str] = None
    ordered_quantity: Optional[float] = None
    #: CUMULATIVE filled quantity. None when the state is UNKNOWN.
    filled_quantity: Optional[float] = None
    remaining_quantity: Optional[float] = None
    average_fill_price: Optional[float] = None
    broker_timestamp: Optional[str] = None
    venue: Optional[str] = None
    #: True when the order is gone from the broker's open-order list, so
    #: "nothing filled" is final rather than "not yet".
    terminal: bool = False
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.status != STATUS_UNKNOWN

    def as_store_fill(self) -> Optional[Dict[str, Any]]:
        """The shape `s6_live.exit_runtime` expects, or None.

        None means "do not act": either the inquiry failed, or the order
        has neither filled nor terminated. The stores treat None as
        "still unconfirmed", which is the correct no-op in both cases.
        """
        if self.status == STATUS_UNKNOWN:
            return None
        if not self.filled_quantity and not self.terminal:
            return None
        return {
            "filled_quantity": self.filled_quantity or 0,
            "average_fill_price": self.average_fill_price,
            "venue": self.venue,
            "order_id": self.order_id,
            "terminal": self.terminal,
            "status": self.status,
            # KIS's own execution time, verbatim. The position row
            # stamps `closed_at` with the TICK's clock, so a fill
            # collected late in a slow tick is recorded up to a tick
            # early -- DT closed at 01:00:10 on a tick whose SELL was
            # submitted at 01:00:29. This is the broker's fact and is
            # passed through unparsed: it is HHMMSS with no date, and a
            # wrong reconstruction would be worse than a coarse one.
            "broker_timestamp": self.broker_timestamp,
        }

    def as_dict(self) -> Dict[str, Any]:
        return dict(vars(self))


def _first(row: Dict[str, Any], names) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def _number(value) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number or number in (
        float("inf"), float("-inf")) else number


def _window(now: datetime, since: Optional[datetime]):
    """KIS's YYYYMMDD range covering the order's own age.

    Starts a day EARLIER than the order timestamp because that timestamp
    is UTC while KIS dates rows by its own trading day -- an order placed
    late in the ET session already belongs to the previous UTC date. That
    is not hypothetical: it is what hid the first live S1 fill. Over-
    fetching is harmless; rows are matched on the exact order number.
    """
    start = now
    if since is not None:
        try:
            start = min(since, now)
        except TypeError:
            start = now
    return ((start - timedelta(days=1)).strftime("%Y%m%d"),
            now.strftime("%Y%m%d"))


def aggregate(fills: List[Dict[str, Any]], broker_order_id: str
              ) -> Dict[str, Any]:
    """Sum the execution ROWS for one order into a cumulative position.

    `ft_ccld_qty` is per-execution, not cumulative (CODEX-044), so this
    sums and weights rather than taking the last row. A row with a
    non-numeric quantity poisons the whole answer rather than being
    skipped: a partial sum reported as cumulative is worse than no answer.
    """
    cumulative = 0.0
    weighted = 0.0
    matched = 0
    symbol = venue = stamp = None
    ordered = None
    for row in fills or []:
        if str(_first(row, FIELD_ORDER_ID) or "") != str(broker_order_id):
            continue
        matched += 1
        qty = _number(_first(row, FIELD_EVENT_QTY))
        price = _number(_first(row, FIELD_EVENT_PRICE))
        if qty is None:
            return {"unusable": (
                f"KIS fill row for {broker_order_id!r} has a non-numeric "
                f"filled quantity {_first(row, FIELD_EVENT_QTY)!r}")}
        symbol = symbol or _first(row, FIELD_SYMBOL)
        venue = venue or _first(row, FIELD_EXCHANGE)
        stamp = _first(row, FIELD_TIME) or stamp
        ordered = ordered or _number(_first(row, FIELD_ORDERED_QTY))
        if qty <= 0:
            continue
        cumulative += qty
        if price is not None and price > 0:
            weighted += qty * price
    return {"matched_rows": matched, "cumulative": cumulative,
            "average": (weighted / cumulative) if cumulative > 0 and weighted else None,
            "symbol": symbol, "venue": venue, "timestamp": stamp,
            "ordered": ordered}


def inquire(broker, *, broker_order_id, symbol=None, side=None,
            ordered_quantity=None, now=None, since=None,
            open_orders=None) -> FillReport:
    """Ask KIS what happened to one order. Never raises.

    `open_orders` may be supplied when the caller already read them, so a
    tick that inquires about several positions does one sweep rather than
    one per position.
    """
    moment = now or datetime.now(timezone.utc)
    ordered = _number(ordered_quantity)

    if not broker_order_id:
        return FillReport(
            None, STATUS_UNKNOWN, symbol=symbol, side=side,
            ordered_quantity=ordered,
            detail="no broker order id was recorded; there is nothing to "
                   "look up at KIS")

    start_date, end_date = _window(moment, since)
    try:
        fills = broker.get_fills(start_date=start_date, end_date=end_date)
    except Exception as exc:  # noqa: BLE001 - an inquiry that failed is
        # UNKNOWN. Reporting it as "nothing filled" would let
        # `sync_buy_fills` abandon a position the account is holding.
        logger.warning("KIS fill inquiry failed for %s", broker_order_id,
                       exc_info=True)
        return FillReport(
            str(broker_order_id), STATUS_UNKNOWN, symbol=symbol, side=side,
            ordered_quantity=ordered,
            detail=f"fill inquiry failed: {str(exc)[:160]}")

    summary = aggregate(fills, str(broker_order_id))
    if summary.get("unusable"):
        return FillReport(
            str(broker_order_id), STATUS_UNKNOWN, symbol=symbol, side=side,
            ordered_quantity=ordered, detail=summary["unusable"])

    cumulative = summary["cumulative"]
    ordered = ordered if ordered is not None else summary.get("ordered")

    # Is the order still live at the broker? Only that makes "nothing
    # filled" non-final. An open-order read that FAILS leaves the answer
    # non-terminal, which is the safe direction: it declines to abandon.
    still_open, open_known = _still_open(broker, broker_order_id, open_orders)

    resolved_symbol = summary.get("symbol") or symbol
    resolved_venue = summary.get("venue")
    stamp = summary.get("timestamp")

    if cumulative <= 0:
        return FillReport(
            str(broker_order_id), STATUS_NO_FILL, symbol=resolved_symbol,
            side=side, ordered_quantity=ordered, filled_quantity=0,
            remaining_quantity=ordered, average_fill_price=None,
            broker_timestamp=stamp, venue=resolved_venue,
            terminal=bool(open_known and not still_open),
            detail=("no fill rows and the order is no longer open at KIS"
                    if open_known and not still_open else
                    "no fill rows yet"))

    remaining = (ordered - cumulative) if ordered is not None else None
    filled_all = ordered is not None and cumulative >= ordered
    return FillReport(
        str(broker_order_id),
        STATUS_FILLED if filled_all else STATUS_PARTIAL,
        symbol=resolved_symbol, side=side, ordered_quantity=ordered,
        filled_quantity=cumulative,
        remaining_quantity=(max(0.0, remaining) if remaining is not None else None),
        average_fill_price=summary.get("average"),
        broker_timestamp=stamp, venue=resolved_venue,
        terminal=bool(filled_all or (open_known and not still_open)),
        detail=f"{summary['matched_rows']} execution row(s)")


def _still_open(broker, broker_order_id, open_orders):
    """(still_open, answer_known). A failed read is never terminal."""
    rows = open_orders
    if rows is None:
        try:
            rows = broker.get_open_orders()
        except Exception:  # noqa: BLE001
            logger.warning("KIS open-order read failed while inquiring about "
                           "%s", broker_order_id, exc_info=True)
            return False, False
    for row in rows or []:
        if str(_first(row, FIELD_ORDER_ID) or "") == str(broker_order_id):
            return True, True
    return False, True


def find_fill(broker, broker_order_id, *, now, since=None
              ) -> Optional[Dict[str, Any]]:
    """The legacy shape `kis_position_manager` returns.

    Kept so S1's caller is unchanged byte for byte: `{"filled_qty",
    "average_fill_price"}` or None. S1 keeps its exact behaviour while
    the parsing underneath becomes shared.
    """
    report = inquire(broker, broker_order_id=broker_order_id, now=now,
                     since=since, open_orders=[])
    if not report.filled_quantity or report.average_fill_price is None:
        return None
    return {"filled_qty": report.filled_quantity,
            "average_fill_price": report.average_fill_price}
