"""Why was this bought? In one row.

The post-mortem this exists for
-------------------------------
Reconstructing the DT entry took four sources -- a candidate JSONL, the
shadow audit trail, the order ledger and the position row -- and the
fact that mattered most was in none of them: the candidate's market data
was hours older than its `generated_at`. Both timestamps are columns
here for exactly that reason. A record that cannot express the failure
it is meant to explain is not a record.

Written, never read by the order path
-------------------------------------
`record()` is called after a decision has been made and never returns
anything the entry path consults. It cannot block an order, and every
failure inside it is swallowed: a trade must not depend on its own
paperwork.
"""

import json
import logging
import uuid

from domain.candidate_state import READY_TO_BUY
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _json(value) -> Optional[str]:
    if value is None:
        return None
    try:
        return json.dumps(value, default=str, sort_keys=True)
    except Exception:  # noqa: BLE001
        return None


def record(conn, *, symbol, strategy_id, internal_order_id=None,
           broker_order_id=None, position_id=None, strategy_version=None,
           scan_id=None, generation_id=None, candidate_id=None,
           session=None, trading_day=None, rank=None, score=None,
           candidate_generated_at=None, market_data_asof=None,
           ready_evaluated_at=None, watch_state=None, watch_conditions=None,
           gate_results=None, order_price=None, quantity=None,
           scanner_id=None, watch_started_at=None, ready_at=None,
           execution_gate_at=None, broker_fill_time=None,
           candidate_state=None, full_scan_started_at=None,
           symbol_evaluated_at=None, candidate_discovered_at=None,
           watchlist_added_at=None, fast_watch_evaluated_at=None,
           candidate_published_at=None, source_consumed_at=None,
           precision_watch_started_at=None, broker_submit_at=None,
           fill_at=None, now=None) -> Optional[str]:
    """Record one order's provenance. Never raises."""
    current = now or datetime.now(timezone.utc)
    try:
        from market_hours import us_trading_day

        lineage_id = f"lin_{uuid.uuid4().hex[:16]}"
        values = {
            "lineage_id": lineage_id, "internal_order_id": internal_order_id,
            "broker_order_id": broker_order_id, "position_id": position_id,
            "strategy_id": strategy_id, "strategy_version": strategy_version,
            "scan_id": scan_id, "generation_id": generation_id,
            "candidate_id": candidate_id, "symbol": str(symbol or "").upper(),
            "session": session, "trading_day": trading_day or us_trading_day(current),
            "rank": rank, "score": score,
            "candidate_generated_at": candidate_generated_at,
            "market_data_asof": market_data_asof,
            "ready_evaluated_at": ready_evaluated_at,
            "watch_state": watch_state, "watch_conditions": _json(watch_conditions),
            "gate_results": _json(gate_results), "order_price": order_price,
            "quantity": quantity, "created_at": current.isoformat(),
            "scanner_id": scanner_id, "watch_started_at": watch_started_at,
            "ready_at": ready_at, "execution_gate_at": execution_gate_at,
            "broker_fill_time": broker_fill_time,
            "candidate_state": candidate_state,
            "full_scan_started_at": full_scan_started_at,
            "symbol_evaluated_at": symbol_evaluated_at,
            "candidate_discovered_at": candidate_discovered_at,
            "watchlist_added_at": watchlist_added_at,
            "fast_watch_evaluated_at": fast_watch_evaluated_at,
            "candidate_published_at": candidate_published_at,
            "source_consumed_at": source_consumed_at,
            "precision_watch_started_at": precision_watch_started_at,
            "broker_submit_at": broker_submit_at, "fill_at": fill_at,
        }
        columns = list(values)
        conn.execute(
            "INSERT INTO order_lineage (%s) VALUES (%s)" % (
                ", ".join(columns), ", ".join("?" for _ in columns)),
            tuple(values[name] for name in columns))
        conn.commit()
        return lineage_id
    except Exception:  # noqa: BLE001 - a trade must not depend on its
        # own paperwork.
        logger.warning("could not record order lineage for %s", symbol,
                       exc_info=True)
        return None


def from_watch(evaluation, *, candidate=None) -> Dict[str, Any]:
    """The lineage fields a watch evaluation already knows.

    Pulled out so a caller records the SAME values the watch decided on,
    rather than re-deriving them a second time and possibly differently.
    """
    features = getattr(evaluation, "features", None)
    row = dict(candidate or {})
    provenance = dict(row.get("provenance") or {})
    return {
        "session": getattr(evaluation, "session", None),
        "rank": row.get("rank"),
        "score": row.get("score"),
        "candidate_generated_at": row.get("generated_at"),
        # The distinction the DT entry turned on: when the candidate was
        # PUBLISHED versus when the market it describes was last seen.
        "market_data_asof": (features.market_data_asof.isoformat()
                             if features is not None
                             and features.market_data_asof else None),
        "ready_evaluated_at": (evaluation.evaluated_at.isoformat()
                               if getattr(evaluation, "evaluated_at", None)
                               else None),
        "watch_state": getattr(evaluation, "state", None),
        "watch_conditions": dict(getattr(evaluation, "conditions", {}) or {}),
        # The moment the STRATEGY said yes. Distinct from when the
        # execution gate ran and from when the broker filled -- §27 asks
        # whether a READY candidate then waited on something it should
        # not have, and that question needs all three.
        "ready_at": (evaluation.evaluated_at.isoformat()
                     if getattr(evaluation, "state", None) == READY_TO_BUY
                     and getattr(evaluation, "evaluated_at", None) else None),
        "candidate_state": getattr(evaluation, "state", None),
        "full_scan_started_at": provenance.get("full_scan_started_at"),
        "symbol_evaluated_at": provenance.get("symbol_evaluated_at"),
        "candidate_discovered_at": provenance.get("candidate_discovered_at"),
        "watchlist_added_at": provenance.get("watchlist_added_at"),
        "fast_watch_evaluated_at": provenance.get("fast_watch_evaluated_at"),
        "candidate_published_at": provenance.get("candidate_published_at"),
        "source_consumed_at": provenance.get("source_consumed_at"),
        "precision_watch_started_at": provenance.get("precision_watch_started_at"),
    }


def explain(conn, *, symbol=None, internal_order_id=None, trading_day=None):
    """Every recorded BUY, newest first, for a report or a post-mortem."""
    where, params = [], []
    if symbol:
        where.append("symbol = ?")
        params.append(str(symbol).upper())
    if internal_order_id:
        where.append("internal_order_id = ?")
        params.append(internal_order_id)
    if trading_day:
        where.append("trading_day = ?")
        params.append(trading_day)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    try:
        return conn.execute(
            "SELECT * FROM order_lineage" + clause +
            " ORDER BY created_at DESC", params).fetchall()
    except Exception:  # noqa: BLE001
        logger.warning("order lineage unreadable", exc_info=True)
        return []
