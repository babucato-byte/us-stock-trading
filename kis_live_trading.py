"""KIS live-order buy-entry pipeline -- the actual cutover entrypoint
spec §1/§25 describes: Alpaca-sourced candidates/signals -> KIS price/
account re-validation -> Sizing -> central Order Gate -> Execution
Engine -> KIS. This module is NEW (not a modification of
paper_strategy_order.py) so the existing, extensively-tested Alpaca
paper order path stays completely untouched -- exactly the same
"grandfathered legacy path, new path is additive" pattern this
codebase's CODEX-040 cycle already established for the Alpaca-internal
live-entry pipeline.

Candidate/score discovery deliberately REUSES paper_strategy_order.
load_watchlist()/analyze_stock() (spec §5: 기존 기능 재사용) -- this
module adds nothing to how candidates are found or scored, only to what
happens to a qualifying candidate afterward.

SCOPE NOTE (documented, not silently omitted): this module implements
the BUY-entry path only. Sell-side automation (stop-loss/take-profit/
partial-exit/trailing-stop/time-exit/EOD-exit monitoring against live
KIS positions) is NOT implemented here -- spec's second message
describing that full strategy lifecycle was truncated mid-transmission
(see docs/autonomous/DECISION_LOG.md's KIS migration section) and
implementing stop-loss/exit logic by guessing at the missing
specification would be exactly the kind of safety-critical guess this
project's conventions forbid. `execution_engine.submit_sell_order()`
and `order_gate.evaluate_sell_gate()` already exist and are fully
tested (tests/test_execution_engine_kis.py, tests/test_order_gate.py)
-- only the strategy-side "when should we sell" decision logic and its
wiring into this pipeline remain.

RESIDUAL RISK (documented): `_build_instrument()` below defaults
leveraged/inverse/otc to False for any symbol on the operator-curated
`live_rollout.allowed_symbols` list -- there is no automated leveraged/
inverse/OTC classifier in this codebase (universe.csv carries no such
field), so this pipeline currently relies entirely on the operator
curating that list to exclude such instruments, not on independent
code-level detection. See docs/live_review/TBD_REVIEW_RECOMMENDATIONS.md.
"""

import logging
import os
import uuid
from datetime import datetime, timezone

import kis_position_manager
import paper_strategy_order as pso
import risk_config
import shadow_audit
import shadow_mode
from brokers.kis_broker import (
    KISAmbiguousResponseError,
    KISBrokerError,
    KISOrderableCashUnavailableError,
)
from config import scalping_strategy_v1_config as strat_cfg
from config.live_rollout_config import LiveRolloutConfig, LiveRolloutConfigError
from domain.cash_sizing import (
    INSUFFICIENT_CASH,
    ORDERABLE_CASH_UNAVAILABLE,
    whole_shares_affordable,
)
from domain.instrument import Instrument, InstrumentError, build_instrument
from domain.order_intent import OrderIntent, OrderIntentError
from domain.signal import Signal, SignalError, build_signal
from execution import entry_limits, execution_engine, order_gate
from market_data.exchange_registry import (
    ExchangeResolutionError,
    build_kis_instrument,
)
from execution.execution_engine import ExecutionEngineError
from execution.order_repository import FatalRepositoryConnectionError
from market_data.kis_validation_provider import (
    KISValidationProvider,
    compute_price_deviation_percent,
)
from market_data.base import MarketDataProviderError
from market_hours import us_trading_day
from operations import kill_switch as ops_kill_switch
from s1_live import candidate_source as s1_candidate_source
from state_store import db as state_db

logger = logging.getLogger(__name__)

SCORE_THRESHOLD = 70  # matches paper_strategy_order.py's existing threshold
SIGNAL_VALID_SECONDS = 120


class KISLiveTradingError(Exception):
    """Raised when a structural precondition (config, commit match) is
    invalid before any per-symbol processing even begins."""


_CYCLE_LEVEL_SYMBOL = "__CYCLE__"


