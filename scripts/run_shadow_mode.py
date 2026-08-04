#!/usr/bin/env python3
"""CODEX-049: the Shadow Mode service entrypoint
(`us-stock-trading-shadow.service`).

Evaluates every watchlist candidate exactly as the live buy path would
-- score, KIS price re-check, account/cash read, sizing, instrument
eligibility, reconciliation snapshot, full Order Gate -- and records the
result to BOTH the structured Shadow record (`shadow_mode.py`) and the
durable Shadow audit trail (`shadow_audit.py`).

It CANNOT place an order. Not "does not", cannot: this module never
imports or calls `execution.execution_engine`, and never invokes the
broker's order-submission method. The only broker methods reachable from
here are the read-only ones. That is deliberately a stronger guarantee
than relying on `KIS_LIVE_ORDER_ENABLED=false`, which is checked as well.

(The sentence above deliberately avoids spelling out the call
expression: tests/test_execution_engine.py scans every non-test source
file for that exact text and treats a match as an unsanctioned call
site. Keeping this file out of that allow-list is the point.)

Two gate evaluations are recorded per candidate:

  - the REAL one, with the deployment's actual flags. In the initial
    posture this blocks at "live order flag is not enabled" /
    "ENTRY_DISABLED", which is the honest record of what the system
    would do right now.
  - the HYPOTHETICAL one, with only those two config flags flipped, so
    the log answers the question an operator actually needs before
    enabling anything: "would this candidate have passed every OTHER
    safety check?" Every non-config check -- price deviation, cash,
    duplicate order, allow-list, instrument eligibility, reconciliation,
    UNKNOWN orders -- is evaluated for real against live KIS reads.
"""

import argparse
import os
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import kis_live_trading as klt  # noqa: E402
import paper_strategy_order as pso  # noqa: E402
import risk_config  # noqa: E402
import shadow_audit  # noqa: E402
from brokers.kis_broker import KISPriceUnavailableError  # noqa: E402
from market_data.exchange_registry import (  # noqa: E402
    ExchangeResolutionError,
    build_kis_instrument,
    partition_kis_executable,
    supported_analysis_exchanges,
)
import shadow_mode  # noqa: E402
from brokers.kis_broker import KISBroker, KISBrokerError  # noqa: E402
from config import scalping_strategy_v1_config as strat_cfg  # noqa: E402
from config.live_rollout_config import LiveRolloutConfig  # noqa: E402
from domain.instrument import InstrumentError, build_instrument  # noqa: E402
from domain.order_intent import OrderIntent, OrderIntentError  # noqa: E402
from domain.signal import SignalError, build_signal  # noqa: E402
from execution import order_gate  # noqa: E402
from execution.order_repository import (  # noqa: E402
    FatalRepositoryConnectionError,
)
from execution.secret_redaction import install_logging_redaction  # noqa: E402
from market_data.base import MarketDataProviderError  # noqa: E402
from market_data.kis_validation_provider import (  # noqa: E402
    KISValidationProvider,
    compute_price_deviation_percent,
)
from reconciliation import freshness  # noqa: E402
from reconciliation import snapshot as reconciliation_snapshot  # noqa: E402
from state_store import db as state_db  # noqa: E402

logger = logging.getLogger("shadow_mode")

EXIT_OK = 0
EXIT_ERROR = 1

EXIT_FATAL_DB = 4
# The reconciliation snapshot this pass would have relied on is missing,
# stale, from the future or unreadable. The service unit checks the same
# thing in ExecStartPre; this covers a manual run, which does not go
# through systemd at all.
EXIT_STALE_RECONCILIATION = 5


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


def _audit(run_id, event_type, result, *, symbol, signal_id=None, reason_code=None,
           detail=None, payload=None, now):
    body = dict(payload) if payload else None
    if detail:
        body = body or {}
        body["detail"] = detail
    shadow_audit.record_event(
        shadow_run_id=run_id, event_type=event_type, result=result, symbol=symbol,
        side="buy", signal_id=signal_id, reason_code=reason_code,
        payload=body, now=now,
    )


