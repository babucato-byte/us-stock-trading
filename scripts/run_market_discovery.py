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
    parser.add_argument("--no-eligibility-filter", action="store_true",
                        help="scan the raw universe (measurement only)")
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

    from discovery import eligible_universe, provider_health

    # A project-local provider cache. Two scans sharing the default one
    # have produced "unable to open database file", which is a local
    # resource fault that arrives looking like a data fault.
    provider_health.use_project_cache("logs/discovery/yfinance-cache")

    raw = load_symbols(limit=args.limit)
    if args.no_eligibility_filter:
        symbols, raw_count = raw, len(raw)
    else:
        # Cached, because security type and listing venue are not
        # intraday facts. Rebuilt at most once a day, or whenever
        # universe.csv changes underneath it.
        cache = eligible_universe.load_or_build()
        keep = set(cache["symbols"])
        symbols = [s for s in raw if s in keep]
        raw_count = len(raw)
        print(f"raw universe        : {raw_count}")
        print(f"eligible universe   : {len(symbols)}")
        print(f"excluded            : {cache['exclude_reason_counts']}")

    document = market_scan.run(
        symbols, trading_day=day, session=session, scanner_commit=_commit(),
        max_symbols=args.max_symbols, min_price=args.min_price,
        min_dollar_volume=args.min_dollar_volume,
        raw_universe_count=raw_count)
    # An empty result BEFORE the open is not a market observation.
    #
    # `fetch_today` requires each symbol's latest daily bar to carry the
    # trading day. Between the ET rollover and the open no symbol has one
    # yet, so a scan in that window prices nothing and passes nothing --
    # and writing that over a good manifest is how a 600-symbol input
    # became 0, which the trading node then correctly rejected as EMPTY
    # and fell back to its own 300-name ranking for the rest of the
    # night. The candidates did not fail a strategy gate; they stopped
    # being offered.
    #
    # So an empty document never replaces a non-empty one for the same
    # trading day. It is still written when there is nothing to lose.
    existing = manifest_module.read(args.out)
    if not document["symbols"] and existing and existing.get("symbols"):
        print(f"REFUSED to overwrite {len(existing['symbols'])} symbols with "
              f"an empty scan (priced {document['first_stage_evaluated']} of "
              f"{document['universe_size']}); keeping the previous manifest")
        return 0

    path = manifest_module.write(document, args.out)

    print(f"eligible scanned    : {document['universe_size']}")
    print(f"first-stage priced  : {document['first_stage_evaluated']}")
    print(f"first-stage passed  : {document['first_stage_passed']}")
    print(f"failed              : {document['failed_count']}")
    print(f"failure reasons     : {document['failure_reason_counts']}")
    print(f"eligible coverage   : {document['eligible_market_coverage']}")
    print(f"raw mkt coverage    : {document['raw_market_coverage']}")
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
