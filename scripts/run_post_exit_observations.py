#!/usr/bin/env python3
"""Observe what the price did after each exit, on a schedule.

The gap this closes
-------------------
`post_exit/` had everything except the thing that runs it. Registration
was wired into all three position stores and worked: on 2026-08-29 the
live database held three tracking rows -- DT/SESSION_EXIT,
OWL/RANGE_REENTRY, SBS/SESSION_EXIT -- all still TRACKING, with

    post_exit_observations rows = 0

and every metric NULL. `due_for_observation` and `complete_expired` were
called from tests and nowhere else, so the analytics had a schema, a
roll-up and no data to roll up. Three real exits had gone unmeasured.

What this does NOT do
---------------------
Nothing here places an order, cancels one, or touches a position row.
It reads closed trades and writes research rows. §"Post-Exit 결과로
threshold/Exit/stop/TP 자동 변경 금지" -- this produces evidence for a
person to read, and changes no parameter on its own.

A missing price is recorded, not skipped
----------------------------------------
"Not observed yet" and "observed, and no price was available" are
different facts that look identical if the second is simply left out --
and the retry loop would then chase that horizon forever. An
unavailable price is stored as UNAVAILABLE.
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import post_exit_policy  # noqa: E402
from post_exit import observations, tracker  # noqa: E402

logger = logging.getLogger("post_exit_observations")

SOURCE = "KIS_REALTIME_BARS"


def _as_dt(stamp):
    if isinstance(stamp, datetime):
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def due_horizons(row, *, now, already):
    """Intraday horizons whose moment has passed and that are unobserved.

    Only the intraday ones. SAME_DAY_CLOSE and the next-day OHLC
    horizons are settled from daily bars, which are not this runner's
    feed -- claiming them here from an intraday price would put a
    mislabelled number in the record.
    """
    exit_at = _as_dt(row["exit_time"])
    if exit_at is None:
        return []
    due = []
    for name, minutes in post_exit_policy.INTRADAY_HORIZONS:
        if name in already:
            continue
        if exit_at + timedelta(minutes=minutes) <= now:
            due.append((name, minutes))
    return due


def observe_row(conn, row, *, price_lookup, now):
    """Record every horizon that has come due for one tracked exit."""
    tracking_id = row["tracking_id"]
    already = {o["horizon"] for o in observations.observations_for(conn, tracking_id)}
    recorded = 0
    for horizon, minutes in due_horizons(row, now=now, already=already):
        price, detail = price_lookup(row["symbol"], _as_dt(row["exit_time"]),
                                     minutes)
        observations.record(
            conn, tracking_id=tracking_id, horizon=horizon, price=price,
            source=SOURCE, detail=detail, now=now,
            status=(post_exit_policy.OBSERVATION_OK if price is not None
                    else post_exit_policy.OBSERVATION_UNAVAILABLE))
        recorded += 1
        logger.info("%s %s %s price=%s %s", row["symbol"], row["exit_reason"],
                    horizon, price, detail or "")
    return recorded


#: How far from the target moment a bar may sit and still answer for it.
#:
#: A bar two minutes either side of the +15m mark is a fair reading of
#: where the price was. One forty minutes away is a different fact
#: wearing the same label, and the whole value of these horizons is that
#: they are comparable across trades -- so beyond this, the answer is
#: UNAVAILABLE rather than the nearest thing to hand.
NEAREST_BAR_TOLERANCE_MINUTES = 2.0


def _bar_price_lookup(env=None):
    """A price at a moment, from the bars the collector already stored.

    Deliberately the collector's snapshot rather than a fresh broker
    call: this runs on a research schedule and must add nothing to the
    order path's rate limiter, which is what starved S1 once already.
    """
    from s6_live import kis_bar_features

    def _bars_for(symbol, day):
        found = []
        for session in ("PREMARKET", "REGULAR", "AFTER_HOURS",
                        "OVERNIGHT_DAYTIME"):
            try:
                store = kis_bar_features.load_store(session, day, env=env)
            except Exception:  # noqa: BLE001
                continue
            if store is None:
                continue
            found.extend(store.bars(symbol, session))
        return found

    def lookup(symbol, exit_at, minutes):
        if exit_at is None:
            return None, "no exit timestamp"
        target = exit_at + timedelta(minutes=minutes)
        # The target can fall on the day after the exit.
        days = {exit_at.strftime("%Y-%m-%d"), target.strftime("%Y-%m-%d")}
        bars = [b for day in sorted(days) for b in _bars_for(symbol, day)]
        if not bars:
            return None, "no collected bars for that symbol"
        nearest = min(bars, key=lambda b: abs((b.minute - target).total_seconds()))
        drift = abs((nearest.minute - target).total_seconds()) / 60.0
        if drift > NEAREST_BAR_TOLERANCE_MINUTES:
            return None, f"nearest bar is {drift:.0f}m from the mark"
        return float(nearest.close), None

    return lookup


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Record post-exit price observations")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level="INFO",
                        format="%(asctime)s %(levelname)s %(message)s")

    from state_store import db

    now = datetime.now(timezone.utc)
    conn = db.open_db()

    rows = tracker.due_for_observation(conn, now=now)
    logger.info("%d exit(s) inside their tracking window", len(rows))
    if args.dry_run:
        for row in rows:
            already = {o["horizon"]
                       for o in observations.observations_for(conn, row["tracking_id"])}
            due = [h for h, _m in due_horizons(row, now=now, already=already)]
            logger.info("  %s %s observed=%s due=%s", row["symbol"],
                        row["exit_reason"], sorted(already), due)
        return 0

    lookup = _bar_price_lookup()
    recorded = sum(observe_row(conn, row, price_lookup=lookup, now=now)
                   for row in rows)
    expired = tracker.complete_expired(conn, now=now)
    logger.info("recorded %d observation(s); completed %d expired window(s)",
                recorded, expired)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
