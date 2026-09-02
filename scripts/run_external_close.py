#!/usr/bin/env python3
"""TCN-02A: retire position rows the broker no longer holds.

Dry run by default. Prints, per book, what `reconciliation/
external_close.py` would do and why each row was kept; nothing is
written unless `--apply` is given.

READ-ONLY against KIS: `get_positions()` and `get_open_orders()` only.
It never submits or cancels an order, and it never invents an exit
price -- a retired row is EXTERNALLY_CLOSED with no realised PnL.

Not scheduled. Wiring this into cron or systemd is TCN-02B's decision;
this script exists so the decision can be exercised by hand first.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from execution.secret_redaction import install_logging_redaction  # noqa: E402
from reconciliation import external_close_service  # noqa: E402
from state_store import db as state_db  # noqa: E402

logger = logging.getLogger("external_close")

EXIT_OK = 0
EXIT_ERROR = 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Retire position rows the broker no longer holds (dry run "
                    "unless --apply)")
    parser.add_argument("--apply", action="store_true",
                        help="actually retire; without it nothing is written")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    install_logging_redaction()

    try:
        from brokers.kis_broker import KISBroker

        broker = KISBroker()
        with state_db.open_db() as conn:
            report = external_close_service.retire_all(
                conn, broker, apply=args.apply)
    except Exception as exc:  # noqa: BLE001 -- script entrypoint
        logger.exception("external close pass failed: %s", exc)
        return EXIT_ERROR

    summary = external_close_service.summarize(report)
    logger.info("external close %s: %s",
                "APPLIED" if args.apply else "DRY RUN", json.dumps(summary))
    print(json.dumps({"applied": bool(args.apply), "summary": summary,
                      "report": report}, default=str, indent=2))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
