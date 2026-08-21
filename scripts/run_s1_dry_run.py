#!/usr/bin/env python3
"""Simulate the S1 allocation and risk chain. Places no order, ever.

    scripts/run_s1_dry_run.py --cash 500
    scripts/run_s1_dry_run.py --cash 500 --trading-day 2026-08-17
    scripts/run_s1_dry_run.py --cash 500 --json

Reads today's validated S1 candidate set and runs it through the same
allocator, guards and sizing the live path would use. It does not import
the broker, the order gate or the execution engine -- a test asserts that
against the import graph.

`--cash` is required. There is deliberately no "read it from the account"
default: KIS reports no account-level orderable cash (see
`s1_live/cash_pool.py`), and a dry run that silently probed the broker
would be a dry run that made a network call.

Exit codes
----------
    0   the simulation ran (zero funded positions is a valid outcome)
    1   the candidate set could not be loaded, or the pool was unusable
    2   the invocation was wrong
"""

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import s1_allocation, scanner_live_mode  # noqa: E402
from scanners.analytics import date_range  # noqa: E402
from scanners.base.trading_calendar import us_trading_day  # noqa: E402
from s1_live import allocator, dry_run, store  # noqa: E402

USAGE_ERROR = 2
logger = logging.getLogger("s1_dry_run")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cash", type=float, required=True,
                        help="cash pool in USD to simulate against")
    parser.add_argument("--trading-day", default=None)
    parser.add_argument("--reserved", type=float, default=0.0,
                        help="USD already committed to open orders")
    parser.add_argument("--json", action="store_true", help="emit the raw result")

    # Account facts the guards need. All optional: omitted means "not
    # established", which makes the guards answer UNKNOWN and block --
    # the same thing that happens live, since KIS's balance response
    # carries neither equity nor a previous-day equity. Supplying them
    # is how an operator inspects the allocation arithmetic; it does not
    # make the live path able to measure them.
    parser.add_argument("--pnl-today", type=float, default=None,
                        help="signed USD P&L so far today (negative is a loss)")
    parser.add_argument("--basis-equity", type=float, default=None,
                        help="equity the day started from (the daily-loss denominator)")
    parser.add_argument("--equity", type=float, default=None,
                        help="current equity (the drawdown numerator)")
    parser.add_argument("--peak-equity", type=float, default=None,
                        help="high-water equity (the drawdown baseline)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        day = (date_range.parse_day(args.trading_day, label="--trading-day").isoformat()
               if args.trading_day else us_trading_day())
    except date_range.DateRangeError as exc:
        print(f"error: {exc}")
        return USAGE_ERROR
    if args.cash <= 0:
        print(f"error: --cash must be positive, got {args.cash}")
        return USAGE_ERROR

    try:
        scanner = scanner_live_mode.require_limited_live(
            scanner_live_mode.S1_SCANNER_NAME)
    except scanner_live_mode.ScannerLiveModeError as exc:
        print(f"refused: {exc}")
        return 1

    loaded = store.load(expected_trading_day=day, expected_scanner=scanner)
    if loaded is None:
        print(f"refused: no validated S1 candidate set for {day} "
              "(see the log for which check rejected it)")
        return 1

    rows = loaded.rows
    prices = {row["symbol"]: row["signal_price"] for row in rows}
    result = dry_run.simulate(
        trading_day=day, candidates=rows, cash_pool_usd=args.cash,
        price_lookup=lambda symbol: prices.get(symbol),
        reserved_usd=args.reserved,
        pnl_today_usd=args.pnl_today, basis_equity_usd=args.basis_equity,
        equity_usd=args.equity, peak_equity_usd=args.peak_equity)

    if args.json:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True, default=str))
        return 0

    _render(result, day)
    return 0


def _render(result, day) -> None:
    config = s1_allocation.as_dict()
    print(f"S1 DRY RUN — {day}   (no order is placed)")
    print(f"  allocation      : {config['allocation_version']}  "
          f"weights={config['rank_weights']}  reserve={config['reserve_weight']}  "
          f"single cap={config['max_single_position_pct']}")
    pool = result.cash_pool
    print(f"  cash pool       : {pool['status']} "
          f"${pool['amount_usd']:.2f}" if pool.get("amount_usd") is not None
          else f"  cash pool       : {pool['status']}")

    print("\n  account guards:")
    for guard in result.account_guards:
        measured = "-" if guard["measured"] is None else f"{guard['measured']:.4f}"
        print(f"    {guard['verdict']:8} {str(guard['reason_code'] or ''):32} "
              f"measured={measured}")
        if guard["detail"]:
            print(f"             {guard['detail']}")

    if not result.account_allowed:
        print("\n  → account guards block new entries. Nothing allocated.")
        print(f"  orders submitted: 0")
        return

    plan = result.plan or {}
    print(f"\n  deployable      : ${plan.get('deployable_usd', 0):.2f}  "
          f"reserve ${plan.get('reserve_usd', 0):.2f}")
    header = f"    {'#':>2} {'symbol':8} {'weight':>7} {'budget':>10} {'px':>9} {'qty':>5} {'cost':>10}  capped_by / status"
    print(header)
    print("    " + "-" * (len(header) - 4))
    for item in plan.get("allocations", []):
        if item["status"] == allocator.STATUS_ALLOCATED:
            print(f"    {item['rank']:>2} {item['symbol']:8} "
                  f"{item['weight']:>7.2f} {item['budget_usd']:>10.2f} "
                  f"{item['price_usd']:>9.2f} {item['quantity']:>5} "
                  f"{item['cost_usd']:>10.2f}  {item['capped_by']}")
        else:
            print(f"    {item['rank']:>2} {item['symbol']:8} "
                  f"{'':>7} {'':>10} {'':>9} {'':>5} {'':>10}  {item['status']}")

    print(f"\n  committed       : ${plan.get('committed_usd', 0):.2f}  "
          f"remaining ${plan.get('remaining_usd', 0):.2f}")
    if result.rejected:
        print("\n  rejected:")
        for item in result.rejected:
            print(f"    {item['symbol']:8} {item['stage']:16} {item['reason_code']}")
    if result.observations:
        print("\n  observations (recorded, not enforced):")
        for item in result.observations:
            ext = item["extension_pct"]
            print(f"    {item['symbol']:8} extension="
                  f"{'-' if ext is None else f'{ext:+.2f}%'}  "
                  f"age={item['signal_age_seconds']}s")
    print(f"\n  would submit    : {result.would_submit}")
    print(f"  orders submitted: 0   (dry run)")


if __name__ == "__main__":
    sys.exit(main())
