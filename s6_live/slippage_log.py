"""What an order actually cost, against what it was supposed to cost.

The measurement that must exist before any execution change — IOC,
re-quoting, ASK-laddering, spread-based sizing — is worth discussing.
Each of those trades certainty for fill quality, and without a baseline
there is no way to say whether the trade was good.

What the existing records could NOT support
-------------------------------------------
`order_state_events` looked like a latency source and is not. Measured
on the real OWL order:

    CREATED       15:17:16.246
    VALIDATING    15:13:06.630   <- earlier than CREATED
    APPROVED      15:13:06.630   <- identical
    SUBMITTING    15:13:06.630   <- identical
    ACCEPTED      15:13:06.630   <- identical
    FILLED        17:49:59.128   <- when RECONCILIATION ran

Four transitions share one timestamp because they are stamped with the
cycle's `current`, not the moment each happened, and the fill time is
really the moment a reconciliation pass noticed the fill hours later.
Deriving `gate_to_submit_ms` from these would produce 0ms for every
order — a fabricated precision that would then be used to argue about
execution.

So latencies are recorded going FORWARD, stamped where they occur, and
anything the historical record cannot support stays UNKNOWN.

Direction matters for slippage
------------------------------
A BUY filling above its signal price is adverse; a SELL filling above is
favourable. One signed number with a hidden convention invites the wrong
sign at exactly the wrong moment, so the raw difference and an explicit
`adverse` flag are both stored.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: What could not be established from the evidence available. Never a
#: zero, never an estimate: a missing latency and a zero latency are
#: different facts and only one of them is a measurement.
UNKNOWN = None


def log_path(trading_day, *, env=None):
    env = env if env is not None else os.environ
    root = (env.get("SLIPPAGE_LOG_DIR") or env.get("SCANNER_DATA_ROOT")
            or "/home/ubuntu/releases/us-stock-trading/shared/scanner")
    return Path(root) / "slippage" / f"{trading_day}.jsonl"


def _ms_between(start, end):
    """Milliseconds, or UNKNOWN if either end is missing.

    Also UNKNOWN when the interval is NEGATIVE. The order events showed
    CREATED stamped after VALIDATING, and a negative latency is evidence
    the stamps are unreliable rather than a fast step.
    """
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return UNKNOWN
    delta = (end - start).total_seconds() * 1000.0
    return delta if delta >= 0 else UNKNOWN


def slippage_bps(*, signal_price, fill_price, side):
    """Signed basis points, plus whether it went against us.

    Returns (bps, adverse). `bps` is fill relative to signal, always in
    the same direction, so the number means one thing regardless of
    side. `adverse` carries the interpretation, because "above signal" is
    bad for a buy and good for a sell.
    """
    try:
        signal = float(signal_price)
        fill = float(fill_price)
    except (TypeError, ValueError):
        return UNKNOWN, UNKNOWN
    if signal <= 0:
        return UNKNOWN, UNKNOWN
    bps = (fill - signal) / signal * 10000.0
    side_text = str(side or "").lower()
    if side_text == "buy":
        adverse = bps > 0
    elif side_text == "sell":
        adverse = bps < 0
    else:
        return bps, UNKNOWN
    return bps, adverse


def build_record(*, symbol, side, session, strategy_id,
                 strategy_version=None, signal_price=None, gate_price=None,
                 order_price=None, fill_price=None,
                 signal_at=None, gate_at=None, submit_at=None,
                 accepted_at=None, fill_at=None,
                 fill_detected_at=None, reconciliation_at=None,
                 market_data_asof=None,
                 qty_requested=None, qty_filled=None,
                 broker_order_id=None, internal_order_id=None,
                 exit_reason=None, evidence=None, now=None) -> Dict[str, Any]:
    """One order's execution quality, with gaps left as gaps."""
    bps, adverse = slippage_bps(signal_price=signal_price,
                                fill_price=fill_price, side=side)
    return {
        "logged_at": (now or datetime.now(timezone.utc)).isoformat(),
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "symbol": str(symbol or "").upper(),
        "side": str(side or "").lower(),
        "session": session,
        "exit_reason": exit_reason,
        "internal_order_id": internal_order_id,
        "broker_order_id": broker_order_id,

        "signal_price": signal_price,
        "gate_price": gate_price,
        "order_price": order_price,
        "fill_price": fill_price,

        "signal_at": _iso(signal_at),
        "gate_at": _iso(gate_at),
        "submit_at": _iso(submit_at),
        "accepted_at": _iso(accepted_at),
        # THREE different moments, never collapsed into one. For OWL
        # the broker's fill time was never captured, a reconciliation
        # pass noticed the fill two hours later, and that pass ran at
        # 17:49:59. Recording the last as the first reports a two-hour
        # execution latency for an order that filled in seconds.
        #
        #   broker_fill_at     when KIS says it filled -- UNKNOWN unless
        #                      the broker actually told us
        #   fill_detected_at   when our side first saw the fill
        #   reconciliation_at  when the pass that saw it ran
        "fill_at": _iso(fill_at),
        "broker_fill_at": _iso(fill_at),
        "fill_detected_at": _iso(fill_detected_at),
        "reconciliation_at": _iso(reconciliation_at),

        "market_data_asof": _iso(market_data_asof),
        "qty_requested": qty_requested,
        "qty_filled": qty_filled,

        "signal_to_gate_ms": _ms_between(signal_at, gate_at),
        "gate_to_submit_ms": _ms_between(gate_at, submit_at),
        # From the BROKER's fill time only. Deriving it from
        # `fill_detected_at` would measure how long our reconciliation
        # took to notice, and call it execution latency.
        "submit_to_fill_ms": _ms_between(submit_at, fill_at),

        "slippage_bps": bps,
        "slippage_adverse": adverse,
        #: Where each number came from, so a reader can tell a measured
        #: value from a reconstructed one.
        "evidence": evidence or "LIVE",
    }


