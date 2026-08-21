#!/usr/bin/env python3
"""Publish the S1 Limited Live candidate set for a trading day.

    scripts/run_s1_publisher.py                      # today
    scripts/run_s1_publisher.py --trading-day 2026-08-17
    scripts/run_s1_publisher.py --limit 5
    scripts/run_s1_publisher.py --dry-run            # build and print, publish nothing

Reads the scanner analytics store and writes ONLY
`s1_live_candidates.csv` and its manifest. It does not read or write
`order_candidates.csv`, does not touch the Candidate Decision layer, and
places no order -- publishing a candidate set changes nothing about
whether an order can be placed, which is still governed by
KIS_LIVE_ORDER_ENABLED / LIVE_ROLLOUT_ENABLED / ENTRY_DISABLED.

Exit codes
----------
    0   a candidate set was published (an empty one is a valid result)
    1   publication was refused -- no successful S1 run for that day, a
        live-mode configuration that is not exactly one LIMITED_LIVE
        scanner, or the shared store could not be located
    2   the invocation was wrong -- a malformed date or a bad --limit
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import scanner_live_mode  # noqa: E402
from scanners.analytics import date_range  # noqa: E402
from scanners.base.trading_calendar import us_trading_day  # noqa: E402
from s1_live import publisher, store  # noqa: E402

USAGE_ERROR = 2
logger = logging.getLogger("s1_publisher")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trading-day", default=None, help="YYYY-MM-DD (default: today)")
    parser.add_argument("--limit", type=int, default=publisher.MAX_S1_LIVE_CANDIDATES,
                        help=f"max candidates exposed to further checking "
                             f"(default {publisher.MAX_S1_LIVE_CANDIDATES}); this is "
                             f"NOT a maximum order count")
    parser.add_argument("--dry-run", action="store_true",
                        help="build and print without publishing")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        day = (date_range.parse_day(args.trading_day, label="--trading-day").isoformat()
               if args.trading_day else us_trading_day())
    except date_range.DateRangeError as exc:
        print(f"error: {exc}")
        return USAGE_ERROR
    if args.limit < 0:
        print(f"error: --limit must not be negative, got {args.limit}")
        return USAGE_ERROR

    try:
        live_scanner = scanner_live_mode.require_limited_live(
            scanner_live_mode.S1_SCANNER_NAME)
    except scanner_live_mode.ScannerLiveModeError as exc:
        print(f"refused: {exc}")
        return 1

    print(f"LIMITED_LIVE scanner : {live_scanner}")
    print(f"DISCOVERY_ONLY       : {', '.join(scanner_live_mode.discovery_only_scanners())}")
    print(f"trading day          : {day}")

    try:
        if args.dry_run:
            built = publisher.build(day, limit=args.limit)
            _print_rows(built["rows"])
            print(f"\nsignals seen: {built['signals_seen']}   "
                  f"truncated: {built['truncated']}   (dry run -- nothing published)")
            return 0
        result = publisher.publish(day, limit=args.limit)
    except publisher.S1PublishRefused as exc:
        print(f"refused: {exc}")
        return 1
    except store.S1StoreError as exc:
        print(f"refused: {exc}")
        return 1

    _print_rows(result["rows"])
    print(f"\nsignals seen: {result['signals_seen']}   truncated: {result['truncated']}")
    print(f"published: {result['candidate_path']}")
    print(f"published: {result['manifest_path']}")
    print(f"run id   : {result['manifest']['scanner_run_id']}")
    print(f"sha256   : {result['manifest']['payload_sha256'][:16]}...")
    print("\n후보 = 추가 검증 대상일 뿐, 매수 결정이 아님 · 실주문은 여전히 차단 상태")
    return 0


def _print_rows(rows) -> None:
    if not rows:
        print("\n(no S1 candidates for this day)")
        return
    print(f"\n{'#':>3} {'symbol':10} {'score':>8} {'price':>10}")
    print("-" * 34)
    for row in rows:
        score = row.get("scanner_score")
        price = row.get("signal_price")
        print(f"{row['rank']:>3} {str(row['symbol'])[:10]:10} "
              f"{('-' if score is None else f'{float(score):.2f}'):>8} "
              f"{('-' if price is None else f'{float(price):.2f}'):>10}")


if __name__ == "__main__":
    sys.exit(main())
