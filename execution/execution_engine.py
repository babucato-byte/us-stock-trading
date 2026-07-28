"""Execution Engine -- the ONLY module in this codebase that is allowed
to call KISBroker.submit_order()/cancel_order() for a live order (spec
§7: "모든 실주문은 중앙 Execution Engine과 Order Gate를 통과해야 한다").
Orchestrates, in order:

    idempotency.register() (duplicate check, durable, before any network call)
    -> order_gate.evaluate_buy_gate()/evaluate_sell_gate() (pure safety checks)
    -> KISBroker.submit_order() (the one real network call)
    -> order_state_machine transition + idempotency.update_status()

Every fact `order_gate.py` needs (KIS price, KIS balance, KIS position,
open orders, reconciliation status) is fetched HERE via the injected
`KISBroker` + `reconciliation` callables, never inside order_gate.py
itself -- keeping order_gate.py's checks pure and this module the only
place I/O happens, matching this codebase's established Account/Risk/
Sizing/Execution Engine layering convention (live_readiness/
live_entry_pipeline.py is the direct precedent for this shape).

On ANY gate/idempotency failure, KISBroker.submit_order() is never
called -- zero broker calls. On a KISAmbiguousResponseError from the
broker call itself, the order lands in UNKNOWN and is intentionally
NOT retried by this function (spec §9) -- reconciliation/
order_reconciler.py is the only path that can move it out of UNKNOWN.
"""

from datetime import datetime, timezone

from brokers.kis_broker import KISAmbiguousResponseError, KISBrokerError
from execution import idempotency, order_gate
from execution.order_state_machine import OrderStateTransitionError, transition


class ExecutionEngineError(Exception):
    """Raised whenever this engine blocks an order before/without
    reaching the broker (idempotency/gate failure). Callers must treat
    this as a hard block. Distinct from KISBrokerError/
    KISAmbiguousResponseError, which mean the broker call itself was
    attempted."""


class ExecutionResult:
    def __init__(self, *, internal_order_id, status, execution_record=None, blocked_reason=None):
        self.internal_order_id = internal_order_id
        self.status = status
        self.execution_record = execution_record
        self.blocked_reason = blocked_reason


def submit_buy_order(*, order_intent, buy_gate_context_builder, conn, broker, instrument, now=None):
    """`buy_gate_context_builder` is a zero-arg callable the caller
    supplies that returns a fully-populated `order_gate.BuyGateContext`
    -- this keeps this function broker/fact-source agnostic (tests can
    supply a fake builder) while still guaranteeing the gate is
    evaluated with FRESH facts gathered at call time, not stale ones
    captured earlier in a longer pipeline."""
    current = now or datetime.now(timezone.utc)
    trading_date = current.date().isoformat()
    with idempotency.single_run_lock():
        try:
            idempotency.register(
                conn, internal_order_id=order_intent.internal_order_id,
                signal_id=order_intent.signal_id, symbol=order_intent.symbol,
                side=order_intent.side, trading_date=trading_date,
            )
        except idempotency.DuplicateOrderAttemptError as exc:
            raise ExecutionEngineError(f"buy order blocked by idempotency check: {exc}") from exc

        try:
            ctx = buy_gate_context_builder()
            order_gate.evaluate_buy_gate(ctx)
        except order_gate.OrderGateBlockedError as exc:
            idempotency.update_status(conn, order_intent.internal_order_id, "REJECTED")
            raise ExecutionEngineError(f"buy order blocked by order gate: {exc}") from exc

        idempotency.update_status(conn, order_intent.internal_order_id, "SUBMITTING")
        try:
            record = broker.submit_order(order_intent, instrument)
        except KISAmbiguousResponseError as exc:
            idempotency.update_status(conn, order_intent.internal_order_id, "UNKNOWN")
            raise
        except KISBrokerError as exc:
            idempotency.update_status(conn, order_intent.internal_order_id, "REJECTED")
            raise

        idempotency.update_status(
            conn, order_intent.internal_order_id, record.status, broker_order_id=record.broker_order_id,
        )
        return ExecutionResult(
            internal_order_id=order_intent.internal_order_id, status=record.status, execution_record=record,
        )


def submit_sell_order(*, order_intent, sell_gate_context_builder, conn, broker, instrument, now=None):
    current = now or datetime.now(timezone.utc)
    trading_date = current.date().isoformat()
    with idempotency.single_run_lock():
        try:
            idempotency.register(
                conn, internal_order_id=order_intent.internal_order_id,
                signal_id=order_intent.signal_id, symbol=order_intent.symbol,
                side=order_intent.side, trading_date=trading_date,
            )
        except idempotency.DuplicateOrderAttemptError as exc:
            raise ExecutionEngineError(f"sell order blocked by idempotency check: {exc}") from exc

        try:
            ctx = sell_gate_context_builder()
            order_gate.evaluate_sell_gate(ctx)
        except order_gate.OrderGateBlockedError as exc:
            idempotency.update_status(conn, order_intent.internal_order_id, "REJECTED")
            raise ExecutionEngineError(f"sell order blocked by order gate: {exc}") from exc

        idempotency.update_status(conn, order_intent.internal_order_id, "SUBMITTING")
        try:
            record = broker.submit_order(order_intent, instrument)
        except KISAmbiguousResponseError:
            idempotency.update_status(conn, order_intent.internal_order_id, "UNKNOWN")
            raise
        except KISBrokerError:
            idempotency.update_status(conn, order_intent.internal_order_id, "REJECTED")
            raise

        idempotency.update_status(
            conn, order_intent.internal_order_id, record.status, broker_order_id=record.broker_order_id,
        )
        return ExecutionResult(
            internal_order_id=order_intent.internal_order_id, status=record.status, execution_record=record,
        )
