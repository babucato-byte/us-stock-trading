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
           now=None) -> Optional[str]:
    """Record one order's provenance. Never raises."""
    current = now or datetime.now(timezone.utc)
    try:
        from market_hours import us_trading_day

        lineage_id = f"lin_{uuid.uuid4().hex[:16]}"
        conn.execute(
            "INSERT INTO order_lineage ("
            "lineage_id, internal_order_id, broker_order_id, position_id, "
            "strategy_id, strategy_version, scan_id, generation_id, "
            "candidate_id, symbol, session, trading_day, rank, score, "
            "candidate_generated_at, market_data_asof, ready_evaluated_at, "
            "watch_state, watch_conditions, gate_results, order_price, "
            "quantity, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (lineage_id, internal_order_id, broker_order_id, position_id,
             strategy_id, strategy_version, scan_id, generation_id,
             candidate_id, str(symbol or "").upper(), session,
             trading_day or us_trading_day(current), rank, score,
             candidate_generated_at, market_data_asof, ready_evaluated_at,
             watch_state, _json(watch_conditions), _json(gate_results),
             order_price, quantity, current.isoformat()))
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
