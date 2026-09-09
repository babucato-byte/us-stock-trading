#!/usr/bin/env python3
"""Post the session readiness message to stock-live-trading.

    scripts/send_session_ready.py                 # session from the clock
    scripts/send_session_ready.py --session REGULAR
    scripts/send_session_ready.py --print         # build only, no Slack

Runs a few minutes after each enabled session opens (crontab lines are
documented in deploy/cron/README or the operations notes; none are
installed by this script). One message per (trading day, session) via
the notification ledger. Read-only against KIS and the state store.

Exit 0 when READY was announced or already announced, 1 when the session
is BLOCKED (the BLOCKED message is still sent), 2 when the report itself
could not be built.
"""
import argparse
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.secret_redaction import install_logging_redaction  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("send_session_ready")


def _session_from_clock():
    from scanners.base import scan_session

    return scan_session.session_at()


def main(argv=None) -> int:
    install_logging_redaction()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", default=None,
                        help="PREMARKET / REGULAR / AFTER_HOURS / OVERNIGHT_DAYTIME")
    parser.add_argument("--print", action="store_true", dest="print_only",
                        help="build and print the message; do not send")
    args = parser.parse_args(argv)

    from operations import session_readiness, slack_presentation as sp

    session = args.session or _session_from_clock()
    if not session:
        print("no market session is open now; nothing to announce")
        return 0
    broker = None
    if not args.print_only:
        try:
            from brokers.kis_broker import KISBroker

            broker = KISBroker()
        except Exception:  # noqa: BLE001
            logger.warning("KIS broker unavailable for the readiness read", exc_info=True)
    try:
        report = session_readiness.build(session=session, broker=broker,
                                         now=datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001
        logger.error("readiness report could not be built: %s", type(exc).__name__,
                     exc_info=True)
        return 2
    print(sp.session_ready(report))
    if args.print_only:
        return 0 if report["ready"] else 1
    conn = None
    try:
        from state_store import db as state_db

        conn = state_db.open_db()
    except Exception:  # noqa: BLE001
        conn = None
    try:
        session_readiness.announce(report, conn=conn)
    finally:
        if conn is not None:
            conn.close()
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
