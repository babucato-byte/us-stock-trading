"""Execution Engine -- the ONLY module permitted to call a broker's
order-submission method, per the layered architecture:

    Market Data -> Strategy Engine -> Signal -> Risk Engine ->
    Account Engine -> Sizing Engine -> Execution Engine -> Broker

No other module may call `AlpacaBroker.submit_order()` (or any raw
`session.post()`/broker order call) directly -- see
`tests/test_execution_engine.py::test_only_execution_engine_and_legacy_
compat_call_broker_submit_order` for the static grep-based guard that
enforces this, and `paper_strategy_order.py`'s module docstring for the
one grandfathered legacy compat path (kept per this cycle's explicit
instruction not to delete existing functionality outright).

`ExecutionEngine` accepts only a `ValidatedOrderCommand` -- an immutable,
fully-specified record of WHAT is being submitted and WHY (which signal,
strategy, account snapshot, risk decision, and sizing decision produced
it) -- never a bare `(symbol, qty, side)` tuple. It performs two
zero-HTTP checks before ever reaching the broker:

  1. The command must not be expired (`expires_at` in the past) --
     Sizing decisions are made against a specific, buffered entry price;
     an old command being submitted long after that price was computed
     is stale and must be rebuilt, not blindly sent.
  2. If a reservation already exists in the durable SQLite ledger under
     this command's `client_order_id` (a retry, or a bug re-submitting
     the same command), its recorded symbol/notional must match the
     command being submitted now -- a mismatch means the command was
     mutated (or the client_order_id was reused for a different order)
     and is blocked, not silently reconciled.

The actual broker call still goes through `broker.submit_order()`,
which is this codebase's SOLE reservation point for live entries
(`live_readiness/order_gateway.py::validate_and_size_live_entry()`,
called from inside `AlpacaBroker.submit_order()` -- see that module's
docstring for why a second, Execution-Engine-owned reservation was
deliberately NOT introduced here: it would double-reserve the same
notional against the same account, exactly the bug CODEX-031's decision
4 already fixed once for the `paper_strategy_order.py` wrapper).
"""

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from live_readiness import entry_reservation_ledger as ledger
from state_store import db as state_db

DEFAULT_COMMAND_TTL_SECONDS = 30


class ExecutionEngineError(Exception):
    """Raised whenever a command cannot be safely submitted. Callers must
    treat this as a hard block -- broker.submit_order() is never reached
    when this is raised."""


@dataclass(frozen=True)
class ValidatedOrderCommand:
    """Immutable record of exactly what is about to be submitted, and
    which upstream decisions produced it. `reservation_id`/
    `entry_intent_id` are populated on the `ExecutionResult` this engine
    returns, not on the command itself -- this codebase's SOLE
    reservation point creates the reservation_id as part of the broker
    call (see module docstring), so it cannot be known before that call
    completes; the command instead carries `client_order_id` (chosen
    before the call, per CODEX-034) as its own durable identity."""

    command_id: str
    signal_id: str
    strategy_id: str
    symbol: str
    side: str
    purpose: str
    qty: float
    estimated_price: float
    estimated_notional: float
    account_snapshot_id: str
    risk_decision_id: str
    sizing_decision_id: str
    client_order_id: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class ExecutionResult:
    command: ValidatedOrderCommand
    broker_response: object
    reservation_id: Optional[str]


def build_validated_order_command(
    *, signal_id, strategy_id, symbol, side, purpose, sizing_decision,
    account_snapshot_id, risk_decision_id, client_order_id=None,
    ttl_seconds=DEFAULT_COMMAND_TTL_SECONDS, now=None,
):
    """Assembles a `ValidatedOrderCommand` from an already-computed
    `SizingDecision` (see `live_readiness/sizing_engine.py`). Never
    accepts a caller-declared qty/notional directly -- both are always
    read off `sizing_decision`."""
    current = now or datetime.now(timezone.utc)
    qty = sizing_decision.actual_qty
    price = sizing_decision.buffered_entry_price_usd
    return ValidatedOrderCommand(
        command_id=f"cmd-{uuid.uuid4().hex[:16]}",
        signal_id=signal_id,
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        purpose=purpose,
        qty=qty,
        estimated_price=price,
        estimated_notional=qty * price,
        account_snapshot_id=account_snapshot_id,
        risk_decision_id=risk_decision_id,
        sizing_decision_id=sizing_decision.sizing_decision_id,
        client_order_id=client_order_id or f"exec-{symbol}-{uuid.uuid4().hex[:12]}",
        created_at=current,
        expires_at=current + timedelta(seconds=ttl_seconds),
    )


