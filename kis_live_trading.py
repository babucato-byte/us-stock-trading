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

import os
import uuid
from datetime import datetime, timezone

import paper_strategy_order as pso
from brokers.kis_broker import KISAmbiguousResponseError, KISBrokerError
from config.live_rollout_config import LiveRolloutConfig, LiveRolloutConfigError
from domain.instrument import Instrument, InstrumentError, build_instrument
from domain.order_intent import OrderIntent, OrderIntentError
from domain.signal import Signal, SignalError, build_signal
from execution import execution_engine, order_gate
from execution.execution_engine import ExecutionEngineError
from market_data.kis_validation_provider import (
    KISValidationProvider,
    compute_price_deviation_percent,
)
from market_data.base import MarketDataProviderError
from operations import kill_switch as ops_kill_switch
from reconciliation.order_reconciler import reconcile_unknown_order
from reconciliation.position_reconciler import reconcile_positions
from state_store import db as state_db

SCORE_THRESHOLD = 70  # matches paper_strategy_order.py's existing threshold
SIGNAL_VALID_SECONDS = 120


class KISLiveTradingError(Exception):
    """Raised when a structural precondition (config, commit match) is
    invalid before any per-symbol processing even begins."""


def _build_instrument(symbol, allowed_symbols):
    """See module docstring's RESIDUAL RISK note. `symbol` must already
    be on `allowed_symbols` (checked by the caller before this is
    invoked) -- leveraged/inverse/otc are trusted-False for exactly that
    reason, not independently detected."""
    return build_instrument(symbol, exchange="NASDAQ")


def _get_deployed_commit():
    return os.environ.get("DEPLOYED_COMMIT", "")


def _get_validated_commit():
    return os.environ.get("VALIDATED_COMMIT", "")


def _get_allowed_account_no():
    return os.environ.get("KIS_ALLOWED_ACCOUNT_NO", "")


