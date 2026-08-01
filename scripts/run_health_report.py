#!/usr/bin/env python3
"""CODEX-049: the health-reporting entrypoint
(`us-stock-trading-health.service`).

Answers, from durable state alone and with no KIS call, the questions an
operator needs between Shadow passes:

    is the schema current?
    is the safety posture still read-only?
    is the reconciliation record fresh and clean?
    are there UNKNOWN orders sitting unresolved?
    does every Shadow run have exactly one terminal event?
    is the Shadow JSONL log free of corrupt lines?
    is us-stock-trading-live.service still disabled?

Exits 0 when everything is healthy, 1 on a structural error, and 2 when
a health problem was found -- so a systemd timer failure is itself the
alarm, without needing anything to parse the output.
"""

import argparse
import json
import logging
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import shadow_audit  # noqa: E402
import shadow_mode  # noqa: E402
from execution import idempotency  # noqa: E402
from execution.secret_redaction import install_logging_redaction  # noqa: E402
from reconciliation import reconciliation_state  # noqa: E402
from state_store import db as state_db  # noqa: E402
from state_store.migrations import CURRENT_SCHEMA_VERSION  # noqa: E402

logger = logging.getLogger("health")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNHEALTHY = 2

LIVE_UNIT = "us-stock-trading-live.service"


def _live_service_state():
    """Returns systemd's own answer, or None where systemctl does not
    exist (developer machines) -- an absent systemctl is not a health
    failure, it just means there is nothing to check."""
    if shutil.which("systemctl") is None:
        return None
    try:
        result = subprocess.run(
            ["systemctl", "is-enabled", LIVE_UNIT], capture_output=True, text=True,
        )
    except OSError:
        return None
    return result.stdout.strip() or result.stderr.strip()


def collect(*, now=None, conn=None, check_live_unit=True):
    current = now or datetime.now(timezone.utc)
    owns_conn = conn is None
    conn = conn or state_db.open_db()
    problems = []
    report = {"checked_at": current.isoformat()}
    try:
        version = state_db.get_schema_version(conn)
        report["schema_version"] = version
        if version != CURRENT_SCHEMA_VERSION:
            problems.append(f"schema version {version} != expected {CURRENT_SCHEMA_VERSION}")

        record = reconciliation_state.get_last_result()
        report["reconciliation"] = None if record is None else {
            "clean": record.clean, "mismatch_count": record.mismatch_count,
            "checked_at": record.checked_at.isoformat(),
        }
        if record is None:
            problems.append("no reconciliation result has ever been recorded")
        elif not record.clean:
            problems.append(f"last reconciliation was dirty ({record.mismatch_count} mismatches)")

        unknown = idempotency.list_unknown_orders(conn)
        report["unknown_orders"] = [row["internal_order_id"] for row in unknown]
        if unknown:
            problems.append(f"{len(unknown)} order(s) are still UNKNOWN")

        integrity = shadow_audit.audit_integrity_report(conn=conn)
        report["shadow_audit"] = integrity
        if integrity["runs_without_terminal_event"]:
            problems.append(
                f"{len(integrity['runs_without_terminal_event'])} shadow run(s) have no terminal event"
            )
        if integrity["runs_with_multiple_terminal_events"]:
            problems.append(
                f"{len(integrity['runs_with_multiple_terminal_events'])} shadow run(s) have "
                "more than one terminal event"
            )

        _records, corruption = shadow_mode.read_all_with_integrity()
        report["shadow_log_corruption"] = corruption
        if corruption:
            problems.append(f"{len(corruption)} corrupt Shadow log line(s)")

        if check_live_unit:
            live_state = _live_service_state()
            report["live_service"] = live_state
            if live_state == "enabled":
                problems.append(f"{LIVE_UNIT} is ENABLED -- it must stay disabled")
    finally:
        if owns_conn:
            conn.close()
    report["problems"] = problems
    report["healthy"] = not problems
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Durable-state health report (makes no KIS calls)")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    install_logging_redaction()
    try:
        report = collect()
    except Exception as exc:  # noqa: BLE001 -- service entrypoint
        logger.exception("health report failed: %s", exc)
        return EXIT_ERROR

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        for key, value in report.items():
            if key != "problems":
                logger.info("%s: %s", key, value)
        for problem in report["problems"]:
            logger.error("HEALTH: %s", problem)
    return EXIT_OK if report["healthy"] else EXIT_UNHEALTHY


if __name__ == "__main__":
    raise SystemExit(main())
