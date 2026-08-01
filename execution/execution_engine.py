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
    -> CAS VALIDATING -> APPROVED
    -> GATE_APPROVED shadow-audit event      (CODEX-048: BEFORE transport)
    -> CAS APPROVED -> SUBMITTING
    -> EXECUTION_PLANNED shadow-audit event  (CODEX-048: BEFORE transport)
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

import logging
import sqlite3
from datetime import datetime, timezone

from brokers.kis_broker import KISAmbiguousResponseError, KISBrokerError
from execution import authorization, idempotency, order_gate, order_repository
from execution.authorization import UnauthorizedExecutionError
from execution.order_repository import (
    FatalRepositoryConnectionError,
    OrderRepositoryError,
    OrderRepositoryReadError,
    OrderRepositoryRollbackError,
)
from execution.order_state_machine import OrderStateTransitionError
import shadow_audit
from reconciliation import snapshot as reconciliation_snapshot
from execution.secret_redaction import safe_repr
from reconciliation.snapshot import (
    ReconciliationBlockedError,
    ReconciliationUnavailableError,
)

logger = logging.getLogger(__name__)


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
REASON_AUDIT_PERSISTENCE = "AUDIT_PERSISTENCE"
REASON_AUDIT_CONTEXT_MISSING = "AUDIT_CONTEXT_MISSING"
REASON_CANCEL_FINAL_STATE_PERSISTENCE = "CANCEL_FINAL_STATE_PERSISTENCE"
REASON_STATE_READ_FAILURE = "STATE_READ_FAILURE"
REASON_FATAL_REPOSITORY_CONNECTION = "FATAL_REPOSITORY_CONNECTION"


class CancelPreTransportBlocked(ExecutionEngineError):
    """A cancel refused BEFORE broker.cancel_order() was called. The
    execution never reached KIS, so the run ends as SHADOW_BLOCKED."""


class CancelPostTransportError(ExecutionEngineError):
    """A cancel that DID reach broker.cancel_order() and then failed --
    typically because the confirmed outcome could not be persisted.

    CODEX-054: this is the distinction that was missing. Classifying it
    as "blocked" would tell an operator the cancel never ran, while the
    order may in fact be cancelled at KIS. It is an execution error."""


def validate_audit_run_id(value):
    """CODEX-053: every execution path must carry the Shadow audit run it
    belongs to. There is no default and no fallback.

    An engine that quietly generated its own id when the caller forgot
    one would be worse than useless: the approval events would exist but
    under an id nothing else references, so the run they belong to would
    still look unaudited. And skipping the audit when the id is absent
    would be a fail-OPEN on the one guarantee CODEX-048 established.
    Both are refused here, before any state transition or network call.

    Returns the normalized id; raises with reason_code
    AUDIT_CONTEXT_MISSING for None, a non-string, an empty string, or a
    whitespace-only string."""
    if not isinstance(value, str):
        raise ExecutionEngineError(
            f"audit_run_id is required and must be a string, got {type(value).__name__}",
            reason_code=REASON_AUDIT_CONTEXT_MISSING,
        )
    normalized = value.strip()
    if not normalized:
        raise ExecutionEngineError(
            "audit_run_id is required and must not be empty or whitespace",
            reason_code=REASON_AUDIT_CONTEXT_MISSING,
        )
    return normalized


class ExecutionResult:
    def __init__(self, *, internal_order_id, status, execution_record=None, blocked_reason=None):
        self.internal_order_id = internal_order_id
        self.status = status
        self.execution_record = execution_record
        self.blocked_reason = blocked_reason


def normalize_persistence_error(exc, *, stage, post_transport, connection_invalidated=False):
    """CODEX-055: one classification for every durable-state failure.

    The engine must not care whether a write failed with an
    OperationalError, an IntegrityError, a DatabaseError or a repository
    error -- they all mean the same operationally: this order's durable
    state is not what we believe it to be, so no success may be returned
    and a human has to reconcile it. Letting each SQLite class surface
    raw meant the UNKNOWN fallback skipped normalization, the operator
    alert and the terminal-audit handling entirely.

    The message carries no SQL, no bound parameters, no broker payload
    and no account identifier; the original exception is chained."""
    detail = "; the database connection was invalidated" if connection_invalidated else ""
    message = (
        f"durable order state could not be confirmed during {stage}{detail} -- "
        "manual reconciliation required"
    )
    error = ExecutionEngineError(message, reason_code=REASON_STATE_PERSISTENCE)
    error.post_transport = post_transport
    error.connection_invalidated = connection_invalidated
    error.__cause__ = exc
    return error


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
    except FatalRepositoryConnectionError:
        raise  # CODEX-059: never swallowed by a best-effort write
    except Exception:  # noqa: BLE001 -- best-effort governance write (CODEX-055)
        logger.exception("could not record REJECTED for %s", record.internal_order_id)
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


