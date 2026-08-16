#!/usr/bin/env python3
"""S1 exit research: what price did after each hma_early_trend signal.

    scripts/run_s1_exit_research.py                       # last 30 days
    scripts/run_s1_exit_research.py --start 2026-08-17 --end 2026-09-17
    scripts/run_s1_exit_research.py --json

Post-hoc analytics only. It reads the scanner signal and performance
stores READ ONLY, writes to `logs/scanners/exit_research/`, and decides
nothing: no stop, target or holding period is recommended or applied, and
nothing here is imported by the scanning, ranking or live path.

Exit codes
    0  a report was produced (an empty window is a valid report)
    1  the report could not be produced
    2  the invocation was wrong
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.paths import get_project_root  # noqa: E402
from scanners.analytics import date_range, exit_research  # noqa: E402

USAGE_ERROR = 2
RESEARCH_SUBDIR = ("logs", "scanners", "exit_research")


def research_dir() -> Path:
    override = os.environ.get("S1_EXIT_RESEARCH_DIR")
    if override and str(override).strip():
        return Path(override)
    return Path(get_project_root()).joinpath(*RESEARCH_SUBDIR)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--days", type=int, default=date_range.DEFAULT_WINDOW_DAYS)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        start, end = date_range.resolve_range(args.start, args.end,
                                              window_days=args.days)
    except date_range.DateRangeError as exc:
        print(f"error: {exc}")
        return USAGE_ERROR

    report = exit_research.build_report(start, end)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(exit_research.format_report(report))

    if not args.no_write:
        directory = research_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"s1_exit_research_{start}_{end}.json"
        path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str),
                        encoding="utf-8")
        print(f"\nsaved: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