# The pre-gate checks, in the order the evaluation performs them. The
# Order Gate is deliberately NOT in this list: it is the one step whose
# verdict is never inferred.
STAGE_EXCHANGE = "EXCHANGE"
STAGE_PRICE = "PRICE"
STAGE_ACCOUNT_READ = "ACCOUNT_READ"
STAGE_CASH = "CASH"
STAGE_ORDER_INTENT = "ORDER_INTENT"
STAGE_RECONCILIATION = "RECONCILIATION"

PRE_GATE_STAGES = (
    STAGE_EXCHANGE, STAGE_PRICE, STAGE_ACCOUNT_READ,
    STAGE_CASH, STAGE_ORDER_INTENT, STAGE_RECONCILIATION,
)


class _PreGateProgress:
    """How far a candidate got before the Order Gate.

    Oracle verification found every candidate reporting
    `hypothetical=None`: one was an ARCA listing, the rest ran into an
    unfunded account, and both stop the evaluation before the gate. The
    log then answered "what did the live path decide?" with nothing at
    all -- not "would have been approved", not "would have been
    rejected", just silence, which is the least useful thing to record
    about a day's candidates.

    This tracks the checks that DID run and reports them as an audit
    event. It runs no check of its own, calls no broker method and
    reaches no gate verdict: it only writes down what already happened.
    """

    __slots__ = ("passed", "blocked_at", "blocked_reason")

    def __init__(self):
        self.passed = []
        self.blocked_at = None
        self.blocked_reason = None

    def passed_stage(self, stage):
        if stage not in self.passed:
            self.passed.append(stage)

    def blocked(self, stage, reason_code):
        self.blocked_at = stage
        self.blocked_reason = reason_code

    def not_evaluated(self):
        done = set(self.passed) | ({self.blocked_at} if self.blocked_at else set())
        return [stage for stage in PRE_GATE_STAGES if stage not in done]

    def as_payload(self):
        return {
            "pre_gate_stages_passed": list(self.passed),
            "blocked_at": self.blocked_at,
            "blocked_reason": self.blocked_reason,
            "pre_gate_stages_not_evaluated": self.not_evaluated(),
            # The single most important field: no gate verdict is being
            # claimed here, and no gate was bypassed to produce one.
            "order_gate_evaluated": False,
        }




def shadow_allowed_symbols(rollout):
    """Which symbols Shadow may EVALUATE -- deliberately separate from
    which symbols the live path may TRADE.

    Oracle verification found Shadow silently doing nothing: the loop
    skipped every candidate because it reused the live rollout allow-list,
    and that list is empty in the read-only posture (correctly -- nothing
    may be traded yet). But Shadow places no orders, so gating its
    evaluation on a live-trading control makes the one tool meant to
    observe candidates observe none of them.

    SHADOW_ALLOWED_SYMBOLS governs evaluation:
        unset  -> evaluate every candidate (the useful default; Shadow
                  cannot place an order, so there is nothing to restrict)
        "A,B"  -> evaluate only those
        ""     -> same as unset

    LIVE_ROLLOUT_ALLOWED_SYMBOLS still governs what the live path may
    trade, and the Order Gate still enforces it inside every evaluation --
    so a symbol Shadow evaluates but the rollout does not allow is
    reported as blocked, which is exactly the information an operator
    wants before enabling anything.
    """
    raw = os.environ.get("SHADOW_ALLOWED_SYMBOLS", "").strip()
    if not raw:
        return None
    symbols = frozenset(part.strip().upper() for part in raw.split(",") if part.strip())
    return symbols or None


def _price_reason_of(exc):
    """HIGH-1: unwrap the broker's specific price reason if it survived
    the market-data wrapper; otherwise stay with the generic code."""
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, KISPriceUnavailableError):
        return cause.reason_code
    return "PRICE_UNAVAILABLE"


def _price_detail(exc, exchange_record=None):
    """Operator-facing detail: venue and requested code only -- never a
    raw response, account number or token."""
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, KISPriceUnavailableError):
        return str(cause.diagnostic())
    if exchange_record is not None:
        return (f"{exchange_record.symbol} canonical_exchange="
                f"{exchange_record.exchange.value} "
                f"kis_exchange_code={exchange_record.kis_exchange_code}: {exc}")
    return str(exc)