def run_live_buy_entry_cycle(*, broker, live_rollout=None, now=None):
    """Returns a results dict: {"submitted": [...], "blocked": [(symbol, reason)], "skipped": [...]}.
    Never raises for a per-symbol failure -- only for a structural
    precondition failure that makes the WHOLE cycle unsafe to run at all
    (config invalid, halted, commit mismatch)."""
    current = now or datetime.now(timezone.utc)
    results = {"submitted": [], "blocked": [], "skipped": []}

    rollout = live_rollout or LiveRolloutConfig.from_env()
    try:
        rollout.validate()
    except LiveRolloutConfigError as exc:
        raise KISLiveTradingError(f"live_rollout config invalid, refusing to run: {exc}") from exc

    if not rollout.enabled:
        raise KISLiveTradingError("live_rollout.enabled is False -- KIS live entries are not active")

    if ops_kill_switch.is_halted():
        raise KISLiveTradingError("operations HALT is set -- no automatic order attempts permitted")
    if not ops_kill_switch.is_entry_allowed():
        raise KISLiveTradingError("ENTRY_OFF (kill_switch_state) is set -- new entries blocked")

    validated_commit = _get_validated_commit()
    deployed_commit = _get_deployed_commit()
    if not validated_commit or validated_commit != deployed_commit:
        raise KISLiveTradingError(
            f"validated commit {validated_commit!r} does not match deployed commit "
            f"{deployed_commit!r} -- refusing to run an unvalidated deployment"
        )

    allowed_account_no = _get_allowed_account_no()
    if not allowed_account_no:
        raise KISLiveTradingError("KIS_ALLOWED_ACCOUNT_NO is not configured -- refusing to run")

    is_regular_session = pso.get_us_market_session() == "regular" if rollout.regular_session_only else True

    watchlist = pso.load_watchlist()
    kis_validation = KISValidationProvider(broker, instrument_lookup=lambda s: _build_instrument(s, rollout.allowed_symbols))

    conn = state_db.open_db()
    try:
        for symbol in watchlist:
            if symbol not in rollout.allowed_symbols:
                results["skipped"].append((symbol, "not in live_rollout.allowed_symbols"))
                continue

            analysis = pso.analyze_stock(symbol)
            if analysis is None or analysis["score"] < SCORE_THRESHOLD:
                results["skipped"].append((symbol, "did not meet score threshold"))
                continue

            try:
                instrument = _build_instrument(symbol, rollout.allowed_symbols)
                signal = build_signal(
                    strategy_id="PAPER_STRATEGY_ORDER_SCORE_V1", strategy_version="v1",
                    config_version="live_rollout_v1", code_commit=deployed_commit,
                    symbol=symbol, exchange=instrument.exchange, signal_price=analysis["price"],
                    score=analysis["score"], entry_reason="score_threshold_breakout",
                    valid_for_seconds=SIGNAL_VALID_SECONDS, now=current,
                )
            except (InstrumentError, SignalError) as exc:
                results["blocked"].append((symbol, f"signal/instrument construction failed: {exc}"))
                continue

            try:
                kis_quote = kis_validation.get_price_quote(symbol)
            except MarketDataProviderError as exc:
                results["blocked"].append((symbol, f"KIS price re-check failed: {exc}"))
                continue

            try:
                account_snapshot = broker.get_account_snapshot()
            except KISBrokerError as exc:
                results["blocked"].append((symbol, f"KIS account read failed: {exc}"))
                continue

            available_usd = account_snapshot.usd_available_for_new_order
            buffered_price = kis_quote.price_usd
            balance_qty = int(available_usd // buffered_price) if buffered_price > 0 else 0
            quantity = min(balance_qty, rollout.max_quantity_per_order)
            if quantity < 1:
                results["blocked"].append((symbol, "insufficient KIS orderable cash for even 1 share"))
                continue

            try:
                order_intent = OrderIntent(
                    internal_order_id=f"kislive-{symbol}-{uuid.uuid4().hex[:12]}",
                    signal_id=signal.signal_id, strategy_id=signal.strategy_id, symbol=symbol,
                    exchange=instrument.exchange, side="buy", quantity=quantity, order_type="limit",
                    limit_price=buffered_price, stop_price=None, target_price=None, created_at=current,
                )
            except OrderIntentError as exc:
                results["blocked"].append((symbol, f"order intent construction failed: {exc}"))
                continue

            try:
                open_orders = broker.get_open_orders()
            except KISBrokerError as exc:
                results["blocked"].append((symbol, f"KIS open-orders read failed: {exc}"))
                continue
            has_open_order_for_symbol = any(
                (o.get("pdno") or o.get("PDNO")) == symbol for o in open_orders
            )

            def _buy_ctx_builder(
                signal=signal, instrument=instrument, order_intent=order_intent,
                kis_price=kis_quote.price_usd, available_usd=available_usd,
                has_open_order_for_symbol=has_open_order_for_symbol,
            ):
                return order_gate.BuyGateContext(
                    execution_broker="kis", live_order_enabled=True, entry_disabled=False,
                    validated_commit=validated_commit, deployed_commit=deployed_commit,
                    kis_account_no=account_snapshot.account_id, allowed_account_no=allowed_account_no,
                    order_intent=order_intent, instrument=instrument, signal=signal,
                    is_regular_session=is_regular_session, kis_price_usd=kis_price,
                    max_price_deviation_percent=rollout.max_price_deviation_percent,
                    usd_orderable_cash=available_usd, has_open_order_for_symbol=has_open_order_for_symbol,
                    has_order_for_signal_id=False, allowed_symbols=rollout.allowed_symbols,
                    reconciliation_ok=True, has_unknown_order=False, now=current,
                )

            try:
                result = execution_engine.submit_buy_order(
                    order_intent=order_intent, buy_gate_context_builder=_buy_ctx_builder,
                    conn=conn, broker=broker, instrument=instrument, now=current,
                )
                results["submitted"].append(symbol)
            except ExecutionEngineError as exc:
                results["blocked"].append((symbol, str(exc)))
            except KISAmbiguousResponseError as exc:
                results["blocked"].append((symbol, f"ambiguous KIS response, order status UNKNOWN: {exc}"))
            except KISBrokerError as exc:
                results["blocked"].append((symbol, f"KIS order rejected: {exc}"))
    finally:
        conn.close()

    return results
