"""Record what the price did after an exit, and roll it up.

Separation this file exists to keep
-----------------------------------
Collecting these prices is research. It runs on whatever schedule
suits it, against whatever feed is available, and when the feed is down
the answer is a row saying UNAVAILABLE. No caller of this module is on
the order path, and `record` never raises: a trade's lifecycle must be
identical whether or not anybody is studying it afterwards.

Why UNAVAILABLE is stored rather than skipped
---------------------------------------------
"Not observed yet" and "observed, and there was no price" look the same
if the second is simply omitted, and the retry loop would then chase a
horizon forever. They are different facts and are stored differently.

Rolled-up metrics
-----------------
`exit_mfe_pct` -- how much further the trade could have run after the
sale. Positive means selling early left money behind.

`avoided_loss_pct` -- how far it fell after the sale. Positive means the
exit prevented that much loss.

Both are stated from the EXIT price, because the question is about the
exit and not about the trade.
"""

import logging
from datetime import datetime, timezone

from config import post_exit_policy

logger = logging.getLogger(__name__)


def _now(now=None):
    return now or datetime.now(timezone.utc)


def record(conn, *, tracking_id, horizon, price=None, observed_at=None,
           source=None, status=None, detail=None, now=None) -> bool:
    """Store one horizon observation. Never raises.

    Idempotent per (tracking_id, horizon): observing a moment that has
    already passed twice is the same fact, not a new one.
    """
    if horizon not in post_exit_policy.ALL_HORIZONS:
        logger.warning("unknown post-exit horizon %r", horizon)
        return False
    current = _now(now)
    resolved = status or (post_exit_policy.OBSERVATION_OK if price is not None
                          else post_exit_policy.OBSERVATION_UNAVAILABLE)
    try:
        exit_price = conn.execute(
            "SELECT exit_price FROM post_exit_tracking WHERE tracking_id = ?",
            (tracking_id,)).fetchone()
        if exit_price is None:
            return False
        base = exit_price[0]
        return_pct = None
        if price is not None and base:
            return_pct = (float(price) / float(base) - 1.0) * 100.0
        conn.execute(
            "INSERT OR REPLACE INTO post_exit_observations ("
            "tracking_id, horizon, observed_at, price, return_pct, source, "
            "status, detail, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (tracking_id, horizon,
             (observed_at or current).isoformat()
             if hasattr(observed_at or current, "isoformat") else observed_at,
             price, return_pct, source, resolved, detail, current.isoformat()))
        conn.commit()
        _roll_up(conn, tracking_id=tracking_id, now=current)
        return True
    except Exception:  # noqa: BLE001 - research bookkeeping
        logger.warning("post-exit observation could not be recorded for %s/%s",
                       tracking_id, horizon, exc_info=True)
        return False


def _roll_up(conn, *, tracking_id, now):
    """Recompute the summary columns from every usable observation."""
    rows = conn.execute(
        "SELECT price, return_pct FROM post_exit_observations "
        "WHERE tracking_id = ? AND status = ? AND price IS NOT NULL",
        (tracking_id, post_exit_policy.OBSERVATION_OK)).fetchall()
    prices = [float(r[0]) for r in rows if r[0] is not None]
    returns = [float(r[1]) for r in rows if r[1] is not None]
    if not prices:
        return
    max_price, min_price = max(prices), min(prices)
    max_ret = max(returns) if returns else None
    min_ret = min(returns) if returns else None
    # Only the favourable side counts as forgone upside, and only the
    # adverse side as avoided loss. A trade that only ever went up has
    # avoided_loss_pct 0, not a negative number.
    mfe = max(max_ret, 0.0) if max_ret is not None else None
    avoided = abs(min(min_ret, 0.0)) if min_ret is not None else None
    conn.execute(
        "UPDATE post_exit_tracking SET max_price_after_exit = ?, "
        "min_price_after_exit = ?, max_return_after_exit_pct = ?, "
        "min_return_after_exit_pct = ?, exit_mfe_pct = ?, "
        "avoided_loss_pct = ?, updated_at = ? WHERE tracking_id = ?",
        (max_price, min_price, max_ret, min_ret, mfe, avoided,
         now.isoformat(), tracking_id))
    conn.commit()


def observations_for(conn, tracking_id):
    try:
        return conn.execute(
            "SELECT horizon, price, return_pct, status, observed_at "
            "FROM post_exit_observations WHERE tracking_id = ? "
            "ORDER BY observation_id", (tracking_id,)).fetchall()
    except Exception:  # noqa: BLE001
        logger.warning("post-exit observations unreadable for %s", tracking_id,
                       exc_info=True)
        return []