def _audit_before_transport(*, audit_run_id, event_type, order_intent, side_label, now,
                             reason_code=None, detail=None):
    """CODEX-048: records an approval/execution-planned audit event BEFORE
    the transport call, and BLOCKS the order if it cannot be persisted.

    The ordering is the point. If this is recorded after
    `broker.submit_order()` returns, a crash during the broker call
    leaves an order that may well have reached KIS with no durable record
    of the approval that authorized it -- which is exactly the state the
    audit trail exists to make impossible. Failing closed here costs a
    missed order; failing open costs an unaudited real order.

    `audit_run_id` is already validated by validate_audit_run_id() at the
    top of the flow, so there is deliberately no "if not set, skip"
    branch here -- that branch was CODEX-053."""
    try:
        shadow_audit.record_event(
            shadow_run_id=audit_run_id, event_type=event_type,
            result=shadow_audit.RESULT_APPROVED, symbol=order_intent.symbol,
            side=order_intent.side, signal_id=order_intent.signal_id,
            internal_order_id=order_intent.internal_order_id, reason_code=reason_code,
            payload={"detail": detail} if detail else None, now=now,
        )
    except shadow_audit.ShadowAuditError as exc:
        raise ExecutionEngineError(
            f"{side_label} order blocked -- {event_type} audit event could not be persisted "
            f"before the transport call: {exc}",
            reason_code=REASON_AUDIT_PERSISTENCE,
        ) from exc


def submit_buy_order(*, order_intent, buy_gate_context_builder, conn, broker, instrument,
                     account_id, audit_run_id, now=None):
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
        account_id=account_id, now=now, side_label="buy", audit_run_id=audit_run_id,
    )


def submit_sell_order(*, order_intent, sell_gate_context_builder, conn, broker, instrument,
                      account_id, audit_run_id, now=None):
    """CODEX-044: identical reconciliation policy to the buy path --
    same snapshot, same TTL, same account/symbol binding, same
    fail-closed outcomes. `sell_gate_context_builder` takes the
    snapshot, exactly like the buy builder does."""
    return _submit_new_order(
        order_intent=order_intent, gate_context_builder=sell_gate_context_builder,
        gate_fn=order_gate.evaluate_sell_gate, conn=conn, broker=broker, instrument=instrument,
        account_id=account_id, now=now, side_label="sell", audit_run_id=audit_run_id,
    )


