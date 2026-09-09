#!/usr/bin/env python3
"""Post the daily trading report to sotck-trading-report. Once per day.

    scripts/run_daily_trading_report.py                    # today's trading day
    scripts/run_daily_trading_report.py --trading-day 2026-09-08 --print

Intended schedule: after the last enabled session of the trading day has
closed (after-hours ends 20:00 ET), so every SELL of the day is in the
book. Sent once per day through the notification ledger. Read-only
against the state store apart from that claim.
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.secret_redaction import install_logging_redaction  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _trading_day():
    from scanners.base.trading_calendar import us_trading_day

    return us_trading_day()


def main(argv=None) -> int:
    install_logging_redaction()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trading-day", default=None)
    parser.add_argument("--print", action="store_true", dest="print_only")
    args = parser.parse_args(argv)
    from operations import daily_trading_report as dtr
    from state_store import db as state_db

    day = args.trading_day or _trading_day()
    conn = state_db.open_db()
    try:
        report = dtr.build(conn, day)
        message = dtr.format_message(report)
        print(message)
        if args.print_only:
            return 0
        from operations import notification_ledger as ledger
        import slack_utils

        key = ledger.key_for("DAILY_TRADING_REPORT", subject_id=day)
        if not ledger.claim(conn, key, event_type="DAILY_TRADING_REPORT",
                            subject_id=day, channel="TRADING_REPORT"):
            print(f"daily trading report for {day} already sent")
            return 0
        slack_utils.send_trading_report_message(message)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
