#!/usr/bin/env python3
"""Post the S1-S5 daily scanner summary to stock-sanner. Once per day.

    scripts/run_daily_scanner_summary.py                  # today's trading day
    scripts/run_daily_scanner_summary.py --trading-day 2026-09-08 --print

Intended schedule: after the forward-outcome tracker has run for the day
(the release `run_scanner_performance.py` job at 18:33 ET), so the
intraday horizons exist. A message for a day is sent once; the
notification ledger remembers.

Read-only: it reads the analytics tree and s1_live_trades and writes
only the ledger claim.
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.secret_redaction import install_logging_redaction  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("daily_scanner_summary")


def _trading_day():
    from scanners.base.trading_calendar import us_trading_day

    return us_trading_day()


def main(argv=None) -> int:
    install_logging_redaction()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trading-day", default=None)
    parser.add_argument("--print", action="store_true", dest="print_only")
    args = parser.parse_args(argv)
    from scanners.analytics import daily_scanner_summary as dss

    day = args.trading_day or _trading_day()
    conn = None
    try:
        from state_store import db as state_db

        conn = state_db.open_db()
    except Exception:  # noqa: BLE001
        conn = None
    try:
        summary = {"trading_day": day, "sessions": dss.build_by_session(day, conn=conn)}
        message = dss.format_message(summary)
        print(message)
        if args.print_only:
            return 0
        from operations import notification_ledger as ledger
        import slack_utils

        if conn is not None:
            key = ledger.key_for("DAILY_SCANNER_SUMMARY", subject_id=day)
            if not ledger.claim(conn, key, event_type="DAILY_SCANNER_SUMMARY",
                                subject_id=day, channel="SCANNER"):
                print(f"daily scanner summary for {day} already sent")
                return 0
        slack_utils.send_scanner_monitor_message(message)
        return 0
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