def _submit_new_order(*, order_intent, gate_context_builder, gate_fn, conn, broker, instrument,
                       account_id, now, side_label, audit_run_id):
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
        -> GATE_APPROVED audit event        (CODEX-048, before transport)
        -> CAS APPROVED     -> SUBMITTING
        -> EXECUTION_PLANNED audit event    (CODEX-048, before transport)
        -> the ONE transport call
        -> CAS SUBMITTING   -> ACCEPTED / REJECTED / UNKNOWN

    Any failure before SUBMITTING means zero transport calls. A CAS
    conflict at any point aborts without ever calling the broker again.
    """
    # CODEX-053: before the idempotency row, before any state transition,
    # before the gate, before the network. No audit context, no order.
    audit_run_id = validate_audit_run_id(audit_run_id)
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
        except FatalRepositoryConnectionError:
            raise  # CODEX-059: fatal outranks the ordinary persistence path
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
        except FatalRepositoryConnectionError:
            raise  # CODEX-059
        except OrderRepositoryError as exc:
            raise ExecutionEngineError(
                f"{side_label} order blocked -- could not durably record APPROVED: {exc}",
                reason_code=REASON_STATE_PERSISTENCE,
            ) from exc

        _audit_before_transport(
            audit_run_id=audit_run_id, event_type=shadow_audit.GATE_APPROVED,
            order_intent=order_intent, side_label=side_label, now=current,
            reason_code="APPROVED",
        )

        try:
            record = order_repository.advance(
                conn, record, "SUBMITTING", event_type="TRANSPORT_SUBMITTING", now=current,
            )
        except FatalRepositoryConnectionError:
            raise  # CODEX-059
        except OrderRepositoryError as exc:
            # The durable state could not be advanced, so the broker is
            # NOT called: an order whose SUBMITTING was never persisted
            # would be invisible to restart recovery.
            raise ExecutionEngineError(
                f"{side_label} order blocked -- could not durably record SUBMITTING: {exc}",
                reason_code=REASON_STATE_PERSISTENCE,
            ) from exc

        # The LAST thing before the network call: "we are about to submit
        # this exact order". A crash after this point leaves an audit
        # trail that says so.
        _audit_before_transport(
            audit_run_id=audit_run_id, event_type=shadow_audit.EXECUTION_PLANNED,
            order_intent=order_intent, side_label=side_label, now=current,
            reason_code="SUBMITTING",
            detail=f"quantity={order_intent.quantity} limit={order_intent.limit_price}",
        )

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
        except FatalRepositoryConnectionError:
            raise  # CODEX-059: no UNKNOWN fallback on a poisoned connection
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
    updated, _persisted, _error = _force_unknown_reported(
        conn, record, reason=reason, now=now, broker_order_id=broker_order_id,
    )
    return updated


def _force_unknown_reported(conn, record, *, reason, now, broker_order_id=None):
    """_force_unknown() that also reports whether UNKNOWN was actually
    persisted, and why not.

    Returns `(record, persisted, error)`. The caller needs the
    distinction: "the order is durably UNKNOWN, reconciliation will
    resolve it" and "we could not even record that we do not know" are
    very different operational situations.

    CODEX-055: catches EVERYTHING. A raw sqlite3.Error escaping from here
    skipped the caller's normalization, its operator alert and its
    terminal-audit handling -- the exact defect this replaces."""
    try:
        updated = order_repository.advance(
            conn, record, "UNKNOWN", event_type="RESPONSE_LOST",
            event_payload={"reason": reason}, broker_order_id=broker_order_id, now=now,
        )
        return updated, True, None
    except FatalRepositoryConnectionError:
        raise  # CODEX-059: the caller must see the fatal type, not a report
    except Exception as exc:  # noqa: BLE001 -- normalized by the caller
        logger.error(
            "could not persist UNKNOWN for order %s: %s",
            record.internal_order_id, type(exc).__name__,
        )
        return record, False, exc


def submit_cancel(*, order_intent, broker_order_id, cancel_gate_context_builder, conn, broker,
                   instrument, audit_run_id, now=None):
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

    CODEX-053: `audit_run_id` is required here too. A cancel reaches the
    KIS transport, so it is held to the same "approval is audited before
    the network call" rule as a new order.

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

    CODEX-053 (terminal audit): a cancel is ONE audit run, and it always
    ends. Success -> SHADOW_COMPLETED, any pre-transport refusal ->
    SHADOW_BLOCKED, an ambiguous/failed/unconfirmed cancel ->
    SHADOW_ERROR, all under the same audit_run_id. Previously the run
    stopped after EXECUTION_PLANNED and stayed open forever, which is the
    one thing the exactly-one-terminal-event invariant forbids.

    On the success path a terminal-audit failure is NOT swallowed: it
    raises AUDIT_PERSISTENCE rather than returning a clean-looking
    result. In the exception handlers it is best-effort and alerted, so
    the ORIGINAL failure -- the more informative one -- still reaches the
    caller. Either way the durable order state is left at whatever was
    actually confirmed; it is never rolled back to hide the outcome.
    """
    audit_run_id = validate_audit_run_id(audit_run_id)
    current = now or datetime.now(timezone.utc)
    # CODEX-054: the AUTHORITY for terminal classification. Not the
    # exception type -- whether broker.cancel_order() was actually
    # invoked. A post-transport failure that surfaced as a generic
    # ExecutionEngineError used to be reported as SHADOW_BLOCKED, i.e.
    # "never reached the broker", for an order that may well be cancelled
    # at KIS.
    transport = {"attempted": False, "completed": False}
    terminal_written = False
    inner_returned = False
    try:
        result = _cancel_inner(
            order_intent=order_intent, broker_order_id=broker_order_id,
            cancel_gate_context_builder=cancel_gate_context_builder, conn=conn, broker=broker,
            instrument=instrument, audit_run_id=audit_run_id, current=current,
            transport=transport,
        )
        inner_returned = True
    except FatalRepositoryConnectionError as exc:
        # CODEX-059: fatal outranks everything. A terminal event is still
        # attempted -- on shadow_audit's OWN connection, never the
        # poisoned one -- but purely best-effort: whether it succeeds or
        # fails, the exception that leaves this function is the fatal
        # one, unchanged, so the entrypoint exits 4.
        terminal_written = _finalize_cancel(
            audit_run_id=audit_run_id, terminal_event=shadow_audit.SHADOW_ERROR,
            order_intent=order_intent, broker_order_id=broker_order_id,
            reason_code=REASON_FATAL_REPOSITORY_CONNECTION, detail=type(exc).__name__,
            now=current, transport=transport, best_effort=True,
        )
        _alert_fatal_connection(order_intent=order_intent, error=exc)
        raise
    except BaseException as exc:  # noqa: BLE001 -- no path may leave the run open
        terminal_written = _finalize_cancel(
            audit_run_id=audit_run_id,
            terminal_event=_cancel_terminal_for(transport, exc),
            order_intent=order_intent, broker_order_id=broker_order_id,
            reason_code=_cancel_reason_for(exc, transport), detail=str(exc), now=current,
            transport=transport, best_effort=True,
        )
        raise
    finally:
        # Safety net for the FAILURE paths only -- the success outcome is
        # finalized below, after this block, so this must not pre-empt it.
        # It classifies by the same authority, so it cannot mislabel a
        # post-transport failure as blocked either.
        # CODEX-059: `_finalize_cancel(best_effort=True)` swallows every
        # audit failure, so this net cannot downgrade an in-flight fatal
        # into an audit error. The one exception it does re-raise is a
        # FatalRepositoryConnectionError, which would replace a fatal
        # with a fatal -- the entrypoint still exits 4 either way. That is
        # the property being relied on here, and it is asserted by tests
        # rather than left implicit.
        if not inner_returned and not terminal_written:
            _finalize_cancel(
                audit_run_id=audit_run_id,
                terminal_event=_cancel_terminal_for(transport),
                order_intent=order_intent, broker_order_id=broker_order_id,
                reason_code="CANCEL_RUN_NOT_FINALIZED", detail=None, now=current,
                transport=transport, best_effort=True,
            )

    # Success path. Unlike the handler above, a terminal-audit failure
    # here is NOT best-effort: returning a successful-looking result for a
    # cancel whose outcome was never audited is exactly what CODEX-053
    # forbids. The durable order state is left at whatever was actually
    # confirmed -- it is never rolled back to hide the outcome.
    if result.status == "CANCELLED":
        _finalize_cancel(
            audit_run_id=audit_run_id, terminal_event=shadow_audit.SHADOW_COMPLETED,
            order_intent=order_intent, broker_order_id=broker_order_id,
            reason_code="CANCEL_CONFIRMED", final_order_state="CANCELLED", now=current,
            transport=transport,
        )
    else:
        # KIS returned something other than a confirmed cancel; the order
        # was already forced to UNKNOWN by _cancel_inner(). The transport
        # ran, so this is an execution ERROR, never a block.
        _finalize_cancel(
            audit_run_id=audit_run_id, terminal_event=shadow_audit.SHADOW_ERROR,
            order_intent=order_intent, broker_order_id=broker_order_id,
            reason_code="CANCEL_OUTCOME_UNKNOWN", detail=result.blocked_reason,
            final_order_state=result.status, now=current, transport=transport,
        )
    return result


