#!/usr/bin/env python3
"""CODEX-049: the Shadow SELL/EXIT evaluation service
(`us-stock-trading-shadow-exit.service`).

Excluding the exit path from the deployment entirely -- as the first
attempt did -- makes the whole point of a Shadow deployment unreachable:
an operator cannot verify that stop-loss, take-profit and the four
CODEX-046 exit features would behave correctly against a real KIS
account, because nothing evaluates them. This service closes that gap
while keeping the read-only posture:

    Shadow sell evaluation  = decides, records, submits NOTHING  -> deployed
    Live sell execution     = submits real orders                -> not deployed

It reuses `positions.lifecycle.decide_exit()` -- the SAME pure function
`check_and_manage()` dispatches on -- so a Shadow verdict cannot drift
from what the live path would actually do. There is no second copy of
the exit rules here.

Structurally incapable of ordering: this module imports neither
`execution.execution_engine` nor `brokers.kis_broker_adapter`, and calls
only `KISBroker`'s read-only methods. It never calls `check_and_manage()`
(which submits), only `decide_exit()` (which cannot).
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
from brokers.kis_broker import KISBroker, KISBrokerError  # noqa: E402
from clock import DEFAULT_CLOCK  # noqa: E402
from config.live_exit_flags import LiveExitFlags  # noqa: E402
from domain.instrument import build_instrument  # noqa: E402
from execution.secret_redaction import install_logging_redaction  # noqa: E402
from positions import lifecycle, store  # noqa: E402
from reconciliation import snapshot as reconciliation_snapshot  # noqa: E402
from state_store import db as state_db  # noqa: E402

logger = logging.getLogger("shadow_exit")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_KIS_UNAVAILABLE = 2


def _audit(run_id, event_type, result, *, symbol, reason_code=None, detail=None, now):
    """Fails closed, exactly like the buy path's audit helper."""
    try:
        shadow_audit.record_event(
            shadow_run_id=run_id, event_type=event_type, result=result, symbol=symbol,
            side="sell", reason_code=reason_code,
            payload={"detail": detail} if detail else None, now=now,
        )
    except shadow_audit.ShadowAuditError as exc:
        shadow_audit.handle_audit_failure(
            exc, shadow_run_id=run_id, symbol=symbol, side="sell", stage=event_type,
        )


