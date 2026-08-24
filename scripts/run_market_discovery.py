#!/usr/bin/env python3
"""First-stage market-wide discovery. The SCANNER node's entry point.

Holds no broker credentials and imports no order module: this machine
produces a list of symbols worth a precision scan, and the trading node
decides everything else. See discovery/manifest.py for the contract.

    scripts/run_market_discovery.py --out <path>
    scripts/run_market_discovery.py --limit 500 --max-symbols 200
"""

import argparse
import logging
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from discovery import manifest as manifest_module  # noqa: E402
from discovery import market_scan  # noqa: E402


def _commit():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"],
                                       stderr=subprocess.DEVNULL,
                                       text=True).strip()
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="logs/discovery/manifest.json")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap the INPUT universe (testing only)")
    parser.add_argument("--max-symbols", type=int,
                        default=market_scan.DEFAULT_MAX_SYMBOLS)
    parser.add_argument("--min-price", type=float, default=market_scan.MIN_PRICE)
    parser.add_argument("--min-dollar-volume", type=float,
                        default=market_scan.MIN_DOLLAR_VOLUME)
    parser.add_argument("--trading-day", default=None)
    parser.add_argument("--session", default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    from market_hours import EASTERN, us_trading_day
    from scanners.base import scan_session
    from scanners.universe import load_symbols
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    day = args.trading_day or str(us_trading_day(now))
    session = args.session or scan_session.session_at(now.astimezone(EASTERN))

    symbols = load_symbols(limit=args.limit)
    document = market_scan.run(
        symbols, trading_day=day, session=session, scanner_commit=_commit(),
        max_symbols=args.max_symbols, min_price=args.min_price,
        min_dollar_volume=args.min_dollar_volume)
    path = manifest_module.write(document, args.out)

    print(f"universe            : {document['universe_size']}")
    print(f"first-stage priced  : {document['first_stage_evaluated']}")
    print(f"first-stage passed  : {document['first_stage_passed']}")
    print(f"scan duration       : {document['scan_duration_seconds']}s")
    print(f"trading day/session : {day} / {session}")
    print(f"manifest            : {path}")
    for row in document["symbols"][:10]:
        print(f"  {row['rank']:>3} {row['symbol']:<6} "
              f"${row['observed_price']:<10} "
              f"dv=${row['dollar_volume']:,.0f} rvol={row['relative_volume']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
