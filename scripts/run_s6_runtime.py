#!/usr/bin/env python3
"""One S6 runtime tick. Orchestration only; owns no safety decision.

Exits, fill synchronisation and the latched-exit retry run in EVERY
session -- a held position must be evaluated whatever the clock says.
Only SUBMISSION is restricted, and the restriction is read from the
session matrix rather than decided here.

Entry is deliberately absent. S6 joins the shared BUY cycle through its
candidate source; this tick manages what is already held.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger("s6_runtime")


def run_once(*, now=None) -> dict:
    from config import s6_sessions
    from market_hours import EASTERN, get_market_state
    from scanners.base import scan_session
    from state_store.db import open_db

    moment = now or datetime.now(timezone.utc)
    market = get_market_state()
    session = scan_session.session_at(moment.astimezone(EASTERN))
    orders_allowed = (market != "CLOSED"
                      and s6_sessions.orders_allowed(session))

    report = {"started_at": moment.isoformat(), "session": session,
              "variant": s6_sessions.variant_for(session),
              "market_state": market, "orders_allowed": orders_allowed,
              "mode": s6_sessions.mode_for(session),
              "buy_fills": [], "exits": [], "retried": [], "sell_fills": [],
              "errors": []}

    with open_db() as conn:
        from s6_live import position_store

        if not position_store.load_live(conn) and \
                not position_store.load_unconfirmed(conn):
            report["status"] = "NO_S6_POSITIONS"
            _attach_session_report(report, conn=conn, session=session,
                                   now=moment)
            return report

        from s6_live import exit_runtime

        adapter, features_fn, price_fn, broker = _dependencies()
        # ONE open-order sweep for the whole tick, shared by both fill
        # lookups. Reading it per position would multiply broker calls by
        # the position count for an answer that cannot change mid-tick.
        # A failed read is None, and `kis_fill_inquiry` treats that as
        # "cannot tell" -- which declines to abandon anything.
        try:
            open_orders = broker.get_open_orders()
        except Exception:  # noqa: BLE001
            logger.warning("S6 open-order sweep failed; fill inquiries will "
                           "not treat an unfilled order as terminal",
                           exc_info=True)
            open_orders = None

        for stage, call in (
            ("buy_fills", lambda: exit_runtime.sync_buy_fills(
                conn, fills_for=_buy_fill_lookup(
                    conn, broker, now=moment, open_orders=open_orders),
                now=moment)),
            ("exits", lambda: exit_runtime.run_exits(
                conn, broker_adapter=adapter, features_fn=features_fn,
                price_fn=price_fn, session=session, now=moment,
                orders_allowed=orders_allowed)),
            ("retried", lambda: exit_runtime.retry_latched_exits(
                conn, broker_adapter=adapter, session=session, now=moment,
                orders_allowed=orders_allowed)),
            ("sell_fills", lambda: exit_runtime.sync_sell_fills(
                conn, fills_for=_sell_fill_lookup(
                    conn, broker, now=moment, open_orders=open_orders),
                now=moment)),
        ):
            try:
                report[stage] = call()
            except Exception as exc:  # noqa: BLE001 - one stage failing
                # must not cost the others; an exit that was never
                # evaluated looks exactly like one that held.
                logger.error("S6 %s stage failed", stage, exc_info=True)
                report["errors"].append(f"{stage}: {exc}")
        report["status"] = "ERROR" if report["errors"] else "OK"
        _attach_session_report(report, conn=conn, session=session, now=moment)
    return report


def _attach_session_report(report, *, conn, session, now) -> None:
    """Generate the shadow-session report automatically, on the tick.

    Only for the sessions that CANNOT order. S6-R has its own report --
    the final check -- which asks a different question and needs a broker
    to answer most of it; running it from every tick would open an
    account connection four times an hour to print a table.

    Attached rather than printed separately so one tick produces one
    record. Guarded because a report must never be able to fail the
    runtime that carries it: the exits already ran by the time this is
    reached, and losing them to a formatting error would be absurd.
    """
    from config import s6_sessions

    if not s6_sessions.scans(session) or s6_sessions.orders_allowed(session):
        return
    try:
        from s6_live import session_report

        report["session_report"] = session_report.build(
            conn=conn, session=session, now=now, runtime_report=report)
    except Exception as exc:  # noqa: BLE001
        logger.warning("S6 session report could not be built", exc_info=True)
        # Deliberately NOT `report["errors"]`. That list is what sets
        # `status` to ERROR, and it means "a trading stage failed". A
        # report that would not render is not a runtime fault, and
        # letting it flip the tick's status would train an operator to
        # ignore the one field that matters.
        report["session_report_error"] = str(exc)


def _dependencies():
    from brokers.kis_broker import KISBroker
    from live_pilot import armed
    from s1_live import executor as s1_executor

    broker = KISBroker()
    return (armed.build_adapter(broker), s1_executor.make_features_fn(),
            s1_executor.make_price_fn(broker), broker)


def _order_id_for(conn, row, *, side):
    """The KIS order number for this position's BUY or SELL.

    The store keeps the BUY's broker id on the row once a fill lands, but
    a SUBMITTED position has only its client order id -- which is exactly
    the row this lookup exists to resolve. So the ledgers are consulted:
    `kis_order_idempotency` for the entry, `exit_intents` for the exit.
    Both are keyed on the client id the submitter minted, so an ambiguous
    submission that never got a response is still findable.
    """
    if side == "buy":
        recorded = row.get("entry_order_id")
        if recorded:
            return recorded, row.get("submitted_at")
        client = row.get("client_order_id")
        if not client:
            return None, row.get("submitted_at")
        found = conn.execute(
            "SELECT broker_order_id, created_at FROM kis_order_idempotency "
            "WHERE internal_order_id = ?", (client,)).fetchone()
        return ((found["broker_order_id"], found["created_at"]) if found
                else (None, row.get("submitted_at")))

    found = conn.execute(
        "SELECT broker_order_id, created_at FROM exit_intents "
        "WHERE position_id = ? AND broker_order_id IS NOT NULL "
        "ORDER BY created_at DESC LIMIT 1", (row.get("position_id"),)
    ).fetchone()
    if found:
        return found["broker_order_id"], found["created_at"]
    return None, row.get("updated_at")


def _fill_lookup(conn, broker, *, side, now, open_orders=None):
    """A callable `exit_runtime` can hand one position row at a time.

    Session-independent by construction: an order id is an order id, so
    the same lookup answers for a PREMARKET entry sold in REGULAR. It
    never inspects the variant.
    """
    from brokers import kis_fill_inquiry
    from s1_live.freshness import as_utc

    def lookup(row):
        order_id, since = _order_id_for(conn, row, side=side)
        report = kis_fill_inquiry.inquire(
            broker, broker_order_id=order_id, symbol=row.get("symbol"),
            side=side, ordered_quantity=row.get("quantity"),
            now=now, since=as_utc(since), open_orders=open_orders)
        if not report.usable:
            # UNKNOWN is not "nothing filled". Returning None leaves the
            # position exactly as it was, which is the only safe answer
            # when the broker could not be asked.
            logger.warning("S6 %s fill inquiry unusable for %s: %s", side,
                           row.get("symbol"), report.detail)
        return report.as_store_fill()
    return lookup


def _buy_fill_lookup(conn, broker, *, now, open_orders=None):
    return _fill_lookup(conn, broker, side="buy", now=now,
                        open_orders=open_orders)


def _sell_fill_lookup(conn, broker, *, now, open_orders=None):
    return _fill_lookup(conn, broker, side="sell", now=now,
                        open_orders=open_orders)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        report = run_once()
    except Exception as exc:  # noqa: BLE001 - a crashed tick must still
        # leave a record; silence is indistinguishable from not firing.
        logger.error("S6 runtime failed", exc_info=True)
        report = {"status": "ERROR", "error": str(exc),
                  "started_at": datetime.now(timezone.utc).isoformat()}
    print(json.dumps(report, default=str))
    return 0 if report.get("status") != "ERROR" else 1


if __name__ == "__main__":
    raise SystemExit(main())
