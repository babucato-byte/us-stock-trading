"""Execution Engine -- the ONLY module in this codebase that is allowed
to call KISBroker.submit_order()/cancel_order() for a live order (spec
§7: "모든 실주문은 중앙 Execution Engine과 Order Gate를 통과해야 한다").
Orchestrates, in order:

    idempotency.register() (duplicate check, durable, before any network call)
    -> CAS CREATED -> VALIDATING
    -> reconciliation.snapshot.build_snapshot() + verify_snapshot()
       (real KIS positions/open-orders/fills vs internal state -- CODEX-044)
    -> execution.authorization.authorize_new_order() (HALT check + order_gate,
       mints a single-use AuthorizedExecution token -- CODEX-043)
    -> CAS VALIDATING -> APPROVED -> SUBMITTING
    -> KISBroker.submit_order(..., authorization=...) (the one real network call)
    -> CAS SUBMITTING -> ACCEPTED/REJECTED/UNKNOWN

Every status change goes through execution/order_repository.py's
compare-and-set (CODEX-047) -- never a bare string write, never an
UPDATE without an expected state AND version, and always with its
order_state_events row written in the same transaction. An illegal jump
(e.g. skipping straight to ACCEPTED without ever being SUBMITTING) and a
concurrent writer's clobber are both hard errors here, not silent
possibilities.

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
from execution import authorization, idempotency, order_gate, order_repository
from execution.authorization import UnauthorizedExecutionError
from execution.order_repository import OrderRepositoryError
from execution.order_state_machine import OrderStateTransitionError
from reconciliation import snapshot as reconciliation_snapshot
from reconciliation.snapshot import (
    ReconciliationBlockedError,
    ReconciliationUnavailableError,
)


class ExecutionEngineError(Exception):
    """Raised whenever this engine blocks an order before/without
    reaching the broker (idempotency/gate/authorization failure).
    Callers must treat this as a hard block. Distinct from
    KISBrokerError/KISAmbiguousResponseError, which mean the broker call
    itself was attempted.

    `reason_code` (CODEX-048) is a stable, machine-readable category the
    Shadow audit trail maps to an event type -- so the audit log records
    "this was a reconciliation block" rather than requiring a caller to
    pattern-match on English exception text."""

    def __init__(self, message, *, reason_code=None):
        super().__init__(message)
        self.reason_code = reason_code


REASON_DUPLICATE = "DUPLICATE"
REASON_RECONCILIATION_UNAVAILABLE = "RECONCILIATION_UNAVAILABLE"
REASON_RECONCILIATION_DIRTY = "RECONCILIATION_DIRTY"
REASON_UNKNOWN_ORDER = "UNKNOWN_ORDER"
REASON_HALT = "HALT"
REASON_GATE = "GATE"
REASON_STATE_PERSISTENCE = "STATE_PERSISTENCE"


class ExecutionResult:
    def __init__(self, *, internal_order_id, status, execution_record=None, blocked_reason=None):
        self.internal_order_id = internal_order_id
        self.status = status
        self.execution_record = execution_record
        self.blocked_reason = blocked_reason


def _reject(conn, record, *, event_type, reason):
    """Best-effort compare-and-set to REJECTED for governance/visibility
    -- if the current state can't legally reach REJECTED, or another
    writer moved the order first (CAS conflict), the durable record is
    left at whatever the other writer established rather than raising a
    second exception that would mask the original error. Returns the
    resulting record (unchanged on failure)."""
    try:
        return order_repository.advance(
            conn, record, "REJECTED", event_type=event_type, event_payload={"reason": reason},
        )
    except (OrderStateTransitionError, OrderRepositoryError):
        return record


def _reconcile_now(*, conn, broker, order_intent, account_id, current, side_label):
    """CODEX-044: the engine collects the REAL state itself, immediately
    before the gate, and judges it -- no caller may hand it a
    `reconciliation_ok` boolean. A failed KIS read, a stale snapshot, a
    position/open-order/fill disagreement, or ANY order still in UNKNOWN
    all raise here, i.e. before APPROVED, before SUBMITTING, and
    therefore with exactly zero transport calls."""
    try:
        snapshot = reconciliation_snapshot.build_snapshot(
            broker=broker, conn=conn, account_id=account_id,
            symbol=order_intent.symbol, now=current,
        )
    except ReconciliationUnavailableError as exc:
        raise ExecutionEngineError(
            f"{side_label} order blocked -- reconciliation could not be performed: {exc}",
            reason_code=REASON_RECONCILIATION_UNAVAILABLE,
        ) from exc
    try:
        reconciliation_snapshot.verify_snapshot(
            snapshot, account_id=account_id, symbol=order_intent.symbol, now=current,
        )
    except ReconciliationBlockedError as exc:
        code = (REASON_UNKNOWN_ORDER if snapshot.has_unknown_orders
                else REASON_RECONCILIATION_DIRTY)
        raise ExecutionEngineError(
            f"{side_label} order blocked by reconciliation: {exc}", reason_code=code,
        ) from exc
    return snapshot


def submit_buy_order(*, order_intent, buy_gate_context_builder, conn, broker, instrument,
                     account_id, now=None):
    """`buy_gate_context_builder` is a ONE-ARG callable the caller
    supplies that takes the `ReconciliationSnapshot` this engine just
    built and returns a fully-populated `order_gate.BuyGateContext` --
    this keeps this function broker/fact-source agnostic (tests can
    supply a fake builder) while still guaranteeing the gate is
    evaluated with FRESH facts gathered at call time, not stale ones
    captured earlier in a longer pipeline, and that the reconciliation
    half of those facts came from this engine's own KIS reads rather
    than from the caller (CODEX-044)."""
    return _submit_new_order(
        order_intent=order_intent, gate_context_builder=buy_gate_context_builder,
        gate_fn=order_gate.evaluate_buy_gate, conn=conn, broker=broker, instrument=instrument,
        account_id=account_id, now=now, side_label="buy",
    )


def submit_sell_order(*, order_intent, sell_gate_context_builder, conn, broker, instrument,
                      account_id, now=None):
    """CODEX-044: identical reconciliation policy to the buy path --
    same snapshot, same TTL, same account/symbol binding, same
    fail-closed outcomes. `sell_gate_context_builder` takes the
    snapshot, exactly like the buy builder does."""
    return _submit_new_order(
        order_intent=order_intent, gate_context_builder=sell_gate_context_builder,
        gate_fn=order_gate.evaluate_sell_gate, conn=conn, broker=broker, instrument=instrument,
        account_id=account_id, now=now, side_label="sell",
    )


def _submit_new_order(*, order_intent, gate_context_builder, gate_fn, conn, broker, instrument,
                       account_id, now, side_label):
    """The single new-order flow both submit_buy_order() and
    submit_sell_order() run -- buy and sell differ ONLY in which gate
    function evaluates the context, never in which safety steps run or
    in what order (CODEX-044's "매수와 매도 모두 동일 정책 적용").

    The mandatory order, with every state change a compare-and-set
    (CODEX-047):

        register (CREATED, version 0)
        -> CAS CREATED@0    -> VALIDATING
        -> real-time reconciliation snapshot + verification
        -> HALT check + Order Gate (mints the single-use authorization)
        -> CAS VALIDATING   -> APPROVED
        -> CAS APPROVED     -> SUBMITTING
        -> the ONE transport call
        -> CAS SUBMITTING   -> ACCEPTED / REJECTED / UNKNOWN

    Any failure before SUBMITTING means zero transport calls. A CAS
    conflict at any point aborts without ever calling the broker again.
    """
    current = now or datetime.now(timezone.utc)
    trading_date = current.date().isoformat()
    with idempotency.single_run_lock():
        try:
            idempotency.register(
                conn, internal_order_id=order_intent.internal_order_id,
                signal_id=order_intent.signal_id, symbol=order_intent.symbol,
                side=order_intent.side, trading_date=trading_date,
                requested_quantity=order_intent.quantity,
            )
        except idempotency.DuplicateOrderAttemptError as exc:
            raise ExecutionEngineError(
                f"{side_label} order blocked by idempotency check: {exc}",
                reason_code=REASON_DUPLICATE,
            ) from exc

        record = order_repository.load(conn, order_intent.internal_order_id)
        try:
            record = order_repository.advance(
                conn, record, "VALIDATING", event_type="VALIDATION_STARTED", now=current,
            )
        except OrderRepositoryError as exc:
            raise ExecutionEngineError(
                f"{side_label} order blocked -- could not durably record VALIDATING: {exc}",
                reason_code=REASON_STATE_PERSISTENCE,
            ) from exc

        try:
            snapshot = _reconcile_now(
                conn=conn, broker=broker, order_intent=order_intent, account_id=account_id,
                current=current, side_label=side_label,
            )
        except ExecutionEngineError as exc:
            _reject(conn, record, event_type="RECONCILIATION_BLOCKED", reason=str(exc))
            raise

        try:
            authorized = authorization.authorize_new_order(
                order_intent, lambda: gate_context_builder(snapshot), gate_fn, now=current,
            )
        except (order_gate.OrderGateBlockedError, UnauthorizedExecutionError) as exc:
            _reject(conn, record, event_type="GATE_REJECTED", reason=str(exc))
            code = (REASON_HALT if isinstance(exc, UnauthorizedExecutionError)
                    else f"{REASON_GATE}:{getattr(exc, 'code', 'GATE')}")
            raise ExecutionEngineError(
                f"{side_label} order blocked by order gate: {exc}", reason_code=code,
            ) from exc

        try:
            record = order_repository.advance(
                conn, record, "APPROVED", event_type="GATE_APPROVED", now=current,
            )
            record = order_repository.advance(
                conn, record, "SUBMITTING", event_type="TRANSPORT_SUBMITTING", now=current,
            )
        except OrderRepositoryError as exc:
            # The durable state could not be advanced, so the broker is
            # NOT called: an order whose SUBMITTING was never persisted
            # would be invisible to restart recovery.
            raise ExecutionEngineError(
                f"{side_label} order blocked -- could not durably record SUBMITTING: {exc}",
                reason_code=REASON_STATE_PERSISTENCE,
            ) from exc

        try:
            execution_record = broker.submit_order(order_intent, instrument, authorization=authorized)
        except KISAmbiguousResponseError as exc:
            # spec §9: the response was lost. The order is UNKNOWN and is
            # never automatically re-submitted -- only reconciliation
            # against KIS's own history may move it out of UNKNOWN.
            _force_unknown(conn, record, reason=str(exc), now=current)
            raise
        except KISBrokerError as exc:
            _reject(conn, record, event_type="TRANSPORT_REJECTED", reason=str(exc))
            raise

        try:
            record = order_repository.advance(
                conn, record, execution_record.status, event_type="TRANSPORT_RESULT",
                event_payload={"broker_order_id": execution_record.broker_order_id},
                broker_order_id=execution_record.broker_order_id, now=current,
            )
        except OrderRepositoryError as exc:
            # The order IS live at KIS but we could not record its
            # outcome -- exactly the "we no longer know the true state"
            # case UNKNOWN exists for. Never re-submit.
            _force_unknown(
                conn, record,
                reason=f"could not durably record {execution_record.status}: {exc}", now=current,
                broker_order_id=execution_record.broker_order_id,
            )
            raise ExecutionEngineError(
                f"{side_label} order reached KIS but its outcome could not be durably recorded "
                f"-- left UNKNOWN for reconciliation: {exc}",
                reason_code=REASON_STATE_PERSISTENCE,
            ) from exc

        return ExecutionResult(
            internal_order_id=order_intent.internal_order_id, status=execution_record.status,
            execution_record=execution_record,
        )


def _force_unknown(conn, record, *, reason, now, broker_order_id=None):
    """Persist UNKNOWN for an order whose true state we can no longer
    determine. A CAS conflict here means another writer already moved
    the order (e.g. reconciliation resolved it); that writer's result
    stands and is not overwritten."""
    try:
        return order_repository.advance(
            conn, record, "UNKNOWN", event_type="RESPONSE_LOST",
            event_payload={"reason": reason}, broker_order_id=broker_order_id, now=now,
        )
    except (OrderStateTransitionError, OrderRepositoryError):
        return record


def submit_cancel(*, order_intent, broker_order_id, cancel_gate_context_builder, conn, broker, instrument, now=None):
    """CODEX-043: cancels a durable, already-submitted order. Uses
    authorization.authorize_cancel() -- deliberately NOT blocked by HALT
    (an existing unfilled order may always be cancelled to reduce risk),
    but still requires order_gate.evaluate_cancel_gate() to pass (target
    order genuinely open, account/symbol match, no duplicate cancel).
    `order_intent` here is the ORIGINAL order's intent (same internal_
    order_id the idempotency ledger already tracks) -- this function
    transitions that SAME row to CANCEL_PENDING/CANCELLED/UNKNOWN, it
    does not register a new idempotency row (a cancel is not a new order
    attempt).

    CODEX-047: the cancel path is now held to the same state machine as
    the order path. It previously skipped CANCEL_PENDING entirely,
    called the broker first, and then wrote CANCELLED/REJECTED/UNKNOWN
    directly. Now:

      - the order must EXIST durably (no record -> no cancel, zero
        transport calls);
      - CAS -> CANCEL_PENDING happens BEFORE the transport call, so a
        crash mid-cancel is recoverable;
      - a lost response is UNKNOWN;
      - a REJECTED cancel is also UNKNOWN, not "REJECTED": KIS refusing
        the cancel says nothing definite about the underlying order's
        state (it may have filled a moment earlier), and CANCEL_PENDING
        -> REJECTED is not a legal transition in the first place.
        Reconciliation against KIS's own history is what resolves it.
    """
    current = now or datetime.now(timezone.utc)
    with idempotency.single_run_lock():
        record = order_repository.load(conn, order_intent.internal_order_id)
        if record is None:
            raise ExecutionEngineError(
                f"cancel blocked -- no durable order record exists for "
                f"{order_intent.internal_order_id!r}"
            )

        try:
            authorized = authorization.authorize_cancel(
                order_intent, cancel_gate_context_builder, order_gate.evaluate_cancel_gate, now=current,
            )
        except (order_gate.OrderGateBlockedError, UnauthorizedExecutionError) as exc:
            raise ExecutionEngineError(f"cancel blocked by order gate: {exc}") from exc

        try:
            record = order_repository.advance(
                conn, record, "CANCEL_PENDING", event_type="CANCEL_REQUESTED",
                event_payload={"broker_order_id": broker_order_id},
                broker_order_id=broker_order_id, now=current,
            )
        except (OrderStateTransitionError, OrderRepositoryError) as exc:
            raise ExecutionEngineError(
                f"cancel blocked -- could not durably record CANCEL_PENDING for "
                f"{order_intent.internal_order_id!r}: {exc}"
            ) from exc

        try:
            execution_record = broker.cancel_order(
                order_intent, instrument, broker_order_id, authorization=authorized,
            )
        except KISAmbiguousResponseError as exc:
            _force_unknown(conn, record, reason=str(exc), now=current)
            raise
        except KISBrokerError as exc:
            _force_unknown(conn, record, reason=f"cancel failed: {exc}", now=current)
            raise

        if execution_record.status != "CANCELLED":
            _force_unknown(
                conn, record,
                reason=f"KIS did not confirm the cancel (status={execution_record.status!r})",
                now=current,
            )
            return ExecutionResult(
                internal_order_id=order_intent.internal_order_id, status="UNKNOWN",
                execution_record=execution_record,
                blocked_reason=f"cancel not confirmed by KIS: {execution_record.status!r}",
            )

        try:
            record = order_repository.advance(
                conn, record, "CANCELLED", event_type="CANCEL_CONFIRMED",
                event_payload={"broker_order_id": broker_order_id}, now=current,
            )
        except (OrderStateTransitionError, OrderRepositoryError) as exc:
            _force_unknown(
                conn, record, reason=f"could not durably record CANCELLED: {exc}", now=current,
            )
            raise ExecutionEngineError(
                f"cancel was confirmed by KIS but could not be durably recorded -- left UNKNOWN "
                f"for reconciliation: {exc}"
            ) from exc

        return ExecutionResult(
            internal_order_id=order_intent.internal_order_id, status=execution_record.status,
            execution_record=execution_record,
        )