def append(record, *, trading_day, env=None) -> bool:
    """Append one row. Never raises: an observation must not cost a trade."""
    try:
        path = log_path(trading_day, env=env)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
        return True
    except Exception:  # noqa: BLE001
        logger.warning("could not append a slippage record", exc_info=True)
        return False


def read(trading_day, *, env=None) -> List[Dict[str, Any]]:
    path = log_path(trading_day, env=env)
    rows = []
    try:
        if not path.exists():
            return rows
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                logger.warning("skipping an unreadable slippage row")
    except Exception:  # noqa: BLE001
        logger.warning("could not read slippage for %s", trading_day,
                       exc_info=True)
    return rows


def summarise(trading_day, *, env=None) -> Dict[str, Any]:
    """Aggregates over the rows that actually carry a measurement.

    Rows without a slippage figure are COUNTED but not averaged. Folding
    them in as zeros would report perfect execution for every order
    nobody could measure.
    """
    rows = read_merged(trading_day, env=env)
    measured = [r for r in rows if isinstance(r.get("slippage_bps"), (int, float))]
    values = sorted(r["slippage_bps"] for r in measured)
    return {
        "orders": len(rows),
        "measured": len(measured),
        "unmeasurable": len(rows) - len(measured),
        "median_bps": values[len(values) // 2] if values else UNKNOWN,
        "worst_adverse_bps": (max((r["slippage_bps"] for r in measured
                                   if r.get("slippage_adverse")), default=UNKNOWN)
                              if measured else UNKNOWN),
    }


def _iso(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


#: A supplement that completes an earlier row rather than a new order.
#:
#: The log is append-only, and the fill price is not known when the
#: order is accepted -- reconciliation learns it later, sometimes hours
#: later. Rewriting the original line in place would mean read-modify-
#: write on a file the entry path also appends to; a supplement keyed by
#: the same order id avoids that entirely and keeps both facts, the
#: acceptance and the fill, with the times they were actually known.
EVIDENCE_FILL = "FILL_SETTLEMENT"


def attach_fill(*, internal_order_id, fill_price, qty_filled=None,
                fill_at=None, side="buy", trading_day, env=None,
                now=None) -> bool:
    """Record the fill for an order whose acceptance was already logged.

    `side` is part of the match, not decoration: a position's entry and
    exit can carry the same client order id, and matching on the id
    alone would let one leg's fill land on the other leg's row.
    """
    if not internal_order_id:
        return False
    return append({
        "logged_at": (now or datetime.now(timezone.utc)).isoformat(),
        "internal_order_id": internal_order_id,
        "side": str(side or "").lower(),
        "fill_price": fill_price,
        "qty_filled": qty_filled,
        "fill_at": _iso(fill_at),
        "evidence": EVIDENCE_FILL,
    }, trading_day=trading_day, env=env)


def read_merged(trading_day, *, env=None) -> List[Dict[str, Any]]:
    """Rows with their fill supplements folded in, slippage recomputed.

    Slippage can only be worked out once both halves are present: the
    signal price is known at acceptance and the fill price is not. This
    is where the two meet.
    """
    # Keyed by (order id, side). A position's entry and exit can carry
    # the SAME client order id -- the backfill produced exactly that --
    # and keying on the id alone silently drops one leg of every trade.
    base: Dict[tuple, Dict[str, Any]] = {}
    loose: List[Dict[str, Any]] = []
    for row in read(trading_day, env=env):
        order_id = row.get("internal_order_id")
        key = (order_id, row.get("side")) if order_id else None
        if row.get("evidence") == EVIDENCE_FILL:
            target = base.get(key)
            if target is None:
                # A fill whose acceptance is not in this day's file --
                # an order accepted before midnight and filled after it.
                # Kept rather than dropped; it is still evidence.
                loose.append(row)
                continue
            for field in ("fill_price", "qty_filled", "fill_at"):
                if row.get(field) is not None:
                    target[field] = row[field]
            bps, adverse = slippage_bps(signal_price=target.get("signal_price"),
                                        fill_price=target.get("fill_price"),
                                        side=target.get("side"))
            target["slippage_bps"] = bps
            target["slippage_adverse"] = adverse
            target["submit_to_fill_ms"] = _ms_between(
                _parse(target.get("submit_at")), _parse(target.get("fill_at")))
        elif key:
            base[key] = dict(row)
        else:
            loose.append(dict(row))
    return list(base.values()) + loose


def _parse(value):
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