def _alert_cancel_persistence_failure(*, order_intent, unknown_persisted, error,
                                       unknown_error=None, connection_invalidated=False):
    """Operator alert for the one case where KIS's answer and our durable
    state have genuinely diverged.

    CODEX-055: errors are reported by TYPE, never by message -- a raw
    exception string can carry SQL text and bound parameters, and this
    goes to an external channel."""
    try:
        from operations import alerts

        alerts.send_alert(
            "*KIS cancel confirmed but final state not persisted*\n"
            f"- internal_order_id: {order_intent.internal_order_id}\n"
            f"- symbol: {order_intent.symbol}\n"
            f"- left UNKNOWN for reconciliation: {unknown_persisted}\n"
            f"- database connection invalidated: {connection_invalidated}\n"
            f"- error type: {type(error).__name__}\n"
            f"- UNKNOWN fallback error type: "
            f"{type(unknown_error).__name__ if unknown_error else 'none'}\n"
            "- action: no automatic re-cancel; manual reconciliation against KIS order "
            "history is required"
        )
    except Exception as exc:  # noqa: BLE001 -- alerting must not mask the failure
        logger.error("could not alert on cancel persistence failure: %s", exc)


def _alert_fatal_connection(*, order_intent, error):
    """CRITICAL alert for a fault that requires a process restart. Only
    the error TYPE is reported -- never SQL, a connection repr, an
    account number or a broker payload."""
    try:
        from operations import alerts

        alerts.send_alert(
            "*CRITICAL: fatal order-state connection fault during cancel*\n"
            f"- internal_order_id: {order_intent.internal_order_id}\n"
            f"- symbol: {order_intent.symbol}\n"
            f"- error type: {type(error).__name__}\n"
            "- HALT: set\n"
            "- action: process must restart to release the SQLite write lock; manual "
            "reconciliation against KIS order history is required"
        )
    except Exception as exc:  # noqa: BLE001 -- alerting must not mask the fault
        logger.error("could not alert on a fatal connection fault: %s", exc)


