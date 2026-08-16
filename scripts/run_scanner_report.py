#!/usr/bin/env python3
"""Weekly, monthly, intersection reports and the analysis export.

Spec sections 15 (weekly), 16 (monthly), 17 (intersections) and 22
(CSV/JSON export for AI analysis).

    scripts/run_scanner_report.py weekly
    scripts/run_scanner_report.py weekly --week-of 2026-08-10
    scripts/run_scanner_report.py monthly --month 2026-08
    scripts/run_scanner_report.py intersections                 # last 30 days
    scripts/run_scanner_report.py intersections --start 2026-08-01 --end 2026-08-31
    scripts/run_scanner_report.py export --start 2026-08-01 --end 2026-08-31

Reads the analytics store and writes reports and exports. It cannot
change a scanner's configuration and has no path to the order system --
section 22 requires that an analysis produce a proposal, never an
applied setting.

Exit codes
----------
    0   the report rendered (an empty window is a valid, successful report)
    1   the command could not produce a report at all
    2   the invocation was wrong -- a malformed date, a backwards range,
        or a missing required argument

2 is kept distinct from 1 so a cron entry with a typo in it is
distinguishable from a genuine failure without reading the log.
"""

import argparse
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanners.analytics import date_range  # noqa: E402
from scanners.analytics import export as export_module  # noqa: E402
from scanners.analytics import intersection_analysis, monthly_report, weekly_report  # noqa: E402

USAGE_ERROR = 2


def _parse_date(value):
    return date_range.parse_day(value, label="--week-of")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("report",
                        choices=["weekly", "monthly", "intersections", "export"])
    parser.add_argument("--start", default=None, help="start day, YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="end day, YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=date_range.DEFAULT_WINDOW_DAYS,
                        help=f"intersections: lookback window in calendar days when "
                             f"--start/--end are omitted (default "
                             f"{date_range.DEFAULT_WINDOW_DAYS})")
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
    parser.add_argument("--slack", action="store_true",
                        help="weekly only: also post a symbol-free summary to "
                             "the existing report webhook (SLACK_WEBHOOK_URL)")
    return parser.parse_args(argv)


def _post_weekly_to_slack(report, start: str, end: str) -> None:
    """Best-effort. A failed post is logged and the report still exits 0 --
    the report itself succeeded, and its file is already on disk."""
    try:
        from scanners.notify import slack as notify

        health = weekly_report.collect_run_health(start, end)
        sent = notify.send_report(weekly_report.format_slack(report, run_health=health))
        print(f"slack: {'sent' if sent else 'not sent'}")
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("weekly Slack post failed", exc_info=True)
        print("slack: not sent (see log)")


def _parse_month(value) -> tuple:
    """`YYYY-MM` -> (year, month), refusing anything else clearly."""
    text = str(value).strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m")
    except (TypeError, ValueError):
        raise date_range.DateRangeError(
            f"--month must be YYYY-MM, got {value!r}") from None
    return parsed.year, parsed.month


def _range(args, default_start, default_end):
    """Explicit endpoints win; otherwise the report's own anchor applies.

    Used by weekly and monthly, whose defaults come from --week-of and
    --month rather than from a rolling window.

    Endpoints are validated AND NORMALISED here, not passed through.

    The store compares day keys as STRINGS (`start <= day <= end` over
    `YYYY-MM-DD` filenames), so an unpadded date is not merely untidy --
    it sorts wrongly. `"2026-8-1" > "2026-08-05"` is true lexically
    because `"8" > "0"`, so `--start 2026-8-1` would have excluded the
    very days it was meant to include, and reported the result as a
    quiet week. Round-tripping through `date` forces the padded form.
    """
    start = date_range.parse_day(args.start or default_start, label="--start")
    end = date_range.parse_day(args.end or default_end, label="--end")
    if start > end:
        raise date_range.DateRangeError(
            f"--start {start.isoformat()} is after --end {end.isoformat()}; "
            "the range would be empty")
    return start.isoformat(), end.isoformat()


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    command = f"run_scanner_report.py {getattr(args, 'report', '?')}"
    try:
        code, detail = _run(args), None
    except date_range.DateRangeError as exc:
        # Usage error, not a failed report. Exiting 2 keeps a typo in a
        # cron entry distinguishable from a genuine operational failure.
        print(f"error: {exc}")
        code, detail = USAGE_ERROR, str(exc)
    _alert_on_failure(command, code, detail)
    return code


def _alert_on_failure(command: str, exit_code: int, detail=None) -> None:
    """Non-zero exit -> the alert channel. Best-effort, always.

    Wrapped here rather than inside `_run` so that the exit code is
    fully decided before any network call, and so a Slack problem can
    never turn a successful report into a failed one.
    """
    if not exit_code:
        return
    try:
        from scanners.notify import slack as notify

        notify.notify_cli_failure(command, exit_code, detail=detail)
    except Exception:  # noqa: BLE001 - a report is not failed by its alert
        logging.getLogger(__name__).warning(
            "could not attempt the CLI failure notification", exc_info=True)


def _run(args) -> int:
    if args.report == "weekly":
        # Anchored on the EASTERN trading day, not `date.today()`. On a
        # UTC host the local date rolls over around 19:00-20:00 ET, so a
        # Sunday-evening cron run would otherwise resolve to next week
        # and render an empty report with nothing to explain why.
        reference = _parse_date(args.week_of) if args.week_of else date_range.today()
        default_start, default_end = weekly_report.week_bounds(reference)
        start, end = _range(args, default_start, default_end)
        report = weekly_report.build(
            start, end, hit_horizon=args.hit_horizon or "return_1d")
        print(weekly_report.format_report(report))
        if not args.no_write:
            print(f"\nsaved: {weekly_report.write(report)}")
        if args.slack:
            _post_weekly_to_slack(report, start, end)
        return 0

    if args.report == "monthly":
        if args.month:
            year, month = _parse_month(args.month)
        else:
            anchor = date_range.today()
            year, month = anchor.year, anchor.month
        default_start, default_end = monthly_report.month_bounds(year, month)
        start, end = _range(args, default_start, default_end)
        report = monthly_report.build(
            start, end, hit_horizon=args.hit_horizon or "return_5d")
        print(monthly_report.format_report(report))
        if not args.no_write:
            print(f"\nsaved: {monthly_report.write(report)}")
        return 0

    if args.report == "intersections":
        # Both endpoints optional: whichever is missing is derived from
        # the rolling window, so the bare command works in cron without
        # a caller having to compute dates.
        start, end = date_range.resolve_range(
            args.start, args.end, window_days=args.days)
        horizon = args.hit_horizon or "return_5d"
        scopes = ([intersection_analysis.BY_DAY, intersection_analysis.BY_RUN]
                  if args.scope == "both" else [args.scope])
        for index, scope in enumerate(scopes):
            if index:
                print("")
            result = intersection_analysis.analyse_range(
                start, end, hit_horizon=horizon, scope=scope)
            print(intersection_analysis.format_report(result))
        return 0

    # export: still requires both endpoints. An export writes a file
    # named after its range, and quietly defaulting that range would
    # produce an authoritative-looking dataset covering a window the
    # caller never asked for.
    if not (args.start and args.end):
        print("error: export needs --start and --end")
        return USAGE_ERROR
    date_range.resolve_range(args.start, args.end)  # validates both
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