def _persist_blocked_record(*, symbol, side="buy", strategy_id="PAPER_STRATEGY_ORDER_SCORE_V1",
                             signal_price=None, kis_price=None, price_diff_percent=None,
                             planned_quantity=None, planned_limit_price=None, stop_price=None,
                             target_price=None, risk_gate_result, rejection_reason,
                             account_available_usd=None, existing_position_quantity=None,
                             existing_open_order=False, now):
    """Shadow Mode completeness (CODEX-review MEDIUM finding): every
    category the pipeline can block/skip on -- config block, signal
    expiry, symbol block, price deviation, insufficient balance,
    reconciliation failure, UNKNOWN present, duplicate order, HALT,
    Order Gate rejection -- must produce a durable Shadow Mode record,
    not just the subset that happened to already have a fully-built
    signal/order_intent in scope. This helper is deliberately tolerant
    of missing data (every non-required field defaults to None) so it
    can be called from the earliest possible point in the pipeline --
    including cycle-level structural blocks (HALT, config invalid, ...)
    that occur before any symbol/signal is ever built."""
    shadow_mode.persist(shadow_mode.build_record(
        signal_id=f"{symbol}-{now.isoformat()}", strategy_id=strategy_id, strategy_version="v1",
        code_commit=os.environ.get("DEPLOYED_COMMIT") or "", symbol=symbol, side=side,
        alpaca_signal_price=signal_price, kis_validation_price=kis_price,
        price_difference_percent=price_diff_percent, planned_quantity=planned_quantity,
        planned_limit_price=planned_limit_price, stop_price=stop_price, target_price=target_price,
        risk_gate_result=risk_gate_result, rejection_reason=rejection_reason,
        account_available_usd=account_available_usd,
        existing_position_quantity=existing_position_quantity,
        existing_open_order=existing_open_order, now=now,
    ))


def _audit(run_id, event_type, result, *, symbol=None, signal_id=None, internal_order_id=None,
            reason_code=None, detail=None, now):
    """CODEX-048: one durable audit row per evaluation step, in SQLite,
    for BOTH the block and the approve paths. `detail` is free text from
    an underlying exception and is redacted at the store boundary.

    Fails CLOSED: if the event cannot be persisted, shadow_audit.
    handle_audit_failure() retries the terminal SHADOW_ERROR, alerts, and
    raises ShadowAuditFailure -- the evaluation is abandoned rather than
    continuing with an incomplete audit trail."""
    try:
        shadow_audit.record_event(
            shadow_run_id=run_id, event_type=event_type, result=result, symbol=symbol,
            side="buy", signal_id=signal_id, internal_order_id=internal_order_id,
            reason_code=reason_code, payload={"detail": detail} if detail else None, now=now,
        )
    except shadow_audit.ShadowAuditError as exc:
        shadow_audit.handle_audit_failure(
            exc, shadow_run_id=run_id, symbol=symbol, side="buy", stage=event_type,
        )


def _finalize(run_id, outcome, *, symbol, now):
    """CODEX-053: terminal events go through shadow_audit.finalize_audit_run(),
    which is idempotent for the same event and refuses a conflicting one --
    so a run cannot end twice however many code paths think they own it."""
    try:
        shadow_audit.finalize_audit_run(
            audit_run_id=run_id,
            terminal_event=shadow_audit.terminal_event_for(outcome["result"]),
            internal_order_id=outcome.get("internal_order_id"), action="buy", symbol=symbol,
            side="buy", reason_code=outcome["reason_code"],
            payload={"detail": outcome["detail"]} if outcome.get("detail") else None, now=now,
        )
    except shadow_audit.ShadowAuditError as exc:
        shadow_audit.handle_audit_failure(
            exc, shadow_run_id=run_id, symbol=symbol, side="buy", stage="terminal",
        )


