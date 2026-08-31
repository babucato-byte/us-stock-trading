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


#: Which half of a cycle a record describes. The watchdog reads both --
#: it wants the most recent evidence that management is happening, and a
#: cycle that has begun is exactly that. Anything counting completed
#: ticks filters on PHASE_COMPLETED.
PHASE_STARTED = "CYCLE_STARTED"
PHASE_COMPLETED = "CYCLE_COMPLETED"


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

    # Recorded BEFORE the cycle runs, not after it finishes.
    #
    # The completion record carries a `started_at` stamped here, but is
    # only appended once `run_cycle()` returns. The position watchdog
    # measures silence against the newest recorded `started_at`, so its
    # view lagged by one whole cycle -- and cycles now take 14-17
    # minutes against a 15-minute schedule.
    #
    # On 2026-08-31 that fired the kill switch on a healthy account. The
    # 14:00 cycle ran 17 minutes, so `flock -n` skipped the 14:15
    # invocation entirely; the 14:30 cycle started and was still running
    # when the watchdog checked at 14:40:08 and read 14:00:05 as the
    # newest tick -- 40.05 minutes, three seconds over the limit.
    # ENTRY_DISABLED, account-wide, while TX was being actively managed
    # by the cycle then in flight. The same false positive had already
    # happened on 2026-08-27.
    #
    # A start marker does not weaken the check. Its timestamp never
    # advances, so a cycle that HANGS still crosses the threshold and
    # still trips the watchdog -- which is the case the watchdog exists
    # for. What it stops is calling a running cycle silent.
    try:
        from scanners.base.trading_calendar import us_trading_day as _day

        record({"started_at": started.isoformat(), "trading_day": _day(),
                "phase": PHASE_STARTED, "dry_run": args.dry})
    except Exception:  # noqa: BLE001 - a heartbeat that cannot be written
        # must not stop a cycle from running; the completion record is
        # still written and the watchdog still has its old signal.
        logger.warning("could not record the cycle start marker",
                       exc_info=True)

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
                "error": f"CYCLE_FAILED: {exc}", "dry_run": args.dry,
                "phase": PHASE_COMPLETED})
        return 1

    payload = report.as_dict()
    payload["dry_run"] = args.dry
    payload["phase"] = PHASE_COMPLETED
    path = record(payload)

    logger.info("tick: session=%s orderable=%s entry=%s submitted=%s exits=%d "
                "synced=%d account=%s -> %s",
                report.market_state, report.session_orderable, report.entry_status,
                report.submitted, len(report.exits), len(report.positions_synced),
                report.account, path.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