def evaluate_position(*, position_id, record, broker, conn, exit_flags, now, eastern_now,
                      account_id):
    """Evaluates ONE position's exit conditions. Returns a summary dict.
    Places no order on any path through this function."""
    run_id = shadow_audit.new_run_id()
    symbol = record["symbol"]
    outcome = {
        "position_id": position_id, "symbol": symbol, "run_id": run_id,
        "result": shadow_audit.RESULT_BLOCKED, "reason_code": None, "decision": None,
    }
    terminal_recorded = False
    try:
        _audit(run_id, shadow_audit.SIGNAL_RECEIVED, shadow_audit.RESULT_INFO, symbol=symbol,
               reason_code="EXIT_EVALUATION", detail=f"state={record['state']}", now=now)

        if record["stop_price"] is None:
            outcome["reason_code"] = "STOP_NOT_FINALIZED"
            _audit(run_id, shadow_audit.INSTRUMENT_BLOCKED, shadow_audit.RESULT_BLOCKED,
                   symbol=symbol, reason_code="STOP_NOT_FINALIZED", now=now)
            return outcome

        try:
            current_price = broker.get_current_price(build_instrument(symbol, exchange="NASDAQ"))
        except (KISBrokerError, Exception) as exc:  # noqa: BLE001 -- any read failure blocks
            outcome["reason_code"] = "PRICE_UNAVAILABLE"
            _audit(run_id, shadow_audit.PRICE_DEVIATION_BLOCKED, shadow_audit.RESULT_BLOCKED,
                   symbol=symbol, reason_code="PRICE_UNAVAILABLE", detail=str(exc), now=now)
            return outcome

        # The same reconciliation gate the live exit path is subject to.
        try:
            snapshot = reconciliation_snapshot.build_snapshot(
                broker=broker, conn=conn, account_id=account_id, symbol=symbol, now=now,
                source="shadow_exit_service",
            )
        except reconciliation_snapshot.ReconciliationUnavailableError as exc:
            outcome["reason_code"] = "RECONCILIATION_UNAVAILABLE"
            _audit(run_id, shadow_audit.RECONCILIATION_BLOCKED, shadow_audit.RESULT_BLOCKED,
                   symbol=symbol, reason_code="RECONCILIATION_UNAVAILABLE", detail=str(exc),
                   now=now)
            return outcome

        try:
            reconciliation_snapshot.verify_snapshot(
                snapshot, account_id=account_id, symbol=symbol, now=now,
            )
            reconciliation_ok = True
            reconciliation_detail = None
        except reconciliation_snapshot.ReconciliationBlockedError as exc:
            reconciliation_ok = False
            reconciliation_detail = str(exc)

        decision = lifecycle.decide_exit(
            record, current_price=current_price, now=eastern_now,
            enable_partial_profit=exit_flags.enable_partial_profit,
            enable_trailing_stop=exit_flags.enable_trailing_stop,
            enable_time_stop=exit_flags.enable_time_stop,
            enable_eod_exit=exit_flags.enable_eod_exit,
        )
        outcome["decision"] = decision.action
        detail = (
            f"action={decision.action} reason={decision.reason} price={current_price} "
            f"stop={record['stop_price']} target_1={record['target_1_price']} "
            f"target_2={record['target_2_price']} remaining={record['remaining_qty']}"
        )

        if not reconciliation_ok:
            # An exit that reconciliation would have blocked is recorded
            # as blocked -- with the decision that WOULD have fired, which
            # is what makes the record useful before enabling anything.
            outcome["reason_code"] = "RECONCILIATION_DIRTY"
            _audit(run_id, shadow_audit.RECONCILIATION_BLOCKED, shadow_audit.RESULT_BLOCKED,
                   symbol=symbol, reason_code="RECONCILIATION_DIRTY",
                   detail=f"{detail} blocked_by={reconciliation_detail}", now=now)
            return outcome

        if snapshot.has_unknown_orders:
            outcome["reason_code"] = "UNKNOWN_ORDER"
            _audit(run_id, shadow_audit.UNKNOWN_ORDER_BLOCKED, shadow_audit.RESULT_BLOCKED,
                   symbol=symbol, reason_code="UNKNOWN_ORDER", detail=detail, now=now)
            return outcome

        if decision.action == lifecycle.ACTION_NONE:
            outcome["result"] = shadow_audit.RESULT_INFO
            outcome["reason_code"] = "NO_EXIT_CONDITION"
            _audit(run_id, shadow_audit.GATE_APPROVED, shadow_audit.RESULT_INFO, symbol=symbol,
                   reason_code="NO_EXIT_CONDITION", detail=detail, now=now)
            return outcome

        outcome["result"] = shadow_audit.RESULT_APPROVED
        outcome["reason_code"] = decision.reason
        _audit(run_id, shadow_audit.GATE_APPROVED, shadow_audit.RESULT_APPROVED, symbol=symbol,
               reason_code=decision.reason, detail=detail, now=now)
        _audit(run_id, shadow_audit.EXECUTION_PLANNED, shadow_audit.RESULT_APPROVED,
               symbol=symbol, reason_code="NO_ORDER_PLACED_SHADOW_MODE", detail=detail, now=now)
        return outcome
    except shadow_audit.ShadowAuditFailure:
        terminal_recorded = True
        outcome["result"] = shadow_audit.RESULT_ERROR
        outcome["reason_code"] = "AUDIT_PERSISTENCE_FAILED"
        raise
    except Exception as exc:  # noqa: BLE001 -- every run ends in a recorded outcome
        outcome["result"] = shadow_audit.RESULT_ERROR
        outcome["reason_code"] = "UNEXPECTED"
        logger.exception("shadow exit evaluation failed for %s", symbol)
        outcome["error"] = str(exc)
        return outcome
    finally:
        if not terminal_recorded:
            _audit(run_id, shadow_audit.terminal_event_for(outcome["result"]), outcome["result"],
                   symbol=symbol, reason_code=outcome["reason_code"], now=now)


def run_once(*, broker=None, now=None, eastern_now=None, conn=None, clock=None):
    current = now or datetime.now(timezone.utc)
    clock = clock or DEFAULT_CLOCK
    eastern = eastern_now or clock.now_eastern()
    broker = broker or KISBroker()
    exit_flags = LiveExitFlags.from_env()

    try:
        account_id = broker.get_account_snapshot().account_id
    except Exception as exc:  # noqa: BLE001 -- no account, no evaluation
        logger.error("KIS account read failed, cannot evaluate exits: %s", exc)
        return {"status": "kis_unavailable", "evaluated": []}

    owns_conn = conn is None
    conn = conn or state_db.open_db()
    outcomes = []
    try:
        for position_id, record in store.load_non_terminal().items():
            outcomes.append(evaluate_position(
                position_id=position_id, record=record, broker=broker, conn=conn,
                exit_flags=exit_flags, now=current, eastern_now=eastern, account_id=account_id,
            ))
    finally:
        if owns_conn:
            conn.close()
    return {"status": "ok", "evaluated": outcomes}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="KIS Shadow exit-condition evaluation (places no orders)",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    install_logging_redaction()
    try:
        result = run_once()
    except Exception as exc:  # noqa: BLE001 -- service entrypoint
        logger.exception("shadow exit pass failed: %s", exc)
        return EXIT_ERROR
    if result["status"] == "kis_unavailable":
        return EXIT_KIS_UNAVAILABLE
    for outcome in result["evaluated"]:
        logger.info(
            "shadow exit %s: decision=%s result=%s reason=%s",
            outcome["symbol"], outcome["decision"], outcome["result"], outcome["reason_code"],
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
