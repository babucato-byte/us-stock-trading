#!/usr/bin/env python3
"""Compute forward returns and MFE/MAE for recorded signals (sections 12-13).

Run daily, after the close. It re-tracks a WINDOW of recent days rather
than only the newest one, because a signal's 3- and 5-day horizons do
not exist until several sessions later -- a tracker that only ever
looked at today would leave every multi-day column permanently null.

Records are appended and the newest per signal wins on read, so running
this repeatedly is safe and converges: each run fills in whatever
horizons have matured since the last one.

    scripts/run_scanner_performance.py                # last 10 recorded days
    scripts/run_scanner_performance.py --days 30
    scripts/run_scanner_performance.py --trading-day 2026-08-12
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanners.analytics import performance_tracker  # noqa: E402
from scanners.base import result_store  # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trading-day", default=None,
                        help="track exactly this day instead of a recent window")
    parser.add_argument("--days", type=int, default=10,
                        help="how many recent recorded days to re-track (default 10)")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute and print without writing to the analytics store")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.trading_day:
        records = performance_tracker.track_day(
            args.trading_day, store=not args.dry_run)
        print(f"{args.trading_day}: {len(records)} signals tracked")
        return 0

    days = result_store.available_trading_days()
    if not days:
        print("no scanner signals recorded yet; nothing to track")
        return 0

    processed = performance_tracker.track_recent(
        days=args.days, store=not args.dry_run)
    for day in sorted(processed):
        print(f"{day}: {processed[day]} signals tracked")
    print(f"\n{sum(processed.values())} signal records across {len(processed)} days"
          + ("  (dry run, nothing written)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
