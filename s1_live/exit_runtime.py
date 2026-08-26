"""The only thing that turns an S1 exit decision into a real SELL.

This closes EXIT_PATH_NOT_WIRED:

    BUY actual fill -> S1PositionState -> persistence
      -> realtime Exit V0 evaluation -> SELL intent
      -> execution_engine.submit_sell_order() -> actual KIS SELL

Why this exists instead of `positions/lifecycle.check_and_manage()`
-------------------------------------------------------------------
The scalping lifecycle applies `risk_config.STOP_LOSS_RATE` (-8%), a
60-minute time stop and `TARGET_1/2_R_MULTIPLE`. Those were chosen for a
holding period of minutes; S1 holds across sessions. Routing an S1
position through that path would give it a stop 33% wider than the one
its position size was derived from, and would liquidate it an hour after
entry regardless of the trend. So S1 positions live in their own table
and are evaluated here, and a test asserts this module never calls the
scalping path.

It does NOT reimplement order submission. `submit_sell()` calls the same
`KISBrokerAdapter.submit_order(side="sell")` the lifecycle calls, which
runs the same reconciliation snapshot, the same order gate, the same
idempotency ledger and the same audit trail. The only thing new here is
WHICH policy decides, never HOW the order is placed.

Duplicate SELL is prevented in three independent places
-------------------------------------------------------
    1. `s1_positions.exit_submitted`  -- `decide()` returns HOLD forever
    2. `exit_intents`                 -- reserve() refuses a second
                                         active intent per position
    3. `kis_order_idempotency`        -- the engine's own registration

Any one of them alone would do it. All three are kept because the cost
of being wrong is selling a position twice with real money.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from s1_live import exit_policy, position_store
from state_store import exit_intent_ledger

logger = logging.getLogger(__name__)

#: Returned per position so a caller (and the tests) can assert exactly
#: what happened without reading the database again.
ACTION_HELD = "HELD"
ACTION_RATCHETED = "RATCHETED"
ACTION_SOLD = "SOLD"
ACTION_LATCHED = "EXIT_PENDING_LATCHED"
ACTION_PENDING_RESUBMITTED = "PENDING_EXIT_SUBMITTED"
ACTION_BLOCKED = "SELL_BLOCKED"


@dataclass
class SessionPolicy:
    """Whether the CURRENT session accepts orders, and on whose authority.

    `orders_allowed` must come from verified broker support (spec §5).
    An unverified session is False, which latches exits rather than
    dropping them -- see `_handle_sell`.
    """

    name: str
    orders_allowed: bool
    verification: str = "UNVERIFIED"

    @property
    def sell_allowed(self) -> bool:
        return bool(self.orders_allowed)


@dataclass
class ExitOutcome:
    position_id: str
    symbol: str
    action: str
    reason: Optional[str] = None
    detail: str = ""
    broker_status: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        return dict(vars(self))


def _broker_order_id(response):
    """The KIS order id carried back on an accepted order.

    Recorded onto the intent because the fill inquiry looks a SELL up by
    it and by nothing else. Dropped, the order is live at the broker and
    unreachable from the intent: the position stays EXIT_SUBMITTED and
    the fill is never collected, which is how DT (KIS 0030785946) stalled
    -- accepted, filled at the broker, and invisible to every tick.

    Absence is returned as None rather than raised. The order has already
    been accepted by the time this is read, and refusing to record the
    submission because its id could not be parsed would be strictly worse
    than recording it without one.
    """
    data = getattr(response, "data", None)
    if isinstance(data, dict):
        found = data.get("id")
        if found:
            return str(found)
    return None


def _submit_sell(conn, *, broker_adapter, position_id, row, reason, now=None,
                 store=None, prefix="s1exit") -> ExitOutcome:
    """Reserve the intent, place the order through the VERIFIED path, and
    record the outcome. Never places a second order for a position.

    `store` is the position store to latch against, defaulting to S1's.
    S2 passes its own and everything else here is byte-identical: the
    ledger reservation, the UNKNOWN handling that refuses to retry, the
    rejection path that does not chase the price. Those behaviours were
    hard-won on S1 and a second copy of them would be a second idea of
    what is safe -- which is exactly what the buy cycle's docstring
    warns about, applied to the sell side.
    """
    symbol, quantity = row["symbol"], int(row["quantity"])
    store = store or position_store

    # Ledger-level duplicate prevention. If an intent is already active
    # the broker is not called at all.
    client_order_id = f"{prefix}-{symbol}-{uuid.uuid4().hex[:12]}"
    try:
        intent_id = exit_intent_ledger.reserve(
            conn, position_id, reason, quantity, client_order_id)
    except exit_intent_ledger.DuplicateExitIntentError as exc:
        logger.warning("S1 exit for %s already has an active intent -- not "
                       "calling the broker: %s", position_id, exc)
        return ExitOutcome(position_id, symbol, ACTION_BLOCKED, reason,
                           f"active exit intent already exists: {exc}")

    try:
        response = broker_adapter.submit_order(
            symbol, quantity, side="sell", client_order_id=client_order_id)
    except Exception as exc:
        # Spec §10 / §9: an ambiguous or failed submission is NEVER
        # auto-retried and NEVER clears the trigger. The intent goes to
        # SUBMISSION_UNKNOWN and reconciliation decides.
        exit_intent_ledger.mark_submission_unknown(conn, intent_id)
        store.latch_pending_exit(conn, position_id, reason, now=now)
        logger.error("S1 exit submission for %s ended UNKNOWN -- left latched for "
                     "reconciliation, not retried: %s", position_id, exc)
        return ExitOutcome(position_id, symbol, ACTION_BLOCKED, reason,
                           f"submission unknown: {exc}")

    status = getattr(response, "status_code", None)
    accepted = status is not None and 200 <= int(status) < 300
    if not accepted:
        # Spec §10: record the rejection, do not chase the price, do not
        # enlarge the quantity, do not retry in a loop. The trigger stays
        # latched so the next orderable session tries once more.
        exit_intent_ledger.mark_aborted(conn, intent_id)
        store.latch_pending_exit(conn, position_id, reason, now=now)
        logger.error("S1 exit for %s REJECTED by broker (status=%s): %s",
                     position_id, status, getattr(response, "text", ""))
        return ExitOutcome(position_id, symbol, ACTION_BLOCKED, reason,
                           f"broker rejected: {getattr(response, 'text', '')}", status)

    exit_intent_ledger.mark_submitted(
        conn, intent_id, broker_order_id=_broker_order_id(response))
    store.mark_exit_submitted(conn, position_id, reason, now=now)
    logger.info("%s SELL submitted: %s %s qty=%d reason=%s",
                prefix.upper(), position_id, symbol, quantity, reason)
    return ExitOutcome(position_id, symbol, ACTION_SOLD, reason,
                       getattr(response, "text", "") or "", status)


def _handle_sell(conn, *, broker_adapter, position_id, row, reason, session,
                 now=None) -> ExitOutcome:
    """Spec §9: a triggered exit is submitted if the session allows it and
    LATCHED if it does not. It is never discarded and never re-decided."""
    if not session.sell_allowed:
        position_store.latch_pending_exit(conn, position_id, reason, now=now)
        return ExitOutcome(
            position_id, row["symbol"], ACTION_LATCHED, reason,
            f"session {session.name} does not accept orders "
            f"({session.verification}) -- exit latched for the next orderable session")
    return _submit_sell(conn, broker_adapter=broker_adapter, position_id=position_id,
                        row=row, reason=reason, now=now)


def evaluate_position(conn, *, broker_adapter, position_id, state, row, current_price,
                      features=None, session, session_date=None, emergency=False,
                      now=None) -> ExitOutcome:
    """One position, one tick, at most one order."""
    symbol = row["symbol"]

    # Count the session ONCE per calendar session, before deciding, so
    # the time exit sees the right number however many times we tick.
    if session_date is not None:
        try:
            held = position_store.advance_session(conn, position_id, session_date, now=now)
            state.sessions_held = held
        except position_store.S1PositionStoreError as exc:
            logger.warning("could not advance S1 session count for %s: %s", position_id, exc)

    # A latched exit outranks a fresh decision: re-deciding would let a
    # later HOLD tick erase a stop that already triggered.
    if row.get("pending_exit_reason") and not row.get("exit_submitted"):
        reason = row["pending_exit_reason"]
        if not session.sell_allowed:
            return ExitOutcome(position_id, symbol, ACTION_LATCHED, reason,
                               f"still latched -- session {session.name} does not accept orders")
        outcome = _submit_sell(conn, broker_adapter=broker_adapter,
                               position_id=position_id, row=row, reason=reason, now=now)
        if outcome.action == ACTION_SOLD:
            outcome.action = ACTION_PENDING_RESUBMITTED
        return outcome

    decision = exit_policy.decide(state, current_price=current_price,
                                  features=features, emergency=emergency)

    if decision.action == exit_policy.SELL:
        return _handle_sell(conn, broker_adapter=broker_adapter, position_id=position_id,
                            row=row, reason=decision.reason, session=session, now=now)

    if decision.action == exit_policy.RATCHET:
        # A ratchet is a state change, never an order. It is applied in
        # EVERY session, including ones that cannot trade, because the
        # floor it records is what protects the position later.
        try:
            position_store.apply_ratchet(
                conn, position_id,
                new_protective_floor_r=decision.new_protective_floor_r,
                peak_r=decision.unrealised_r or 0.0, now=now)
        except position_store.S1PositionStoreError as exc:
            logger.error("could not persist S1 ratchet for %s: %s", position_id, exc)
            return ExitOutcome(position_id, symbol, ACTION_HELD, None,
                               f"ratchet not persisted: {exc}")
        return ExitOutcome(position_id, symbol, ACTION_RATCHETED, None, decision.detail)

    # HOLD -- still record a new high-water mark, which the floor derives from.
    if decision.unrealised_r is not None:
        try:
            position_store.record_peak(conn, position_id, decision.unrealised_r, now=now)
        except position_store.S1PositionStoreError:
            logger.warning("could not persist S1 peak for %s", position_id, exc_info=True)
    return ExitOutcome(position_id, symbol, ACTION_HELD, decision.reason, decision.detail)


def run_exit_cycle(conn, *, broker_adapter, price_fn, session, features_fn=None,
                   session_date=None, emergency=False, now=None) -> List[ExitOutcome]:
    """Evaluate every live S1 position. EXIT_PENDING positions go first.

    `price_fn(symbol)` returns the CURRENT-SESSION realtime price, which
    is deliberately a different data source from the completed daily bars
    S1's entry signal is computed on (spec §6). `features_fn(symbol)`
    returns the daily HMA structure for the trend axis, or None when it
    cannot be established -- in which case the trend axis abstains rather
    than guessing.
    """
    outcomes = []
    for position_id, state, row in position_store.load_live(conn):
        symbol = row["symbol"]
        try:
            price = price_fn(symbol)
        except Exception as exc:
            logger.error("S1 exit: no realtime price for %s -- holding: %s", symbol, exc)
            outcomes.append(ExitOutcome(position_id, symbol, ACTION_HELD,
                                        exit_policy.REASON_INSUFFICIENT_DATA, str(exc)))
            continue
        features = None
        if features_fn is not None:
            try:
                features = features_fn(symbol)
            except Exception:
                logger.warning("S1 exit: trend features unavailable for %s -- the "
                               "trend axis abstains", symbol, exc_info=True)
        outcomes.append(evaluate_position(
            conn, broker_adapter=broker_adapter, position_id=position_id, state=state,
            row=row, current_price=price, features=features, session=session,
            session_date=session_date, emergency=emergency, now=now))
    return outcomes