def _alert_repository_read_failure(*, order_intent, operation, error):
    """CODEX-057: a durable-read fault is operator-visible. Only the error
    TYPE is reported -- a raw SQLite message can carry SQL text and bound
    parameters, and this goes to an external channel."""
    logger.error(
        "order state read failed: operation=%s order=%s error=%s",
        operation, order_intent.internal_order_id, type(error).__name__,
    )
    try:
        from operations import alerts

        alerts.send_alert(
            "*Order state could not be read*\n"
            f"- operation: {operation}\n"
            f"- internal_order_id: {order_intent.internal_order_id}\n"
            f"- symbol: {order_intent.symbol}\n"
            f"- error type: {type(error).__name__}\n"
            "- action: this is a database fault, not a policy block; manual reconciliation "
            "is required"
        )
    except Exception as exc:  # noqa: BLE001 -- alerting must not mask the failure
        logger.error("could not alert on an order state read failure: %s", exc)


# CODEX-057: infrastructure faults that must never be reported as a
# policy block, even though they happen before the transport.
#
# Deliberately narrow. A pre-transport AUDIT persistence failure stays
# SHADOW_BLOCKED: the execution really was refused before reaching the
# broker, which is what that terminal means, and the audit failure
# itself is separately alerted by handle_audit_failure(). Only a failure
# to READ the durable order state is reclassified, because that is the
# case where "we refused" would be an outright false statement -- we
# never got far enough to decide anything.
_SYSTEM_FAULT_REASONS = frozenset({
    REASON_STATE_READ_FAILURE,
})


def _is_system_fault(exc):
    """A DATABASE fault, as opposed to a decision. SHADOW_BLOCKED means
    "we refused"; reporting "we could not look" that way tells an
    operator the opposite of the truth."""
    if isinstance(exc, (FatalRepositoryConnectionError, OrderRepositoryReadError)):
        return True
    return getattr(exc, "reason_code", None) in _SYSTEM_FAULT_REASONS


def _cancel_terminal_for(transport, exc=None):
    """CODEX-054: SHADOW_BLOCKED means "the execution never reached the
    broker", so once the transport has been attempted every failure is
    SHADOW_ERROR.

    CODEX-057 adds the second half: a failure BEFORE the transport is
    only a block if it was a DECISION. A database fault is an error at
    any point in the flow."""
    if transport["attempted"] or _is_system_fault(exc):
        return shadow_audit.SHADOW_ERROR
    return shadow_audit.SHADOW_BLOCKED


def _cancel_reason_for(exc, transport):
    if isinstance(exc, FatalRepositoryConnectionError):
        return REASON_STATE_PERSISTENCE
    explicit = getattr(exc, "reason_code", None)
    if explicit:
        return explicit
    if isinstance(exc, (KISAmbiguousResponseError, KISBrokerError)):
        return "CANCEL_OUTCOME_UNKNOWN"
    if transport["attempted"]:
        return "CANCEL_POST_TRANSPORT_ERROR"
    return "CANCEL_BLOCKED"


