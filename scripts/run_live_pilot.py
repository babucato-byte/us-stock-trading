#!/usr/bin/env python3
"""T9: the real-time pilot entrypoint -- scanner, entry conditions and
exit conditions driven against live market data, tick after tick, for a
whole session.

    scripts/start_live_pilot.sh          the operator-facing wrapper
    scripts/run_live_pilot.py            this program

It refuses to start unless every gate in `live_pilot/preflight.py`
passes (KIS environment, the two unconfirmed KIS wire-format values,
kill switch, reconciliation freshness, a real account read, the scan
universe, the watchlist, flag consistency, a writable log directory and
no other trading process mid-run). There is no --skip-preflight and no
environment variable that turns a gate off.

What a tick DOES is decided by the operator's environment, never by this
program:

    default (any of the three flags unset)  OBSERVE -- evaluate
        everything against live data, submit nothing
    KIS_LIVE_ORDER_ENABLED + LIVE_ROLLOUT_ENABLED set and
    ENTRY_DISABLED clear                     ARMED -- drive the existing
        live buy cycle and the existing exit tick

This file sets none of those. Starting with KIS_ENV=paper puts the
ARMED posture against the 모의투자 account, which is the sanctioned way
to exercise the buy and sell paths for real; KIS_ENV=live additionally
requires LIVE_PILOT_ACK_LIVE_ENV=true and refuses while any KIS value is
still LIVE_RESPONSE_PENDING.

Exit codes:
    0  the session ran and the daily report was written
    1  an unexpected failure
    3  preflight refused to start the session
    4  an unrecoverable order-state connection fault (HALT is set)
    7  another pilot already holds the session lock
"""

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from execution.order_repository import (  # noqa: E402
    FatalRepositoryConnectionError,
)
from execution.secret_redaction import install_logging_redaction  # noqa: E402
from live_pilot import preflight as pilot_preflight  # noqa: E402
from live_pilot import recorder, runner  # noqa: E402

logger = logging.getLogger("live_pilot")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_PREFLIGHT_REFUSED = 3
EXIT_FATAL_DB = 4
EXIT_LOCKED = 7


def _fail_stop(stage, exc):
    """Report an unrecoverable database-connection fault and let the
    caller exit non-zero. HALT was set by the repository before this
    exception was raised; nothing here clears it."""
    logger.critical(
        "FATAL: unrecoverable order-state connection fault during %s (%s) -- "
        "HALT is set and this process must restart so the OS releases the SQLite lock",
        stage, type(exc).__name__,
    )
    try:
        from operations import alerts

        alerts.send_alert(
            "*CRITICAL: trading process fail-stop*\n"
            f"- stage: {stage}\n"
            f"- cause: {type(exc).__name__}\n"
            "- HALT: set\n"
            "- action: process exiting non-zero so systemd restarts it and the SQLite "
            "write lock is released"
        )
    except Exception as alert_exc:  # noqa: BLE001 -- alerting must not mask the fault
        logger.error("could not alert on fail-stop: %s", alert_exc)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Real-time pilot: scanner, entry and exit conditions against live data",
    )
    parser.add_argument("--interval", type=float, default=runner.DEFAULT_INTERVAL_SECONDS,
                        help="seconds between ticks (default: %(default)s)")
    parser.add_argument("--max-ticks", type=int, default=None,
                        help="stop after this many ticks (default: run until --until or a signal)")
    parser.add_argument("--once", action="store_true",
                        help="run exactly one tick; same as --max-ticks 1 --interval 0")
    parser.add_argument("--until", default=None,
                        help="stop at this ISO-8601 instant, e.g. 2026-08-06T20:00:00+00:00")
    parser.add_argument("--sessions", default=",".join(runner.DEFAULT_SESSIONS),
                        help="comma-separated sessions to evaluate in: premarket, regular, "
                             "aftermarket, closed (default: %(default)s)")
    parser.add_argument("--scan-interval", type=float,
                        default=runner.DEFAULT_SCAN_INTERVAL_SECONDS,
                        help="seconds between scanner passes; 0 disables scanning and reuses "
                             "the existing candidate files (default: %(default)s)")
    parser.add_argument("--scan-limit", type=int, default=None,
                        help="cap the scanner at this many symbols per pass")
    parser.add_argument("--preset", default=None, help="scanner preset name")
    parser.add_argument("--log-dir", default=None,
                        help="where tick JSONL and the daily report go "
                             "(default: $LIVE_PILOT_LOG_DIR or logs/live_pilot)")
    parser.add_argument("--preflight-only", action="store_true",
                        help="run the start-up checklist, print it and exit")
    parser.add_argument("--report-only", action="store_true",
                        help="rebuild a day's report from its recorded ticks and exit")
    parser.add_argument("--date", default=None,
                        help="with --report-only: the day to rebuild (YYYY-MM-DD, UTC)")
    parser.add_argument("--log-level", default="INFO")
    return parser


