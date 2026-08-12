#!/usr/bin/env python3
"""Weekly, monthly, intersection reports and the analysis export.

Spec sections 15 (weekly), 16 (monthly), 17 (intersections) and 22
(CSV/JSON export for AI analysis).

    scripts/run_scanner_report.py weekly
    scripts/run_scanner_report.py weekly --week-of 2026-08-10
    scripts/run_scanner_report.py monthly --month 2026-08
    scripts/run_scanner_report.py intersections --start 2026-08-01 --end 2026-08-31
    scripts/run_scanner_report.py export --start 2026-08-01 --end 2026-08-31

Reads the analytics store and writes reports and exports. It cannot
change a scanner's configuration and has no path to the order system --
section 22 requires that an analysis produce a proposal, never an
applied setting.
"""

import argparse
import logging
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanners.analytics import export as export_module  # noqa: E402
from scanners.analytics import intersection_analysis, monthly_report, weekly_report  # noqa: E402


def _parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("report",
                        choices=["weekly", "monthly", "intersections", "export"])
    parser.add_argument("--start", default=None, help="start day, YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="end day, YYYY-MM-DD")
    parser.add_argument("--week-of", default=None,
                        help="any day in the target week (weekly report)")
    parser.add_argument("--month", default=None,
                        help="target month as YYYY-MM (monthly report)")
    parser.add_argument("--hit-horizon", default=None,
                        help="return field the hit rate is computed on")
    parser.add_argument("--format", choices=["csv", "json", "both"], default="both",
                        help="export format (export only)")
    parser.add_argument("--scope", choices=["day", "run", "both"], default="both",
                        help="intersections: agreement within one runner invocation "
                             "('run'), anywhere in the trading day ('day'), or both")
    parser.add_argument("--no-write", action="store_true",
                        help="print the report without saving it")
    return parser.parse_args(argv)


def _range(args, default_start, default_end):
    start = args.start or default_start
    end = args.end or default_end
    return start, end


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.report == "weekly":
        reference = _parse_date(args.week_of) if args.week_of else date.today()
        default_start, default_end = weekly_report.week_bounds(reference)
        start, end = _range(args, default_start, default_end)
        report = weekly_report.build(
            start, end, hit_horizon=args.hit_horizon or "return_1d")
        print(weekly_report.format_report(report))
        if not args.no_write:
            print(f"\nsaved: {weekly_report.write(report)}")
        return 0

    if args.report == "monthly":
        if args.month:
            year, month = (int(part) for part in args.month.split("-"))
        else:
            today = date.today()
            year, month = today.year, today.month
        default_start, default_end = monthly_report.month_bounds(year, month)
        start, end = _range(args, default_start, default_end)
        report = monthly_report.build(
            start, end, hit_horizon=args.hit_horizon or "return_5d")
        print(monthly_report.format_report(report))
        if not args.no_write:
            print(f"\nsaved: {monthly_report.write(report)}")
        return 0

    if args.report == "intersections":
        if not (args.start and args.end):
            print("intersections needs --start and --end")
            return 1
        horizon = args.hit_horizon or "return_5d"
        scopes = ([intersection_analysis.BY_DAY, intersection_analysis.BY_RUN]
                  if args.scope == "both" else [args.scope])
        for index, scope in enumerate(scopes):
            if index:
                print("")
            result = intersection_analysis.analyse_range(
                args.start, args.end, hit_horizon=horizon, scope=scope)
            print(intersection_analysis.format_report(result))
        return 0

    if not (args.start and args.end):
        print("export needs --start and --end")
        return 1
    wrote = []
    if args.format in ("csv", "both"):
        path = export_module.to_csv(args.start, args.end)
        if path:
            wrote.append(path)
    if args.format in ("json", "both"):
        path = export_module.to_json(args.start, args.end)
        if path:
            wrote.append(path)
    if not wrote:
        print(f"no signals between {args.start} and {args.end}; nothing exported")
        return 0
    for path in wrote:
        print(f"exported: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
