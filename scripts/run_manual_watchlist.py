#!/usr/bin/env python3
"""Build a Manual Watchlist (track C).

    scripts/run_manual_watchlist.py tomorrow            # evening pass, file only
    scripts/run_manual_watchlist.py today --slack       # morning pass, posts TOP 5
    scripts/run_manual_watchlist.py today --slack --top 10
    scripts/run_manual_watchlist.py today --no-write    # print, save nothing

MANUAL_ONLY. This script reads the scanner analytics store and writes
under `logs/watchlist/`. It cannot publish a candidate, size a position,
or reach a broker -- `tests/test_watchlist_isolation.py` asserts that
against the source tree in both directions.

`tomorrow` files its output under the NEXT trading day, because that is
the day the list is for. `today` reads that file back and applies this
morning's premarket confirmation.

Exit codes
----------
    0   a watchlist was produced (an empty one is a valid result)
    1   the watchlist could not be produced
    2   the invocation was wrong -- a malformed date, an unknown stage
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanners.analytics import date_range  # noqa: E402
from scanners.base import trading_calendar  # noqa: E402
from scanners.base.trading_calendar import us_trading_day  # noqa: E402
from watchlist import builder, config, render, store  # noqa: E402

USAGE_ERROR = 2
logger = logging.getLogger("manual_watchlist")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=[config.STAGE_TOMORROW, config.STAGE_TODAY])
    parser.add_argument("--session-day", default=None,
                        help="tomorrow: the session that just closed (default: today)")
    parser.add_argument("--target-day", default=None,
                        help="tomorrow: the day the list is FOR (default: next trading day)")
    parser.add_argument("--trading-day", default=None,
                        help="today: the trading day to build for (default: today)")
    parser.add_argument("--slack", action="store_true",
                        help="today only: post the TOP N to the report webhook")
    parser.add_argument("--top", type=int, default=None,
                        help=f"Slack size, default {config.SLACK_TOP_N}, "
                             f"max {config.SLACK_TOP_N_MAX}")
    parser.add_argument("--no-write", action="store_true",
                        help="print without saving")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        return _run(args)
    except (date_range.DateRangeError, trading_calendar.TradingCalendarError) as exc:
        print(f"error: {exc}")
        return USAGE_ERROR
    except store.WatchlistStoreError as exc:
        print(f"error: {exc}")
        return 1


def _run(args) -> int:
    if args.stage == config.STAGE_TOMORROW:
        session_day = (date_range.parse_day(args.session_day, label="--session-day").isoformat()
                       if args.session_day else us_trading_day())
        target_day = (date_range.parse_day(args.target_day, label="--target-day").isoformat()
                      if args.target_day
                      else trading_calendar.next_trading_day(session_day))
        payload = builder.build_tomorrow(session_day, target_day)
    else:
        trading_day = (date_range.parse_day(args.trading_day, label="--trading-day").isoformat()
                       if args.trading_day else us_trading_day())
        payload = builder.build_today(trading_day)

    print(render.format_console(payload))

    if not args.no_write:
        day, stage = payload["trading_day"], payload["stage"]
        print(f"\nsaved: {store.write_json(payload, trading_day=day, stage=stage)}")
        print(f"saved: {store.write_text(render.format_markdown(payload), trading_day=day, stage=stage)}")

    if args.slack:
        if args.stage == config.STAGE_TOMORROW:
            # Track C-2: the evening pass is silent by design.
            print("slack: skipped (tomorrow stage does not post)")
        else:
            _post(payload, args.top)
    return 0


def _post(payload, top) -> None:
    """Best-effort. A failed post never changes the exit code: the
    watchlist itself succeeded and its file is already on disk."""
    try:
        from scanners.notify import slack as notify

        sent = notify.send_report(render.format_slack(payload, top_n=top))
        print(f"slack: {'sent' if sent else 'not sent'}")
    except Exception:  # noqa: BLE001
        logger.warning("watchlist Slack post failed", exc_info=True)
        print("slack: not sent (see log)")


if __name__ == "__main__":
    sys.exit(main())