def _audit_cycle_block(run_id, event_type, reason_code, detail, *, now):
    """A cycle-level structural block. Emits the specific block event AND
    exactly one terminal SHADOW_BLOCKED, so no run is ever left without a
    final outcome event."""
    _audit(run_id, event_type, shadow_audit.RESULT_BLOCKED, symbol=_CYCLE_LEVEL_SYMBOL,
           reason_code=reason_code, detail=detail, now=now)
    _finalize(run_id, {"result": shadow_audit.RESULT_BLOCKED, "reason_code": reason_code,
                       "detail": detail}, symbol=_CYCLE_LEVEL_SYMBOL, now=now)


def _build_instrument(symbol, allowed_symbols):
    """See module docstring's RESIDUAL RISK note. `symbol` must already
    be on `allowed_symbols` (checked by the caller before this is
    invoked) -- leveraged/inverse/otc are trusted-False for exactly that
    reason, not independently detected.

    HIGH-1: the venue is RESOLVED, never assumed. This used to hardcode
    NASDAQ, which made every NYSE/AMEX name unpriceable (KIS answers a
    wrong-exchange quote with rt_cd=0 and an empty price). An unresolved
    symbol raises, so the caller blocks with EXCHANGE_UNKNOWN and places
    no order."""
    instrument, _record = build_kis_instrument(symbol)
    return instrument


def _get_deployed_commit():
    return os.environ.get("DEPLOYED_COMMIT", "")


def _get_validated_commit():
    return os.environ.get("VALIDATED_COMMIT", "")


def _get_allowed_account_no():
    return os.environ.get("KIS_ALLOWED_ACCOUNT_NO", "")