def _parse_until(raw):
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None:
        raise ValueError("--until must carry a timezone offset, e.g. ...+00:00")
    return value


def main(argv=None):
    args = build_parser().parse_args(argv)

    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    install_logging_redaction()

    directory = Path(args.log_dir) if args.log_dir else recorder.log_dir()

    if args.report_only:
        for_date = (recorder.parse_date(args.date) if args.date
                    else datetime.now(timezone.utc).date())
        target, report = recorder.write_report(for_date=for_date, directory=directory)
        print(f"report: {target}")
        print(f"ticks={report['tick_count']} "
              f"entry_evaluations={report['entry']['evaluations']} "
              f"exit_evaluations={report['exit']['evaluations']} "
              f"errors={len(report['errors'])} "
              f"unreadable_lines={len(report['unreadable_lines'])}")
        return EXIT_OK

    try:
        until = _parse_until(args.until) if args.until else None
    except ValueError as exc:
        logger.error("could not parse --until: %s", exc)
        return EXIT_ERROR

    sessions = tuple(s.strip().lower() for s in args.sessions.split(",") if s.strip())
    if not sessions:
        logger.error("--sessions resolved to nothing; pass at least one session name")
        return EXIT_ERROR

    interval = 0.0 if args.once else args.interval
    max_ticks = 1 if args.once else args.max_ticks
    scan_enabled = args.scan_interval is not None and args.scan_interval > 0

    try:
        broker = runner.build_broker()
    except Exception as exc:  # noqa: BLE001 -- a configuration problem, not a crash
        logger.error("cannot construct a KIS client: %s", type(exc).__name__)
        broker = None

    report = pilot_preflight.run_preflight(
        broker=broker, log_dir=directory, scan_enabled=scan_enabled)
    print(report.render())
    if not report.passed:
        print(f"\nPILOT PREFLIGHT FAILED: {len(report.failures)} gate(s) did not pass -- "
              "refusing to start the session.")
        return EXIT_PREFLIGHT_REFUSED
    print("\nPILOT PREFLIGHT OK.")
    if args.preflight_only:
        return EXIT_OK

    stop = runner.StopSignal().install()
    try:
        with runner.PilotLock(runner.lock_path()):
            summary = runner.run_loop(
                broker=broker, interval=interval, max_ticks=max_ticks, until=until,
                sessions=sessions, scan_interval=args.scan_interval,
                scan_limit=args.scan_limit, preset=args.preset, directory=directory,
                stop=stop,
            )
    except runner.PilotLockError as exc:
        logger.error("%s", exc)
        return EXIT_LOCKED
    except FatalRepositoryConnectionError as exc:
        _fail_stop("live pilot session", exc)
        return EXIT_FATAL_DB
    except Exception as exc:  # noqa: BLE001 -- entrypoint
        logger.exception("live pilot session failed: %s", exc)
        return EXIT_ERROR
    finally:
        stop.restore()

    print(f"\nticks={summary['ticks']} recorded={summary['recorded']} "
          f"stopped_because={summary['stopped_because']}")
    print(f"report: {summary['report_path']}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