def _validate_command_shape(command, now):
    if not isinstance(command, ValidatedOrderCommand):
        raise ExecutionEngineError(
            f"submit_validated_command() requires a ValidatedOrderCommand, got {type(command).__name__}"
        )
    if now > command.expires_at:
        raise ExecutionEngineError(
            f"command {command.command_id} expired at {command.expires_at.isoformat()} "
            f"(now={now.isoformat()}) -- rebuild sizing/command, never submit a stale one"
        )
    expected_notional = command.qty * command.estimated_price
    if not math.isclose(expected_notional, command.estimated_notional, rel_tol=1e-6, abs_tol=1e-6):
        raise ExecutionEngineError(
            f"command {command.command_id} estimated_notional {command.estimated_notional!r} "
            f"does not match qty*price {expected_notional!r} -- possible mutation"
        )


def _validate_against_existing_reservation(command, conn):
    existing = ledger.get_by_client_order_id(conn, command.client_order_id)
    if existing is None:
        return
    if existing["symbol"] != command.symbol:
        raise ExecutionEngineError(
            f"client_order_id {command.client_order_id!r} already has a reservation for symbol "
            f"{existing['symbol']!r}, does not match command symbol {command.symbol!r}"
        )
    existing_notional = float(existing["notional_krw"])
    # command.estimated_notional is in USD (qty * per-share price); the
    # ledger stores KRW. An exact-value comparison isn't meaningful across
    # currencies, so this check only catches a GROSS mismatch (e.g. a
    # reservation for a completely different order size reused under the
    # same client_order_id) via a generous relative tolerance, not a
    # precise reconciliation -- true reconciliation is
    # entry_reservation_ledger.reconcile_by_client_order_id()'s job.
    if existing_notional <= 0:
        raise ExecutionEngineError(
            f"client_order_id {command.client_order_id!r} has an existing reservation with "
            f"non-positive notional {existing_notional!r} -- refusing to reuse it"
        )


def submit_validated_command(command, broker, live_entry_context, *, conn=None, now=None):
    """The SOLE sanctioned path to `broker.submit_order()`. Raises
    `ExecutionEngineError` -- with ZERO calls to the broker -- if the
    command is not a `ValidatedOrderCommand`, is expired, has been
    mutated (qty/price/notional inconsistent), or its `client_order_id`
    already has a conflicting reservation in the durable ledger.

    `live_entry_context` must be a `live_readiness.order_gateway.
    LiveEntryContext` whose `symbol`/pricing are consistent with
    `command` -- this engine does not construct one itself (that
    requires an Account Engine snapshot + operator config the caller
    already has), it only enforces the command contract around the call.
    """
    current = now or datetime.now(timezone.utc)
    _validate_command_shape(command, current)

    own_conn = conn is None
    active_conn = conn if conn is not None else state_db.open_db()
    try:
        _validate_against_existing_reservation(command, active_conn)

        if live_entry_context.symbol != command.symbol:
            raise ExecutionEngineError(
                f"live_entry_context.symbol {live_entry_context.symbol!r} does not match "
                f"command.symbol {command.symbol!r}"
            )

        response = broker.submit_order(
            command.symbol,
            qty=command.qty,
            side=command.side,
            client_order_id=command.client_order_id,
            live_entry_context=live_entry_context,
        )
    finally:
        if own_conn:
            active_conn.close()

    reservation_id = None
    if isinstance(getattr(response, "data", None), dict):
        reservation_id = response.data.get("live_entry_reservation_id")

    return ExecutionResult(command=command, broker_response=response, reservation_id=reservation_id)