def _finalize_cancel(*, audit_run_id, terminal_event, order_intent, broker_order_id,
                      reason_code, now, detail=None, final_order_state=None, transport=None,
                      best_effort=False):
    """Ends a cancel run. Returns True if a terminal event is now durably
    recorded for this run.

    `best_effort=True` is used from exception handlers: an audit failure
    there must be alerted but must NOT replace the original exception,
    which is the more informative one. On the success path it is False,
    so an unrecordable outcome surfaces as AUDIT_PERSISTENCE instead of
    being returned as a clean cancel."""
    payload = {
        # CODEX-050: never the raw broker id in a free-text field; the
        # structural redactor sees this dict and the id itself is a KIS
        # order number, not a secret, but it is masked for symmetry with
        # every other durable payload.
        "broker_order_id_last4": (str(broker_order_id)[-4:] if broker_order_id else None),
        "internal_order_id": order_intent.internal_order_id,
        "symbol": order_intent.symbol,
    }
    if transport is not None:
        # CODEX-054: the audit row itself records whether the transport
        # ran, so an auditor can check the classification rather than
        # having to trust it.
        payload["transport_attempted"] = transport["attempted"]
        payload["transport_completed"] = transport["completed"]
    if detail:
        payload["detail"] = detail
    if final_order_state:
        payload["final_order_state"] = final_order_state
    try:
        shadow_audit.finalize_audit_run(
            audit_run_id=audit_run_id, terminal_event=terminal_event,
            internal_order_id=order_intent.internal_order_id, action="cancel",
            symbol=order_intent.symbol, side="cancel", reason_code=reason_code,
            payload=payload, now=now,
        )
        return True
    except FatalRepositoryConnectionError:
        raise  # CODEX-059: audit handling never outranks a fatal fault
    except (shadow_audit.ShadowAuditError, shadow_audit.AuditInvariantError) as exc:
        if not best_effort:
            raise ExecutionEngineError(
                f"cancel outcome could not be durably audited: {exc}",
                reason_code=REASON_AUDIT_PERSISTENCE,
            ) from exc
        # Alert, then let the ORIGINAL exception propagate unchanged.
        try:
            shadow_audit.handle_audit_failure(
                exc, shadow_run_id=audit_run_id, symbol=order_intent.symbol, side="cancel",
                stage=f"cancel:{terminal_event}",
            )
        except Exception:  # noqa: BLE001 -- handle_audit_failure always raises
            pass
        return False


