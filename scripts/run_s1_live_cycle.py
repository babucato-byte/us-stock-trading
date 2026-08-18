#!/usr/bin/env python3
"""One S1 live cycle. Intended for cron; safe to run by hand.

Writes one JSON line per tick to `logs/s1_live/cycles-<trading_day>.jsonl`
so a day's behaviour can be read back without re-deriving it from
scattered logs -- including the ticks where nothing happened, which are
the ones that answer "why didn't it trade today".

    --dry   run every read and gate, then stop before the entry cycle.
            No order is submitted. Use this to validate a deployment.

Exit codes: 0 normal (including "nothing to do"), 1 the cycle itself
failed. A tick that correctly declines to trade is not an error.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = os.environ.get("TRADING_PROJECT_ROOT") or str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

logger = logging.getLogger("s1_live_cycle")


def log_dir() -> Path:
    configured = os.environ.get("S1_LIVE_LOG_DIR")
    if configured:
        return Path(configured)
    return Path(ROOT) / "logs" / "s1_live"


def record(report_dict) -> Path:
    directory = log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    day = report_dict.get("trading_day") or "unknown"
    path = directory / f"cycles-{day}.jsonl"
    with path.open("a") as fh:
        fh.write(json.dumps(report_dict, default=str) + "\n")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry", action="store_true",
                        help="read and gate, but never submit an entry")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    from s1_live import executor

    started = datetime.now(timezone.utc)
    try:
        from brokers.kis_broker import KISBroker

        broker = KISBroker()
        if args.dry:
            # The dry path exercises everything EXCEPT the entry cycle:
            # session, exits, fill sync and the account reads all run for
            # real, because those are the parts a deployment can break.
            from state_store import db as state_db
            from scanners.base.trading_calendar import us_trading_day
            from live_pilot import armed

            conn = state_db.open_db()
            try:
                session = executor.resolve_session()
                report = executor.CycleReport(
                    started_at=started.isoformat(), trading_day=us_trading_day(),
                    market_state=session.name, session_orderable=session.orders_allowed)
                adapter = armed.build_adapter(broker)
                report.exits = executor.run_exit_half(
                    conn, broker=broker, broker_adapter=adapter, session=session,
                    trading_day=report.trading_day)
                report.positions_synced = executor.sync_fills(
                    conn, broker, trading_day=report.trading_day)
                report.entry_status = "DRY_RUN_ENTRY_SKIPPED"
                from s1_live import position_store as ps
                report.account = {
                    "cash_usd": broker.get_account_cash_usd(),
                    "broker_positions": len(broker.get_positions()),
                    "open_orders": len(broker.get_open_orders()),
                    "local_s1_positions": ps.live_count(conn),
                }
            finally:
                conn.close()
        else:
            report = executor.run_cycle(broker=broker)
    except Exception as exc:  # noqa: BLE001 - a cycle failure must be recorded
        logger.error("S1 live cycle failed: %s", exc, exc_info=True)
        record({"started_at": started.isoformat(), "trading_day": None,
                "error": f"CYCLE_FAILED: {exc}", "dry_run": args.dry})
        return 1

    payload = report.as_dict()
    payload["dry_run"] = args.dry
    path = record(payload)

    logger.info("tick: session=%s orderable=%s entry=%s submitted=%s exits=%d "
                "synced=%d account=%s -> %s",
                report.market_state, report.session_orderable, report.entry_status,
                report.submitted, len(report.exits), len(report.positions_synced),
                report.account, path.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
