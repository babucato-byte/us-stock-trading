#!/usr/bin/env python3
"""CODEX-049: the reconciliation service entrypoint
(`us-stock-trading-reconcile.service`).

READ-ONLY against KIS, by construction: it calls only
`get_positions()` / `get_open_orders()` / `get_fills()`, which run
`KISConfig.validate_read_allowed()`, never `submit_order()` /
`cancel_order()`. It deliberately does NOT call
`kis_position_manager.sync_kis_fills_and_manage_exits()` -- that tick
can submit real exit orders, and the initial Oracle deployment runs
without any order path enabled at all.

Each pass:

    1. builds a real reconciliation snapshot (internal vs KIS positions,
       open orders and fills)
    2. resolves any UNKNOWN order against KIS's own history
    3. records the durable reconciliation result (or records NOTHING if
       a KIS read failed -- the previous result then ages out, which is
       the fail-closed outcome)
    4. applies the Shadow audit retention policy

Exit code 0 means "a real reconciliation completed"; 2 means "KIS could
not be read, nothing was recorded"; 1 means an unexpected error.
"""

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import shadow_audit  # noqa: E402
import shadow_mode  # noqa: E402
from brokers.kis_broker import KISBroker  # noqa: E402
from execution import idempotency, order_repository  # noqa: E402
from execution.order_repository import (  # noqa: E402
    FatalRepositoryConnectionError,
)
from execution.secret_redaction import install_logging_redaction  # noqa: E402
from reconciliation import reconciliation_state  # noqa: E402
from reconciliation import snapshot as reconciliation_snapshot  # noqa: E402
from reconciliation.order_reconciler import reconcile_unknown_order  # noqa: E402
from state_store import db as state_db  # noqa: E402

logger = logging.getLogger("reconciliation")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_KIS_UNAVAILABLE = 2

EXIT_FATAL_DB = 4


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


def resolve_unknown_orders(conn, broker, *, now):
    """Resolves every UNKNOWN order against KIS's own open-order/fill
    history, through the compare-and-set repository (CODEX-047) -- never
    a direct status write, and never a re-submission."""
    try:
        kis_open_orders = broker.get_open_orders()
        kis_fills = broker.get_fills(
            start_date=now.strftime("%Y%m%d"), end_date=now.strftime("%Y%m%d"),
        )
    except Exception as exc:
        logger.warning("cannot resolve UNKNOWN orders this pass: %s", exc)
        return []
    resolved = []
    for row in idempotency.list_unknown_orders(conn):
        outcome = reconcile_unknown_order(
            row["internal_order_id"], row["broker_order_id"], kis_open_orders, kis_fills,
            requested_quantity=row["requested_quantity"],
        )
        if not outcome.resolved:
            logger.info("order %s stays UNKNOWN: %s", row["internal_order_id"], outcome.reason)
            continue
        try:
            order_repository.compare_and_set_state(
                conn, order_id=row["internal_order_id"], expected_state="UNKNOWN",
                next_state=outcome.confirmed_status, event_type="UNKNOWN_RECONCILED",
                event_payload={"reason": outcome.reason}, expected_version=row["version"],
                via_reconciliation=True, now=now,
            )
        except FatalRepositoryConnectionError:
            # CODEX-059: not a per-order problem -- the connection is
            # unusable and the process must stop.
            raise
        except order_repository.OrderRepositoryError as exc:
            # Another writer got there first -- their result stands.
            logger.warning("CAS conflict resolving %s: %s", row["internal_order_id"], exc)
            continue
        resolved.append((row["internal_order_id"], outcome.confirmed_status))
    return resolved


def run_once(*, broker=None, now=None, conn=None, account_id=None):
    current = now or datetime.now(timezone.utc)
    broker = broker or KISBroker()
    owns_conn = conn is None
    conn = conn or state_db.open_db()
    try:
        resolved = resolve_unknown_orders(conn, broker, now=current)
        try:
            snapshot = reconciliation_snapshot.build_snapshot(
                broker=broker, conn=conn,
                account_id=account_id if account_id is not None else broker.config.account_no or "",
                symbol=None, now=current, source="reconcile_service",
            )
        except reconciliation_snapshot.ReconciliationUnavailableError as exc:
            # Record NOTHING: a failed read must never refresh the clean
            # timestamp the order gates read (CODEX-044).
            logger.error("reconciliation could not be performed: %s", exc)
            return {"status": "kis_unavailable", "resolved": resolved, "snapshot": None}

        reconciliation_state.record_result(
            clean=snapshot.is_clean(), mismatch_count=snapshot.mismatch_count(), now=current,
        )
        if not snapshot.is_clean():
            for line in snapshot.detail:
                logger.error("reconciliation mismatch: %s", line)
        purged_rows = shadow_audit.purge_old_events(now=current, conn=conn)
        purged_files = shadow_mode.purge_old_files(now=current)
        return {
            "status": "clean" if snapshot.is_clean() else "mismatch",
            "resolved": resolved, "snapshot": snapshot,
            "purged_audit_rows": purged_rows, "purged_log_files": len(purged_files),
        }
    finally:
        if owns_conn:
            conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="KIS read-only reconciliation pass")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    install_logging_redaction()

    try:
        result = run_once()
    except FatalRepositoryConnectionError as exc:
        # CODEX-058: the order-state connection could neither be rolled
        # back nor closed, so this process may still hold a SQLite write
        # lock that blocks every other writer. HALT is already set by the
        # repository; exiting non-zero is what actually releases the lock
        # (the OS reclaims the descriptor) and lets systemd's
        # Restart=on-failure bring the service back cleanly.
        _fail_stop("reconciliation", exc)
        return EXIT_FATAL_DB
    except Exception as exc:  # noqa: BLE001 -- service entrypoint
        logger.exception("reconciliation pass failed: %s", exc)
        return EXIT_ERROR

    if result["status"] == "kis_unavailable":
        return EXIT_KIS_UNAVAILABLE
    logger.info(
        "reconciliation %s; resolved=%d purged_audit_rows=%s purged_log_files=%s",
        result["status"], len(result["resolved"]), result.get("purged_audit_rows"),
        result.get("purged_log_files"),
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
