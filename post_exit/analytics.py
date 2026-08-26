"""Exit quality, per strategy and per exit reason -- never pooled.

Why the grouping is not negotiable
----------------------------------
"Our exits give back 1.4% on average" is a number about nothing. S6's
RANGE_REENTRY fires when an intraday breakout fails; S1's structure exit
fires when a multi-day trend breaks. Pooling them produces an average
that describes neither, and acting on it would change a rule using
evidence from a different strategy's trades.

Every figure this module produces is keyed by (strategy_id,
exit_reason). There is deliberately no "all strategies" total.

What the numbers are for
------------------------
  exit_mfe          how much was left behind -- large means selling early
  avoided_loss      how much was dodged -- large means the rule earned its keep
  reentry_share     how often price recovered past the exit
  further_fall_share how often it kept falling

`interpretation` comes from `config/post_exit_policy` and is the sample
size stated in words. Nothing here changes a threshold; see §M.
"""

import logging
import statistics
from typing import Optional

from config import post_exit_policy

logger = logging.getLogger(__name__)


def _median(values):
    return statistics.median(values) if values else None


def _mean(values):
    return (sum(values) / len(values)) if values else None


def _share(values, predicate):
    if not values:
        return None
    return sum(1 for v in values if predicate(v)) / len(values)


def summarise(conn, *, strategy_id=None, exit_reason=None,
              include_incomplete=False):
    """Per (strategy_id, exit_reason) exit-quality statistics.

    `include_incomplete` folds in rows still being tracked. Off by
    default: a window that has not finished has not produced its worst
    or best moment yet, and mixing partial rows into a median moves it
    for a reason that has nothing to do with the exits.
    """
    where, params = [], []
    if not include_incomplete:
        where.append("status = ?")
        params.append(post_exit_policy.STATUS_COMPLETED)
    if strategy_id:
        where.append("strategy_id = ?")
        params.append(strategy_id)
    if exit_reason:
        where.append("exit_reason = ?")
        params.append(exit_reason)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    try:
        rows = conn.execute(
            "SELECT tracking_id, strategy_id, exit_reason, realized_pnl_pct, "
            "exit_mfe_pct, avoided_loss_pct, max_return_after_exit_pct, "
            "min_return_after_exit_pct FROM post_exit_tracking" + clause,
            params).fetchall()
    except Exception:  # noqa: BLE001
        logger.warning("post-exit tracking unreadable", exc_info=True)
        return []

    grouped = {}
    for row in rows:
        key = (row["strategy_id"], row["exit_reason"])
        grouped.setdefault(key, []).append(row)

    out = []
    for (strategy, reason), group in sorted(
            grouped.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        mfe = [r["exit_mfe_pct"] for r in group if r["exit_mfe_pct"] is not None]
        avoided = [r["avoided_loss_pct"] for r in group
                   if r["avoided_loss_pct"] is not None]
        ups = [r["max_return_after_exit_pct"] for r in group
               if r["max_return_after_exit_pct"] is not None]
        downs = [r["min_return_after_exit_pct"] for r in group
                 if r["min_return_after_exit_pct"] is not None]
        realized = [r["realized_pnl_pct"] for r in group
                    if r["realized_pnl_pct"] is not None]
        out.append({
            "strategy_id": strategy,
            "exit_reason": reason,
            "sample_count": len(group),
            "interpretation": post_exit_policy.interpretation_for(len(group)),
            "avg_realized_pnl_pct": _mean(realized),
            "avg_exit_mfe": _mean(mfe),
            "median_exit_mfe": _median(mfe),
            "avg_avoided_loss": _mean(avoided),
            "median_avoided_loss": _median(avoided),
            # How often the exit was followed by a recovery past it, and
            # how often by a further fall. Thresholded at zero rather
            # than at a "significant" move: the question is direction.
            "reentry_share": _share(ups, lambda v: v > 0),
            "further_fall_share": _share(downs, lambda v: v < 0),
        })
    return out


def horizon_performance(conn, *, strategy_id=None, exit_reason=None):
    """Mean return at each horizon, still grouped by strategy+reason."""
    try:
        rows = conn.execute(
            "SELECT t.strategy_id, t.exit_reason, o.horizon, o.return_pct "
            "FROM post_exit_observations o "
            "JOIN post_exit_tracking t ON t.tracking_id = o.tracking_id "
            "WHERE o.status = ? AND o.return_pct IS NOT NULL",
            (post_exit_policy.OBSERVATION_OK,)).fetchall()
    except Exception:  # noqa: BLE001
        logger.warning("post-exit observations unreadable", exc_info=True)
        return {}

    grouped = {}
    for row in rows:
        if strategy_id and row["strategy_id"] != strategy_id:
            continue
        if exit_reason and row["exit_reason"] != exit_reason:
            continue
        key = (row["strategy_id"], row["exit_reason"], row["horizon"])
        grouped.setdefault(key, []).append(float(row["return_pct"]))
    return {key: {"n": len(vals), "avg_return_pct": _mean(vals),
                  "median_return_pct": _median(vals)}
            for key, vals in sorted(grouped.items(), key=lambda kv: str(kv[0]))}


def reentry_block_report(conn, *, trading_day=None):
    """Every same-day re-entry block, with what the exit did afterwards.

    §N: the block prevents a trade, and whether that was right is only
    answerable from the price after the exit that caused it. Joined to
    the tracking row so the counterfactual is one query rather than a
    reconstruction.
    """
    clause, params = "", []
    if trading_day:
        clause = " WHERE b.trading_day = ?"
        params.append(trading_day)
    try:
        return conn.execute(
            "SELECT b.strategy_id, b.symbol, b.trading_day, b.blocked_at, "
            "b.candidate_rank, b.candidate_score, b.candidate_price, "
            "b.previous_exit_price, b.previous_exit_reason, "
            "t.max_return_after_exit_pct, t.min_return_after_exit_pct, "
            "t.exit_mfe_pct, t.avoided_loss_pct, t.status AS tracking_status "
            "FROM reentry_blocks b "
            "LEFT JOIN post_exit_tracking t "
            "  ON t.position_id = b.previous_position_id" + clause,
            params).fetchall()
    except Exception:  # noqa: BLE001
        logger.warning("re-entry block report unreadable", exc_info=True)
        return []