def _record_excluded(item, *, now):
    """Writes down a candidate the KIS pipeline was not handed.

    No analysis, no KIS read, no gate: the venue alone decides this, and
    it is decided before anything else runs. The record exists so the day
    reads as "analysed, held back, here is why" rather than as a symbol
    that quietly never appeared.
    """
    run_id = shadow_audit.new_run_id()
    _audit(run_id, shadow_audit.KIS_PIPELINE_EXCLUDED, shadow_audit.RESULT_INFO,
           symbol=item.symbol, reason_code=item.reason_code, detail=item.detail,
           payload={"kis_pipeline": False,
                    "supported_exchanges": list(supported_analysis_exchanges())},
           now=now)
    _audit(run_id, shadow_audit.SHADOW_COMPLETED, shadow_audit.RESULT_INFO,
           symbol=item.symbol, reason_code=item.reason_code, now=now)
    return {"symbol": item.symbol, "run_id": run_id,
            "result": shadow_audit.RESULT_INFO,
            "reason_code": item.reason_code,
            "hypothetical": "NOT_APPLICABLE:KIS_PIPELINE_EXCLUDED"}


def _evaluate_symbol(*, symbol, broker, rollout, conn, kis_validation, deployed_commit,
                     validated_commit, allowed_account_no, is_regular_session, now):
    """Returns a dict describing what the live path WOULD have done. No
    order is placed on any code path through this function."""
    run_id = shadow_audit.new_run_id()
    outcome = {"symbol": symbol, "run_id": run_id, "result": shadow_audit.RESULT_BLOCKED,
               "reason_code": None, "hypothetical": None}
    signal = None
    progress = _PreGateProgress()
    signalled = False
    try:
        analysis = pso.analyze_stock(symbol)
        if analysis is None or analysis["score"] < klt.SCORE_THRESHOLD:
            outcome["result"] = shadow_audit.RESULT_INFO
            outcome["reason_code"] = "BELOW_SCORE_THRESHOLD"
            return outcome

        _audit(run_id, shadow_audit.SIGNAL_RECEIVED, shadow_audit.RESULT_INFO, symbol=symbol,
               reason_code="SCORE_THRESHOLD_MET", now=now)
        signalled = True

        try:
            # HIGH-1: resolve the venue instead of assuming NASDAQ.
            instrument, exchange_record = build_kis_instrument(symbol)
            progress.passed_stage(STAGE_EXCHANGE)
            signal = build_signal(
                strategy_id="PAPER_STRATEGY_ORDER_SCORE_V1", strategy_version="v1",
                config_version="live_rollout_v1", code_commit=deployed_commit, symbol=symbol,
                exchange=instrument.exchange, signal_price=analysis["price"],
                score=analysis["score"], entry_reason="score_threshold_breakout",
                valid_for_seconds=klt.SIGNAL_VALID_SECONDS, now=now,
            )
        except ExchangeResolutionError as exc:
            # HIGH-1: fail closed. No default venue, no transport.
            outcome["reason_code"] = exc.reason_code
            progress.blocked(STAGE_EXCHANGE, exc.reason_code)
            _audit(run_id, shadow_audit.INSTRUMENT_BLOCKED, shadow_audit.RESULT_BLOCKED,
                   symbol=symbol, reason_code=exc.reason_code, detail=str(exc), now=now)
            return outcome
        except (InstrumentError, SignalError) as exc:
            outcome["reason_code"] = "INSTRUMENT_INVALID"
            progress.blocked(STAGE_EXCHANGE, "INSTRUMENT_INVALID")
            _audit(run_id, shadow_audit.INSTRUMENT_BLOCKED, shadow_audit.RESULT_BLOCKED,
                   symbol=symbol, reason_code="INSTRUMENT_INVALID", detail=str(exc), now=now)
            return outcome

        try:
            kis_quote = kis_validation.get_price_quote(symbol)
            progress.passed_stage(STAGE_PRICE)
        except MarketDataProviderError as exc:
            # HIGH-1: keep the specific reason when the broker gave one --
            # an empty price on a successful call means the exchange code
            # is probably wrong, which a flat PRICE_UNAVAILABLE hides.
            reason = getattr(exc, "reason_code", None) or _price_reason_of(exc)
            outcome["reason_code"] = reason
            progress.blocked(STAGE_PRICE, reason)
            _audit(run_id, shadow_audit.PRICE_DEVIATION_BLOCKED, shadow_audit.RESULT_BLOCKED,
                   symbol=symbol, signal_id=signal.signal_id, reason_code=reason,
                   detail=_price_detail(exc, exchange_record), now=now)
            return outcome

        try:
            account_snapshot = broker.get_account_snapshot()
            open_orders = broker.get_open_orders()
            progress.passed_stage(STAGE_ACCOUNT_READ)
        except KISBrokerError as exc:
            outcome["reason_code"] = "ACCOUNT_READ_FAILED"
            progress.blocked(STAGE_ACCOUNT_READ, "ACCOUNT_READ_FAILED")
            _audit(run_id, shadow_audit.CASH_BLOCKED, shadow_audit.RESULT_BLOCKED, symbol=symbol,
                   signal_id=signal.signal_id, reason_code="ACCOUNT_READ_FAILED",
                   detail=str(exc), now=now)
            return outcome

        available_usd = account_snapshot.usd_available_for_new_order
        price = kis_quote.price_usd
        quantity = min(int(available_usd // price) if price > 0 else 0,
                       rollout.max_quantity_per_order)
        if quantity < 1:
            outcome["reason_code"] = "INSUFFICIENT_CASH"
            progress.blocked(STAGE_CASH, "INSUFFICIENT_CASH")
            _audit(run_id, shadow_audit.CASH_BLOCKED, shadow_audit.RESULT_BLOCKED, symbol=symbol,
                   signal_id=signal.signal_id, reason_code="INSUFFICIENT_CASH", now=now)
            return outcome

        try:
            order_intent = OrderIntent(
                internal_order_id=f"shadow-{symbol}-{uuid.uuid4().hex[:12]}",
                signal_id=signal.signal_id, strategy_id=signal.strategy_id, symbol=symbol,
                exchange=instrument.exchange, side="buy", quantity=quantity, order_type="limit",
                limit_price=price, stop_price=None, target_price=None, created_at=now,
            )
            progress.passed_stage(STAGE_CASH)
            progress.passed_stage(STAGE_ORDER_INTENT)
        except OrderIntentError as exc:
            outcome["reason_code"] = "ORDER_INTENT_INVALID"
            progress.blocked(STAGE_ORDER_INTENT, "ORDER_INTENT_INVALID")
            _audit(run_id, shadow_audit.INSTRUMENT_BLOCKED, shadow_audit.RESULT_BLOCKED,
                   symbol=symbol, signal_id=signal.signal_id, reason_code="ORDER_INTENT_INVALID",
                   detail=str(exc), now=now)
            return outcome

        try:
            snapshot = reconciliation_snapshot.build_snapshot(
                broker=broker, conn=conn, account_id=account_snapshot.account_id, symbol=symbol,
                now=now, source="shadow_service",
            )
            progress.passed_stage(STAGE_RECONCILIATION)
        except reconciliation_snapshot.ReconciliationUnavailableError as exc:
            outcome["reason_code"] = "RECONCILIATION_UNAVAILABLE"
            progress.blocked(STAGE_RECONCILIATION, "RECONCILIATION_UNAVAILABLE")
            _audit(run_id, shadow_audit.RECONCILIATION_BLOCKED, shadow_audit.RESULT_BLOCKED,
                   symbol=symbol, signal_id=signal.signal_id,
                   reason_code="RECONCILIATION_UNAVAILABLE", detail=str(exc), now=now)
            return outcome

        has_open_order = any((o.get("pdno") or o.get("PDNO")) == symbol for o in open_orders)

        def _ctx(*, live_order_enabled, entry_disabled):
            return order_gate.BuyGateContext(
                execution_broker="kis", live_order_enabled=live_order_enabled,
                entry_disabled=entry_disabled, validated_commit=validated_commit,
                deployed_commit=deployed_commit, kis_account_no=account_snapshot.account_id,
                allowed_account_no=allowed_account_no, order_intent=order_intent,
                instrument=instrument, signal=signal, is_regular_session=is_regular_session,
                kis_price_usd=price, max_price_deviation_percent=rollout.max_price_deviation_percent,
                usd_orderable_cash=available_usd, has_open_order_for_symbol=has_open_order,
                has_order_for_signal_id=False, allowed_symbols=rollout.allowed_symbols,
                reconciliation=snapshot, now=now,
            )

        real_blocked = None
        try:
            order_gate.evaluate_buy_gate(_ctx(
                live_order_enabled=klt_live_order_enabled(), entry_disabled=klt_entry_disabled(),
            ))
        except order_gate.OrderGateBlockedError as exc:
            real_blocked = exc

        hypothetical_blocked = None
        try:
            order_gate.evaluate_buy_gate(_ctx(live_order_enabled=True, entry_disabled=False))
        except order_gate.OrderGateBlockedError as exc:
            hypothetical_blocked = exc

        planned_stop = price * (1 + risk_config.STOP_LOSS_RATE)
        planned_target = price + (price - planned_stop) * strat_cfg.TARGET_1_R_MULTIPLE
        gate_result = "BLOCKED" if hypothetical_blocked else "WOULD_APPROVE"
        shadow_mode.persist(shadow_mode.build_record(
            signal_id=signal.signal_id, strategy_id=signal.strategy_id, strategy_version="v1",
            code_commit=deployed_commit, symbol=symbol, side="buy",
            alpaca_signal_price=signal.signal_price, kis_validation_price=price,
            price_difference_percent=compute_price_deviation_percent(signal.signal_price, price),
            planned_quantity=quantity, planned_limit_price=price, stop_price=planned_stop,
            target_price=planned_target, risk_gate_result=gate_result,
            rejection_reason=str(hypothetical_blocked) if hypothetical_blocked else None,
            account_available_usd=available_usd, existing_position_quantity=None,
            existing_open_order=has_open_order, now=now,
        ))

        if real_blocked is not None:
            outcome["reason_code"] = f"GATE:{real_blocked.code}"
            _audit(run_id, shadow_audit.event_type_for_reason_code(f"GATE:{real_blocked.code}"),
                   shadow_audit.RESULT_BLOCKED, symbol=symbol, signal_id=signal.signal_id,
                   reason_code=f"GATE:{real_blocked.code}", detail=str(real_blocked), now=now)
        if hypothetical_blocked is not None:
            outcome["hypothetical"] = f"BLOCKED:{hypothetical_blocked.code}"
            _audit(run_id, shadow_audit.GATE_REJECTED, shadow_audit.RESULT_BLOCKED, symbol=symbol,
                   signal_id=signal.signal_id, reason_code=f"HYPOTHETICAL:{hypothetical_blocked.code}",
                   detail=str(hypothetical_blocked), now=now)
        else:
            outcome["hypothetical"] = "WOULD_APPROVE"
            _audit(run_id, shadow_audit.GATE_APPROVED, shadow_audit.RESULT_INFO, symbol=symbol,
                   signal_id=signal.signal_id, reason_code="HYPOTHETICAL_APPROVED", now=now)
            _audit(run_id, shadow_audit.EXECUTION_PLANNED, shadow_audit.RESULT_INFO, symbol=symbol,
                   signal_id=signal.signal_id, reason_code="NO_ORDER_PLACED_SHADOW_MODE",
                   detail=f"quantity={quantity} limit={price}", now=now)
        if real_blocked is None and hypothetical_blocked is None:
            outcome["result"] = shadow_audit.RESULT_APPROVED
        return outcome
    except Exception as exc:  # noqa: BLE001 -- every run must end in a recorded outcome
        outcome["result"] = shadow_audit.RESULT_ERROR
        outcome["reason_code"] = "UNEXPECTED"
        _audit(run_id, shadow_audit.SHADOW_ERROR, shadow_audit.RESULT_ERROR, symbol=symbol,
               signal_id=signal.signal_id if signal is not None else None,
               reason_code="UNEXPECTED", detail=str(exc), now=now)
        logger.exception("shadow evaluation failed for %s", symbol)
        return outcome
    finally:
        if signalled and outcome["hypothetical"] is None:
            # The evaluation stopped before the Order Gate. Record what
            # the pre-gate checks actually established -- never a gate
            # verdict, and without re-running anything.
            outcome["hypothetical"] = f"NOT_EVALUATED:{progress.blocked_at or 'UNKNOWN'}"
            _audit(run_id, shadow_audit.HYPOTHETICAL_INCOMPLETE, shadow_audit.RESULT_INFO,
                   symbol=symbol, signal_id=signal.signal_id if signal is not None else None,
                   reason_code=f"HYPOTHETICAL_NOT_EVALUATED:{progress.blocked_at or 'UNKNOWN'}",
                   payload=progress.as_payload(), now=now)
        _audit(run_id, shadow_audit.SHADOW_COMPLETED, outcome["result"], symbol=symbol,
               signal_id=signal.signal_id if signal is not None else None,
               reason_code=outcome["reason_code"], now=now)


def klt_live_order_enabled():
    import os
    return str(os.environ.get("KIS_LIVE_ORDER_ENABLED", "")).strip().lower() in ("1", "true", "yes", "on")


def klt_entry_disabled():
    import os
    return str(os.environ.get("ENTRY_DISABLED", "")).strip().lower() in ("1", "true", "yes", "on")


def run_once(*, broker=None, rollout=None, watchlist=None, now=None, conn=None):
    current = now or datetime.now(timezone.utc)
    broker = broker or KISBroker()
    rollout = rollout or LiveRolloutConfig.from_env()
    deployed_commit = klt._get_deployed_commit()
    validated_commit = klt._get_validated_commit()
    allowed_account_no = klt._get_allowed_account_no()
    is_regular_session = (
        pso.get_us_market_session() == "regular" if rollout.regular_session_only else True
    )
    symbols = watchlist if watchlist is not None else pso.load_watchlist()
    kis_validation = KISValidationProvider(
        # HIGH-1: the provider resolves each symbol's real venue too.
        broker, instrument_lookup=lambda s: build_kis_instrument(s)[0],
    )
    owns_conn = conn is None
    conn = conn or state_db.open_db()
    outcomes = []
    evaluable = shadow_allowed_symbols(rollout)
    requested = [s for s in symbols if evaluable is None or s in evaluable]

    # The analysis side and the KIS-executable side are not the same set.
    # Candidates whose venue has no KIS order exchange code never enter
    # the KIS pipeline: they cost a scored analysis pass and a KIS read
    # only to end in UNSUPPORTED_EXCHANGE. They stay in the analysis
    # output and are recorded here, so "analysed but not executable" is
    # visible rather than silent.
    executable, excluded = partition_kis_executable(requested)
    try:
        for item in excluded:
            outcomes.append(_record_excluded(item, now=current))
        for symbol, _record in executable:
            outcomes.append(_evaluate_symbol(
                symbol=symbol, broker=broker, rollout=rollout, conn=conn,
                kis_validation=kis_validation, deployed_commit=deployed_commit,
                validated_commit=validated_commit, allowed_account_no=allowed_account_no,
                is_regular_session=is_regular_session, now=current,
            ))
    finally:
        if owns_conn:
            conn.close()
    return outcomes


def main(argv=None):
    parser = argparse.ArgumentParser(description="KIS Shadow Mode evaluation pass (places no orders)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    install_logging_redaction()

    # Same module the service unit and the timer-approval script use, so
    # a manual `python scripts/run_shadow_mode.py` cannot evaluate
    # against an account reconciliation nobody has refreshed.
    try:
        snapshot = freshness.evaluate()
    except freshness.SnapshotUnusable as exc:
        logger.error(
            "reconciliation snapshot refused: reason=%s detail=%s "
            "shadow_run_suppressed=true", exc.reason_code, exc.detail,
        )
        return EXIT_STALE_RECONCILIATION
    logger.info(
        "reconciliation snapshot accepted: %s",
        " ".join(f"{k}={v}" for k, v in snapshot.as_log_fields().items()),
    )

    try:
        outcomes = run_once()
    except FatalRepositoryConnectionError as exc:
        # CODEX-058: the order-state connection could neither be rolled
        # back nor closed, so this process may still hold a SQLite write
        # lock that blocks every other writer. HALT is already set by the
        # repository; exiting non-zero is what actually releases the lock
        # (the OS reclaims the descriptor) and lets systemd's
        # Restart=on-failure bring the service back cleanly.
        _fail_stop("shadow entry evaluation", exc)
        return EXIT_FATAL_DB
    except Exception as exc:  # noqa: BLE001 -- service entrypoint
        logger.exception("shadow pass failed: %s", exc)
        return EXIT_ERROR
    for outcome in outcomes:
        logger.info(
            "shadow %s: result=%s reason=%s hypothetical=%s",
            outcome["symbol"], outcome["result"], outcome["reason_code"], outcome["hypothetical"],
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