def run_live_buy_entry_cycle(*, broker, live_rollout=None, now=None,
                             candidate_source=None):
    """Returns a results dict: {"submitted": [...], "blocked": [(symbol, reason)], "skipped": [...]}.
    Never raises for a per-symbol failure -- only for a structural
    precondition failure that makes the WHOLE cycle unsafe to run at all
    (config invalid, halted, commit mismatch).

    `candidate_source` supplies the two things this cycle used to answer
    inline: which symbols to evaluate, and which symbols the Order Gate
    is told are allowed. Omitting it resolves the source from the
    environment, which yields the legacy watchlist source unless
    `S1_LIVE_SOURCE_ENABLED` is explicitly set -- so the default path is
    the one that shipped, symbol for symbol.

    Only the SOURCE is pluggable. Every gate below -- allow-list check,
    price re-validation, orderable cash, duplicate order, entry limits,
    kill switch, reconciliation, the Execution Engine -- is shared by
    every source and exists exactly once. A second candidate source must
    never mean a second pipeline: two pipelines are two ideas of what is
    safe, and they diverge silently.
    """
    current = now or datetime.now(timezone.utc)
    results = {"submitted": [], "blocked": [], "skipped": []}
    cycle_run_id = shadow_audit.new_run_id()

    rollout = live_rollout or LiveRolloutConfig.from_env()
    try:
        rollout.validate()
    except LiveRolloutConfigError as exc:
        reason = f"live_rollout config invalid, refusing to run: {exc}"
        _persist_blocked_record(
            symbol=_CYCLE_LEVEL_SYMBOL, risk_gate_result="BLOCKED", rejection_reason=reason, now=current,
        )
        _audit_cycle_block(cycle_run_id, shadow_audit.CONFIG_BLOCKED, "CONFIG_INVALID", reason, now=current)
        raise KISLiveTradingError(reason) from exc

    if not rollout.enabled:
        reason = "live_rollout.enabled is False -- KIS live entries are not active"
        _persist_blocked_record(
            symbol=_CYCLE_LEVEL_SYMBOL, risk_gate_result="BLOCKED", rejection_reason=reason, now=current,
        )
        _audit_cycle_block(cycle_run_id, shadow_audit.CONFIG_BLOCKED, "ROLLOUT_DISABLED", reason, now=current)
        raise KISLiveTradingError(reason)

    if ops_kill_switch.is_halted():
        reason = "operations HALT is set -- no automatic order attempts permitted"
        _persist_blocked_record(
            symbol=_CYCLE_LEVEL_SYMBOL, risk_gate_result="HALT", rejection_reason=reason, now=current,
        )
        _audit_cycle_block(cycle_run_id, shadow_audit.HALT_BLOCKED, "HALT", reason, now=current)
        raise KISLiveTradingError(reason)
    if not ops_kill_switch.is_entry_allowed():
        reason = "ENTRY_OFF (kill_switch_state) is set -- new entries blocked"
        _persist_blocked_record(
            symbol=_CYCLE_LEVEL_SYMBOL, risk_gate_result="BLOCKED", rejection_reason=reason, now=current,
        )
        _audit_cycle_block(cycle_run_id, shadow_audit.HALT_BLOCKED, "ENTRY_OFF", reason, now=current)
        raise KISLiveTradingError(reason)

    validated_commit = _get_validated_commit()
    deployed_commit = _get_deployed_commit()
    if not validated_commit or validated_commit != deployed_commit:
        reason = (
            f"validated commit {validated_commit!r} does not match deployed commit "
            f"{deployed_commit!r} -- refusing to run an unvalidated deployment"
        )
        _persist_blocked_record(
            symbol=_CYCLE_LEVEL_SYMBOL, risk_gate_result="BLOCKED", rejection_reason=reason, now=current,
        )
        _audit_cycle_block(cycle_run_id, shadow_audit.CONFIG_BLOCKED, "COMMIT_MISMATCH", reason, now=current)
        raise KISLiveTradingError(reason)

    allowed_account_no = _get_allowed_account_no()
    if not allowed_account_no:
        reason = "KIS_ALLOWED_ACCOUNT_NO is not configured -- refusing to run"
        _persist_blocked_record(
            symbol=_CYCLE_LEVEL_SYMBOL, risk_gate_result="BLOCKED", rejection_reason=reason, now=current,
        )
        _audit_cycle_block(cycle_run_id, shadow_audit.CONFIG_BLOCKED, "ACCOUNT_UNCONFIGURED", reason, now=current)
        raise KISLiveTradingError(reason)

    is_regular_session = pso.get_us_market_session() == "regular" if rollout.regular_session_only else True

    # Resolved AFTER every structural precondition above, so a cycle that
    # was going to refuse anyway never reads a candidate file.
    # `watchlist_module=pso` hands over THIS module's own reference. It
    # must not be re-imported inside the source: test_ai_analysis.py
    # pops "paper_strategy_order" from sys.modules and leaves it popped,
    # so a fresh import would build a different module object than the
    # one `klt.pso` -- and therefore every existing monkeypatch -- uses.
    source = candidate_source or s1_candidate_source.resolve(
        rollout, trading_day=us_trading_day(current), watchlist_module=pso)
    # One evaluation of the allow-list per cycle. Re-reading it per symbol
    # would let the set change underneath a cycle that had already made
    # decisions against the earlier value.
    allowed_symbols = source.allowed_symbols()
    logger.info("candidate source: %s", source.describe())

    watchlist = source.symbols()
    kis_validation = KISValidationProvider(broker, instrument_lookup=lambda s: _build_instrument(s, allowed_symbols))

    conn = state_db.open_db()
    try:
        for symbol in watchlist:
            # CODEX-048: one shadow_run_id per symbol evaluation ties every
            # step of that evaluation together, and the finally-block below
            # guarantees exactly one terminal event per run -- there is no
            # path (block, approve, or unexpected exception) that leaves a
            # run without a recorded outcome.
            run_id = shadow_audit.new_run_id()
            outcome = {"result": shadow_audit.RESULT_BLOCKED, "reason_code": None,
                       "detail": None, "internal_order_id": None}
            terminal_recorded = False
            try:
                if symbol not in allowed_symbols:
                    results["skipped"].append((symbol, "not in live_rollout.allowed_symbols"))
                    _persist_blocked_record(
                        symbol=symbol, risk_gate_result="BLOCKED",
                        rejection_reason="symbol not in live_rollout.allowed_symbols", now=current,
                    )
                    outcome["reason_code"] = "SYMBOL_NOT_ALLOWED"
                    _audit(run_id, shadow_audit.INSTRUMENT_BLOCKED, shadow_audit.RESULT_BLOCKED,
                           symbol=symbol, reason_code="SYMBOL_NOT_ALLOWED", now=current)
                    continue

                analysis = pso.analyze_stock(symbol)
                if analysis is None or analysis["score"] < SCORE_THRESHOLD:
                    results["skipped"].append((symbol, "did not meet score threshold"))
                    outcome["result"] = shadow_audit.RESULT_INFO
                    outcome["reason_code"] = "BELOW_SCORE_THRESHOLD"
                    continue

                _audit(run_id, shadow_audit.SIGNAL_RECEIVED, shadow_audit.RESULT_INFO, symbol=symbol,
                       reason_code="SCORE_THRESHOLD_MET", now=current)

                try:
                    instrument = _build_instrument(symbol, allowed_symbols)
                    signal = build_signal(
                        strategy_id="PAPER_STRATEGY_ORDER_SCORE_V1", strategy_version="v1",
                        config_version="live_rollout_v1", code_commit=deployed_commit,
                        symbol=symbol, exchange=instrument.exchange, signal_price=analysis["price"],
                        score=analysis["score"], entry_reason="score_threshold_breakout",
                        valid_for_seconds=SIGNAL_VALID_SECONDS, now=current,
                    )
                except (InstrumentError, SignalError) as exc:
                    reason = f"signal/instrument construction failed: {exc}"
                    results["blocked"].append((symbol, reason))
                    _persist_blocked_record(
                        symbol=symbol, signal_price=analysis["price"], risk_gate_result="BLOCKED",
                        rejection_reason=reason, now=current,
                    )
                    outcome["reason_code"] = "INSTRUMENT_INVALID"
                    _audit(run_id, shadow_audit.INSTRUMENT_BLOCKED, shadow_audit.RESULT_BLOCKED,
                           symbol=symbol, reason_code="INSTRUMENT_INVALID", detail=reason, now=current)
                    continue

                try:
                    kis_quote = kis_validation.get_price_quote(symbol)
                except MarketDataProviderError as exc:
                    reason = f"KIS price re-check failed: {exc}"
                    results["blocked"].append((symbol, reason))
                    _persist_blocked_record(
                        symbol=symbol, signal_price=signal.signal_price, risk_gate_result="BLOCKED",
                        rejection_reason=reason, now=current,
                    )
                    outcome["reason_code"] = "PRICE_UNAVAILABLE"
                    _audit(run_id, shadow_audit.PRICE_DEVIATION_BLOCKED, shadow_audit.RESULT_BLOCKED,
                           symbol=symbol, signal_id=signal.signal_id,
                           reason_code="PRICE_UNAVAILABLE", detail=reason, now=current)
                    continue

                try:
                    account_snapshot = broker.get_account_snapshot()
                except KISBrokerError as exc:
                    reason = f"KIS account read failed: {exc}"
                    results["blocked"].append((symbol, reason))
                    _persist_blocked_record(
                        symbol=symbol, signal_price=signal.signal_price, kis_price=kis_quote.price_usd,
                        risk_gate_result="BLOCKED", rejection_reason=reason, now=current,
                    )
                    outcome["reason_code"] = "ACCOUNT_READ_FAILED"
                    _audit(run_id, shadow_audit.CASH_BLOCKED, shadow_audit.RESULT_BLOCKED, symbol=symbol,
                           signal_id=signal.signal_id, reason_code="ACCOUNT_READ_FAILED",
                           detail=reason, now=current)
                    continue

                # ORACLE-CASH-01: the same per-candidate orderable-amount
                # read the Shadow path uses. The account snapshot carries
                # no cash figure (TTTS3012R does not return one), and KIS
                # answers orderable cash per (symbol, exchange, limit
                # price) -- so it is asked at `buffered_price`, the exact
                # price the OrderIntent below is built with.
                buffered_price = kis_quote.price_usd
                try:
                    # One read per candidate; reused for sizing, the gate
                    # context and the shadow record below.
                    available_usd = broker.get_orderable_usd(instrument, buffered_price)
                except KISOrderableCashUnavailableError as exc:
                    reason = f"KIS orderable-amount read unusable: {exc.diagnostic()}"
                    results["blocked"].append((symbol, reason))
                    _persist_blocked_record(
                        symbol=symbol, signal_price=signal.signal_price, kis_price=kis_quote.price_usd,
                        risk_gate_result="BLOCKED", rejection_reason=reason, now=current,
                    )
                    outcome["reason_code"] = ORDERABLE_CASH_UNAVAILABLE
                    _audit(run_id, shadow_audit.CASH_BLOCKED, shadow_audit.RESULT_BLOCKED, symbol=symbol,
                           signal_id=signal.signal_id, reason_code=ORDERABLE_CASH_UNAVAILABLE,
                           detail=reason, now=current)
                    continue

                balance_qty = whole_shares_affordable(available_usd, buffered_price)
                quantity = min(balance_qty, rollout.max_quantity_per_order)
                if quantity < 1:
                    reason = "insufficient KIS orderable cash for even 1 share"
                    results["blocked"].append((symbol, reason))
                    _persist_blocked_record(
                        symbol=symbol, signal_price=signal.signal_price, kis_price=kis_quote.price_usd,
                        account_available_usd=available_usd, risk_gate_result="BLOCKED",
                        rejection_reason=reason, now=current,
                    )
                    outcome["reason_code"] = INSUFFICIENT_CASH
                    _audit(run_id, shadow_audit.CASH_BLOCKED, shadow_audit.RESULT_BLOCKED, symbol=symbol,
                           signal_id=signal.signal_id, reason_code=INSUFFICIENT_CASH,
                           detail=reason, now=current)
                    continue

                try:
                    order_intent = OrderIntent(
                        internal_order_id=f"kislive-{symbol}-{uuid.uuid4().hex[:12]}",
                        signal_id=signal.signal_id, strategy_id=signal.strategy_id, symbol=symbol,
                        exchange=instrument.exchange, side="buy", quantity=quantity, order_type="limit",
                        limit_price=buffered_price, stop_price=None, target_price=None, created_at=current,
                    )
                except OrderIntentError as exc:
                    reason = f"order intent construction failed: {exc}"
                    results["blocked"].append((symbol, reason))
                    _persist_blocked_record(
                        symbol=symbol, signal_price=signal.signal_price, kis_price=buffered_price,
                        account_available_usd=available_usd, risk_gate_result="BLOCKED",
                        rejection_reason=reason, now=current,
                    )
                    outcome["reason_code"] = "ORDER_INTENT_INVALID"
                    _audit(run_id, shadow_audit.INSTRUMENT_BLOCKED, shadow_audit.RESULT_BLOCKED,
                           symbol=symbol, signal_id=signal.signal_id,
                           reason_code="ORDER_INTENT_INVALID", detail=reason, now=current)
                    continue

                try:
                    open_orders = broker.get_open_orders()
                except KISBrokerError as exc:
                    reason = f"KIS open-orders read failed: {exc}"
                    results["blocked"].append((symbol, reason))
                    _persist_blocked_record(
                        symbol=symbol, signal_price=signal.signal_price, kis_price=buffered_price,
                        planned_quantity=order_intent.quantity, planned_limit_price=order_intent.limit_price,
                        account_available_usd=available_usd, risk_gate_result="BLOCKED",
                        rejection_reason=reason, now=current,
                    )
                    outcome["reason_code"] = "OPEN_ORDER_READ_FAILED"
                    _audit(run_id, shadow_audit.RECONCILIATION_BLOCKED, shadow_audit.RESULT_BLOCKED,
                           symbol=symbol, signal_id=signal.signal_id,
                           internal_order_id=order_intent.internal_order_id,
                           reason_code="OPEN_ORDER_READ_FAILED", detail=reason, now=current)
                    continue
                has_open_order_for_symbol = any(
                    (o.get("pdno") or o.get("PDNO")) == symbol for o in open_orders
                )

                try:
                    existing_positions = broker.get_positions()
                except KISBrokerError:
                    existing_positions = []
                existing_position_qty = next(
                    (p.quantity for p in existing_positions if p.symbol == symbol), 0
                )
                planned_stop_price = buffered_price * (1 + risk_config.STOP_LOSS_RATE)
                planned_risk_per_share = buffered_price - planned_stop_price
                planned_target_price = buffered_price + planned_risk_per_share * strat_cfg.TARGET_1_R_MULTIPLE
                price_diff_percent = compute_price_deviation_percent(signal.signal_price, kis_quote.price_usd)

                def _buy_ctx_builder(
                    reconciliation,
                    signal=signal, instrument=instrument, order_intent=order_intent,
                    kis_price=kis_quote.price_usd, available_usd=available_usd,
                    has_open_order_for_symbol=has_open_order_for_symbol,
                ):
                    # Collected inside the builder, so it is read at gate
                    # time rather than at candidate-selection time -- the
                    # execution engine has already registered this
                    # attempt's idempotency row by now, which is why the
                    # attempt excludes itself from its own counts.
                    limits = entry_limits.collect(
                        broker=broker, conn=conn, rollout=rollout, now=current,
                        exclude_internal_order_id=order_intent.internal_order_id,
                    )
                    return order_gate.BuyGateContext(
                        execution_broker="kis", live_order_enabled=True, entry_disabled=False,
                        validated_commit=validated_commit, deployed_commit=deployed_commit,
                        kis_account_no=account_snapshot.account_id, allowed_account_no=allowed_account_no,
                        order_intent=order_intent, instrument=instrument, signal=signal,
                        is_regular_session=is_regular_session, kis_price_usd=kis_price,
                        max_price_deviation_percent=rollout.max_price_deviation_percent,
                        usd_orderable_cash=available_usd, has_open_order_for_symbol=has_open_order_for_symbol,
                        has_order_for_signal_id=False, allowed_symbols=allowed_symbols,
                        # CODEX-044: supplied BY the Execution Engine from its
                        # own live KIS reads -- this pipeline cannot assert
                        # reconciliation status, only pass through what the
                        # engine actually observed.
                        reconciliation=reconciliation,
                        entry_limits=limits,
                        now=current,
                    )

                def _shadow_record(risk_gate_result, rejection_reason=None):
                    return shadow_mode.build_record(
                        signal_id=signal.signal_id, strategy_id=signal.strategy_id,
                        strategy_version="v1", code_commit=deployed_commit, symbol=symbol, side="buy",
                        alpaca_signal_price=signal.signal_price, kis_validation_price=kis_quote.price_usd,
                        price_difference_percent=price_diff_percent, planned_quantity=order_intent.quantity,
                        planned_limit_price=order_intent.limit_price, stop_price=planned_stop_price,
                        target_price=planned_target_price, risk_gate_result=risk_gate_result,
                        rejection_reason=rejection_reason, account_available_usd=available_usd,
                        existing_position_quantity=existing_position_qty,
                        existing_open_order=has_open_order_for_symbol, now=current,
                    )

                try:
                    # CODEX-048: audit_run_id lets the Execution Engine
                    # record GATE_APPROVED and EXECUTION_PLANNED BEFORE it
                    # calls the broker. Recording them here, after this
                    # call returns, would leave a crash during the broker
                    # call with no audit of the approval that authorized
                    # an order that may already have reached KIS.
                    result = execution_engine.submit_buy_order(
                        order_intent=order_intent, buy_gate_context_builder=_buy_ctx_builder,
                        conn=conn, broker=broker, instrument=instrument,
                        account_id=account_snapshot.account_id, now=current,
                        audit_run_id=run_id,
                    )
                    results["submitted"].append(symbol)
                    shadow_mode.persist(_shadow_record("APPROVED"))
                    # GATE_APPROVED and EXECUTION_PLANNED were already
                    # recorded by the engine, before the transport call.
                    outcome["result"] = shadow_audit.RESULT_APPROVED
                    outcome["reason_code"] = "APPROVED"
                    # spec: "매수 체결 이후 포지션 관리는 KIS 실제 보유수량과
                    # 평균체결가를 기준으로 한다" -- create the positions/
                    # lifecycle.py row now so kis_position_manager.py's sync
                    # cycle can pick up the fill and start managing stop/
                    # target/time/EOD exits (see kis_position_manager.py's
                    # module docstring for the full rationale).
                    try:
                        kis_position_manager.create_kis_position_after_buy(
                            strategy_id=order_intent.strategy_id, strategy_version="v1", symbol=symbol,
                            quantity=order_intent.quantity, client_order_id=order_intent.internal_order_id,
                            broker_order_id=result.execution_record.broker_order_id, now=current,
                        )
                    except Exception as exc:
                        # Position tracking failure must never be treated as
                        # order failure -- the KIS order already succeeded.
                        # Surfaced via results["blocked"] as a warning entry so
                        # it's visible, but the symbol stays in "submitted".
                        results["blocked"].append((symbol, f"WARNING: position tracking failed after successful buy: {exc}"))
                except ExecutionEngineError as exc:
                    results["blocked"].append((symbol, str(exc)))
                    shadow_mode.persist(_shadow_record("BLOCKED", str(exc)))
                    outcome["reason_code"] = exc.reason_code or "GATE"
                    _audit(run_id, shadow_audit.event_type_for_reason_code(exc.reason_code),
                           shadow_audit.RESULT_BLOCKED, symbol=symbol, signal_id=signal.signal_id,
                           internal_order_id=order_intent.internal_order_id,
                           reason_code=exc.reason_code, detail=str(exc), now=current)
                except KISAmbiguousResponseError as exc:
                    results["blocked"].append((symbol, f"ambiguous KIS response, order status UNKNOWN: {exc}"))
                    shadow_mode.persist(_shadow_record("AMBIGUOUS", str(exc)))
                    # The terminal SHADOW_ERROR is written once, by the
                    # finally-block below. Writing it here as well gave
                    # this run TWO terminal events.
                    outcome["result"] = shadow_audit.RESULT_ERROR
                    outcome["reason_code"] = "AMBIGUOUS_RESPONSE"
                    outcome["detail"] = str(exc)
                except KISBrokerError as exc:
                    results["blocked"].append((symbol, f"KIS order rejected: {exc}"))
                    shadow_mode.persist(_shadow_record("REJECTED", str(exc)))
                    outcome["reason_code"] = "BROKER_REJECTED"
                    _audit(run_id, shadow_audit.GATE_REJECTED, shadow_audit.RESULT_BLOCKED, symbol=symbol,
                           signal_id=signal.signal_id,
                           internal_order_id=order_intent.internal_order_id,
                           reason_code="BROKER_REJECTED", detail=str(exc), now=current)
            except shadow_audit.ShadowAuditFailure as exc:
                # handle_audit_failure() already recorded the terminal
                # SHADOW_ERROR and alerted. Do not write a second terminal
                # event for this run.
                terminal_recorded = True
                results["blocked"].append((symbol, f"shadow audit failure: {exc}"))
            except FatalRepositoryConnectionError:
                # CODEX-059: a fatal connection fault aborts the WHOLE
                # cycle -- no further symbols are evaluated -- and reaches
                # the entrypoint unchanged so the process fail-stops.
                terminal_recorded = True
                _finalize(run_id, {"result": shadow_audit.RESULT_ERROR,
                                   "reason_code": "FATAL_REPOSITORY_CONNECTION",
                                   "detail": None}, symbol=symbol, now=current)
                raise
            except Exception as exc:  # noqa: BLE001 -- audited, then reported as a blocked result
                outcome = {"result": shadow_audit.RESULT_ERROR, "reason_code": "UNEXPECTED"}
                results["blocked"].append((symbol, f"unexpected error: {exc}"))
            finally:
                if not terminal_recorded:
                    _finalize(run_id, outcome, symbol=symbol, now=current)
    finally:
        conn.close()

    return results
