"""Execution Engine -- the ONLY module in this codebase that is allowed
to call KISBroker.submit_order()/cancel_order() for a live order (spec
§7: "모든 실주문은 중앙 Execution Engine과 Order Gate를 통과해야 한다").
Orchestrates, in order:

    idempotency.register() (duplicate check, durable, before any network call)
    -> order_state_machine.transition(CREATED -> VALIDATING)
    -> execution.authorization.authorize_new_order() (HALT check + order_gate,
       mints a single-use AuthorizedExecution token -- CODEX-043)
    -> order_state_machine.transition(VALIDATING -> APPROVED -> SUBMITTING)
    -> KISBroker.submit_order(..., authorization=...) (the one real network call)
    -> order_state_machine.transition(SUBMITTING -> ACCEPTED/REJECTED/UNKNOWN)
       + idempotency.update_status()

Every status change goes through order_state_machine.transition() --
never a bare string write -- so an illegal jump (e.g. skipping straight
to ACCEPTED without ever being SUBMITTING) is a hard error here, not a
silent possibility.

Every fact `order_gate.py` needs (KIS price, KIS balance, KIS position,
open orders, reconciliation status) is fetched HERE via the injected
`KISBroker` + `reconciliation` callables, never inside order_gate.py
itself -- keeping order_gate.py's checks pure and this module the only
place I/O happens, matching this codebase's established Account/Risk/
Sizing/Execution Engine layering convention (live_readiness/
live_entry_pipeline.py is the direct precedent for this shape).

On ANY gate/idempotency/authorization failure, KISBroker.submit_order()
is never called -- zero broker calls. On a KISAmbiguousResponseError
from the broker call itself, the order lands in UNKNOWN and is
intentionally NOT retried by this function (spec §9) -- reconciliation/
order_reconciler.py is the only path that can move it out of UNKNOWN.
"""

from datetime import datetime, timezone

from brokers.kis_broker import KISAmbiguousResponseError, KISBrokerError
from execution import authorization, idempotency, order_gate
from execution.authorization import UnauthorizedExecutionError
from execution.order_state_machine import OrderStateTransitionError, transition


class ExecutionEngineError(Exception):
    """Raised whenever this engine blocks an order before/without
    reaching the broker (idempotency/gate/authorization failure).
    Callers must treat this as a hard block. Distinct from
    KISBrokerError/KISAmbiguousResponseError, which mean the broker call
    itself was attempted."""


class ExecutionResult:
    def __init__(self, *, internal_order_id, status, execution_record=None, blocked_reason=None):
        self.internal_order_id = internal_order_id
        self.status = status
        self.execution_record = execution_record
        self.blocked_reason = blocked_reason


def _reject(conn, internal_order_id, current_status):
    """Best-effort transition to REJECTED for governance/visibility --
    if the current status can't legally reach REJECTED (shouldn't
    happen given the states this is called from), the idempotency
    record is left at its last valid status rather than raising a
    second exception that would mask the original error."""
    try:
        new_status = transition(current_status, "REJECTED")
        idempotency.update_status(conn, internal_order_id, new_status)
    except OrderStateTransitionError:
        pass


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

        status = transition("CREATED", "VALIDATING")
        idempotency.update_status(conn, order_intent.internal_order_id, status)

        try:
            authorized = authorization.authorize_new_order(
                order_intent, buy_gate_context_builder, order_gate.evaluate_buy_gate, now=current,
            )
        except (order_gate.OrderGateBlockedError, UnauthorizedExecutionError) as exc:
            _reject(conn, order_intent.internal_order_id, status)
            raise ExecutionEngineError(f"buy order blocked by order gate: {exc}") from exc

        status = transition(status, "APPROVED")
        idempotency.update_status(conn, order_intent.internal_order_id, status)
        status = transition(status, "SUBMITTING")
        idempotency.update_status(conn, order_intent.internal_order_id, status)
        try:
            record = broker.submit_order(order_intent, instrument, authorization=authorized)
        except KISAmbiguousResponseError as exc:
            status = transition(status, "UNKNOWN")
            idempotency.update_status(conn, order_intent.internal_order_id, status)
            raise
        except KISBrokerError as exc:
            _reject(conn, order_intent.internal_order_id, status)
            raise

        status = transition(status, record.status)
        idempotency.update_status(conn, order_intent.internal_order_id, status, broker_order_id=record.broker_order_id)
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

        status = transition("CREATED", "VALIDATING")
        idempotency.update_status(conn, order_intent.internal_order_id, status)

        try:
            authorized = authorization.authorize_new_order(
                order_intent, sell_gate_context_builder, order_gate.evaluate_sell_gate, now=current,
            )
        except (order_gate.OrderGateBlockedError, UnauthorizedExecutionError) as exc:
            _reject(conn, order_intent.internal_order_id, status)
            raise ExecutionEngineError(f"sell order blocked by order gate: {exc}") from exc

        status = transition(status, "APPROVED")
        idempotency.update_status(conn, order_intent.internal_order_id, status)
        status = transition(status, "SUBMITTING")
        idempotency.update_status(conn, order_intent.internal_order_id, status)
        try:
            record = broker.submit_order(order_intent, instrument, authorization=authorized)
        except KISAmbiguousResponseError:
            status = transition(status, "UNKNOWN")
            idempotency.update_status(conn, order_intent.internal_order_id, status)
            raise
        except KISBrokerError:
            _reject(conn, order_intent.internal_order_id, status)
            raise

        status = transition(status, record.status)
        idempotency.update_status(conn, order_intent.internal_order_id, status, broker_order_id=record.broker_order_id)
        return ExecutionResult(
            internal_order_id=order_intent.internal_order_id, status=record.status, execution_record=record,
        )


def submit_cancel(*, order_intent, broker_order_id, cancel_gate_context_builder, conn, broker, instrument, now=None):
    """CODEX-043: cancels a durable, already-submitted order. Uses
    authorization.authorize_cancel() -- deliberately NOT blocked by HALT
    (an existing unfilled order may always be cancelled to reduce risk),
    but still requires order_gate.evaluate_cancel_gate() to pass (target
    order genuinely open, account/symbol match, no duplicate cancel).
    `order_intent` here is the ORIGINAL order's intent (same internal_
    order_id the idempotency ledger already tracks) -- this function
    transitions that SAME row to CANCEL_PENDING/CANCELLED/REJECTED, it
    does not register a new idempotency row (a cancel is not a new order
    attempt)."""
    current = now or datetime.now(timezone.utc)
    with idempotency.single_run_lock():
        try:
            authorized = authorization.authorize_cancel(
                order_intent, cancel_gate_context_builder, order_gate.evaluate_cancel_gate, now=current,
            )
        except (order_gate.OrderGateBlockedError, UnauthorizedExecutionError) as exc:
            raise ExecutionEngineError(f"cancel blocked by order gate: {exc}") from exc

        try:
            record = broker.cancel_order(order_intent, instrument, broker_order_id, authorization=authorized)
        except KISAmbiguousResponseError:
            idempotency.update_status(conn, order_intent.internal_order_id, "UNKNOWN")
            raise
        except KISBrokerError:
            idempotency.update_status(conn, order_intent.internal_order_id, "REJECTED")
            raise

        idempotency.update_status(conn, order_intent.internal_order_id, record.status, broker_order_id=broker_order_id)
        return ExecutionResult(
            internal_order_id=order_intent.internal_order_id, status=record.status, execution_record=record,
        )
