#!/usr/bin/env python3
"""Reconstruct slippage records for trades that already happened.

Only from evidence that exists. The order events cannot support
latencies -- four transitions share the cycle's start timestamp and the
FILLED transition is when reconciliation noticed, not when the fill
occurred -- so every latency here is UNKNOWN and says so.

What IS real: the filled price (the broker's own average), the
quantity, the broker order id, the session and the exit reason. Those
are recorded; everything else is left empty rather than reconstructed,
because a plausible number in an execution-quality log is worse than a
gap. The gap can be fixed by collecting better data; a wrong number gets
argued with.
"""

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s6_live import slippage_log  # noqa: E402

logger = logging.getLogger("backfill_slippage")

EVIDENCE = "BACKFILL_FROM_POSITION_STORE"


def rows_for(conn, *, strategy_id="S6_ORB_BREAKOUT_V1"):
    """Every closed S6 position, as the two orders that made it."""
    found = conn.execute(
        "SELECT symbol, quantity, entry_price, exit_price, entry_session, "
        "exit_session, exit_reason, entry_time, closed_at, entry_order_id, "
        "client_order_id FROM s6_positions WHERE status = 'CLOSED' "
        "AND entry_price IS NOT NULL").fetchall()
    out = []
    for row in found:
        data = dict(row)
        common = {
            "symbol": data["symbol"],
            "strategy_id": strategy_id,
            "qty_requested": data.get("quantity"),
            "qty_filled": data.get("quantity"),
            "evidence": EVIDENCE,
        }
        # The BUY. `entry_price` is the broker's own average fill.
        out.append(dict(common, side="buy",
                        session=data.get("entry_session"),
                        fill_price=data.get("entry_price"),
                        broker_order_id=data.get("entry_order_id"),
                        internal_order_id=data.get("client_order_id"),
                        fill_at=data.get("entry_time")))
        # The SELL, when there was one. Deliberately WITHOUT an order
        # id: `client_order_id` names the ENTRY order, and reusing it
        # here would make the two legs indistinguishable to any reader
        # matching on it. The exit's own order id was never recorded.
        if data.get("exit_price") is not None:
            out.append(dict(common, side="sell",
                            session=data.get("exit_session"),
                            fill_price=data.get("exit_price"),
                            exit_reason=data.get("exit_reason"),
                            fill_at=data.get("closed_at")))
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description="Backfill slippage records")
    parser.add_argument("--trading-day", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")

    from state_store import db

    conn = db.open_db()
    written = 0
    for fields in rows_for(conn):
        record = slippage_log.build_record(**fields)
        # Stated plainly rather than implied by empty fields.
        logger.info("%-5s %-4s fill=%-10s qty=%-3s slippage=%s (no signal "
                    "price on record) latencies=UNKNOWN",
                    record["symbol"], record["side"], record["fill_price"],
                    record["qty_filled"], record["slippage_bps"])
        if not args.dry_run:
            written += int(slippage_log.append(record,
                                               trading_day=args.trading_day))
    logger.info("%d records %s", written,
                "would be written" if args.dry_run else "written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