def _cancel_inner(*, order_intent, broker_order_id, cancel_gate_context_builder, conn, broker,
                   instrument, audit_run_id, current, transport):
    """The cancel flow itself. Raises for every refusal so submit_cancel()
    above owns terminal-event handling in exactly one place.

    `transport` is the shared marker submit_cancel() classifies on. It is
    set to attempted=True IMMEDIATELY before broker.cancel_order() and
    never reset, so any failure from that point on -- including one that
    surfaces as a plain ExecutionEngineError -- is classified as an
    execution error rather than as a pre-transport block (CODEX-054)."""
    with idempotency.single_run_lock():
        try:
            record = order_repository.load(conn, order_intent.internal_order_id)
        except OrderRepositoryReadError as exc:
            # CODEX-057: "the order does not exist" and "the database
            # could not be read" are opposite conclusions. Reporting a
            # read fault as a policy block would tell an operator the
            # cancel was refused on purpose.
            _alert_repository_read_failure(
                order_intent=order_intent, operation="load order for cancel", error=exc,
            )
            raise CancelPostTransportError(
                "cancel could not proceed -- the durable order state could not be read; "
                "manual reconciliation required",
                reason_code=REASON_STATE_READ_FAILURE,
            ) from exc
        if record is None:
            raise CancelPreTransportBlocked(
                f"cancel blocked -- no durable order record exists for "
                f"{order_intent.internal_order_id!r}",
                reason_code="CANCEL_NO_ORDER_RECORD",
            )

        try:
            authorized = authorization.authorize_cancel(
                order_intent, cancel_gate_context_builder, order_gate.evaluate_cancel_gate, now=current,
            )
        except (order_gate.OrderGateBlockedError, UnauthorizedExecutionError) as exc:
            raise CancelPreTransportBlocked(
                f"cancel blocked by order gate: {exc}",
                reason_code=f"{REASON_GATE}:{getattr(exc, 'code', 'GATE')}",
            ) from exc

        # CODEX-053: a cancel is a real transport call, so it carries the
        # same audit obligation a new order does.
        _audit_before_transport(
            audit_run_id=audit_run_id, event_type=shadow_audit.GATE_APPROVED,
            order_intent=order_intent, side_label="cancel", now=current,
            reason_code="CANCEL_APPROVED",
        )

        try:
            record = order_repository.advance(
                conn, record, "CANCEL_PENDING", event_type="CANCEL_REQUESTED",
                event_payload={"broker_order_id": broker_order_id},
                broker_order_id=broker_order_id, now=current,
            )
        except FatalRepositoryConnectionError:
            raise  # CODEX-059
        except (OrderStateTransitionError, OrderRepositoryError) as exc:
            raise CancelPreTransportBlocked(
                f"cancel blocked -- could not durably record CANCEL_PENDING for "
                f"{order_intent.internal_order_id!r}: {exc}",
                reason_code=REASON_STATE_PERSISTENCE,
            ) from exc

        _audit_before_transport(
            audit_run_id=audit_run_id, event_type=shadow_audit.EXECUTION_PLANNED,
            order_intent=order_intent, side_label="cancel", now=current,
            reason_code="CANCEL_PENDING", detail=f"broker_order_id={broker_order_id}",
        )

        # From here on, any failure is a POST-transport failure.
        transport["attempted"] = True
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
        transport["completed"] = True

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
        except FatalRepositoryConnectionError:
            # CODEX-059: the connection could neither be rolled back nor
            # closed. It must NOT be converted into an ordinary execution
            # error, and nothing further may be attempted on it -- no
            # UNKNOWN fallback, no additional CAS, no read. HALT and the
            # CRITICAL alert were already raised by the repository. This
            # propagates unchanged so the service entrypoint fail-stops
            # the process, which is the only thing that actually releases
            # the SQLite write lock.
            logger.critical(
                "cancel final-state persistence hit a fatal connection fault for order %s -- "
                "propagating unchanged so the process can fail-stop",
                order_intent.internal_order_id,
            )
            raise
        except Exception as exc:  # noqa: BLE001 -- every failure is normalized (CODEX-055)
            # CODEX-054: KIS CONFIRMED the cancel and we could not record
            # it. This is not a block -- the execution happened. Do the
            # best safe thing (leave the order durably UNKNOWN so
            # reconciliation resolves it against KIS's own history), never
            # re-cancel automatically, and never return success.
            #
            # CODEX-055/056: the UNKNOWN fallback itself may fail with any
            # exception class, including one that invalidated the
            # connection. Every one of those still gets a normalized
            # reason code, an operator alert and a SHADOW_ERROR terminal.
            _record, unknown_persisted, unknown_error = _force_unknown_reported(
                conn, record,
                reason=f"could not durably record CANCELLED ({type(exc).__name__})", now=current,
            )
            invalidated = isinstance(exc, OrderRepositoryRollbackError) or isinstance(
                unknown_error, OrderRepositoryRollbackError,
            )
            # The broker result is logged through safe_repr(), never raw,
            # and the underlying errors only by TYPE -- their messages can
            # carry SQL text and bound parameters.
            logger.error(
                "cancel confirmed by KIS but final state not persisted: order=%s "
                "broker_result=%s unknown_persisted=%s connection_invalidated=%s "
                "error=%s unknown_error=%s",
                order_intent.internal_order_id, safe_repr(execution_record), unknown_persisted,
                invalidated, type(exc).__name__,
                type(unknown_error).__name__ if unknown_error else None,
            )
            _alert_cancel_persistence_failure(
                order_intent=order_intent, unknown_persisted=unknown_persisted,
                error=exc, unknown_error=unknown_error, connection_invalidated=invalidated,
            )
            if unknown_persisted and not invalidated:
                raise CancelPostTransportError(
                    "cancel was confirmed by KIS but its final state could not be recorded -- "
                    "the order was left UNKNOWN for reconciliation",
                    reason_code=REASON_CANCEL_FINAL_STATE_PERSISTENCE,
                ) from exc
            normalized = normalize_persistence_error(
                unknown_error or exc, stage="cancel final-state persistence",
                post_transport=True, connection_invalidated=invalidated,
            )
            raise CancelPostTransportError(
                str(normalized), reason_code=REASON_STATE_PERSISTENCE,
            ) from (unknown_error or exc)

        return ExecutionResult(
            internal_order_id=order_intent.internal_order_id, status=execution_record.status,
            execution_record=execution_record,
        )

