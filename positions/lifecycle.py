"""Position lifecycle operations (Stage 4, roadmap Phase 5): entry, fill
tracking, partial/full exits, time-stop, end-of-day forced close, restart
recovery, and PnL.

Design notes (see docs/autonomous/DECISION_LOG.md "Stage 4" section for the
full reasoning):

  - Entry submission reuses paper_strategy_order.try_reserve_order() +
    submit_order(side="buy") unchanged -- this is exactly what that
    machinery is for (kill switch + credential + RequestPurpose-gated
    broker call, plus the (symbol, order_date) duplicate-entry guard).
  - Exit submission calls paper_strategy_order.submit_order(side="sell")
    DIRECTLY, bypassing try_reserve_order()/is_duplicate_order(): those
    functions enforce "at most one row per (symbol, order_date)" as an
    *entry* dedup rule, which would incorrectly reject a legitimate exit
    order for a symbol that already has an entry recorded that day.
    submit_order() still runs the full kill-switch/credential/
    RequestPurpose safety gate for every exit -- only the entry-specific
    daily-duplicate bookkeeping is skipped.
  - Every entry MUST pass strategy.registry.require_active(strategy_id)
    first (Stage 3's guard) -- an inactive strategy can never reach the
    broker through this module.
  - Duplicate-exit prevention is structural, not best-effort: every state
    change that submits an order to the broker happens inside a single
    positions.store.locked_position() block, so a second concurrent
    attempt on the same position blocks until the first is fully recorded,
    then sees the updated state and has nothing left to do.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import paper_strategy_order
from clock import DEFAULT_CLOCK
from config import scalping_strategy_v1_config as cfg
from live_readiness import entry_reservation_ledger as eil_entry
from market_hours import MARKET_REGULAR_END, combine_eastern, eastern_now
from positions import fill_validation, order_status, states, store
from state_store import db as state_db
from state_store import exit_intent_ledger as eil
from strategy import registry as strategy_registry


class PositionLifecycleError(Exception):
    """Raised for programming-error-level misuse (e.g. entering a position
    for a strategy with no signal). Not raised for ordinary trading
    conditions (kill switch engaged, broker rejection, etc.) -- those are
    recorded on the position record instead, per the project's fail-closed,
    sentinel-over-exception convention for expected trading states."""


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def enter_position(strategy, symbol, bars, qty, *, order_date, mode="paper", dry_run=True,
                    broker=None, as_of=None, registry=None, lock_timeout=store.LOCK_TIMEOUT_SECONDS):
    """Evaluate `strategy` against `bars`, and if (and only if) it signals
    an entry, reserve + submit a real "buy" order through
    paper_strategy_order's existing safety-gated path, then create and
    return a position record.

    Returns None if the strategy does not signal an entry for this bar
    data (no position is created in that case -- there is nothing to
    track). Raises PositionLifecycleError if the strategy is not ACTIVE
    in the registry (checked via require_active() before anything else
    happens -- no reservation, no broker call). `registry` defaults to
    strategy.registry.default_registry but callers (especially tests)
    should pass their own isolated StrategyRegistry() instance, per that
    module's own guidance on not sharing the process-wide default across
    tests. Kill-switch blocks and broker rejections are NOT exceptions --
    they result in a position record ending in REJECTED, since "the
    strategy correctly signalled but the order was refused" is an
    ordinary, expected outcome the caller needs to observe on the record,
    not a bug.
    """
    registry = registry or strategy_registry.default_registry
    try:
        registry.require_active(strategy.strategy_id)
    except strategy_registry.StrategyNotActiveError as exc:
        raise PositionLifecycleError(
            f"Refusing to enter a position for {strategy.strategy_id!r}: not ACTIVE "
            f"in the registry ({exc})"
        ) from None

    evaluation = strategy.generate_entry(bars, symbol=symbol, as_of=as_of)
    if not evaluation.signal:
        return None

    record = store.create_position(
        strategy.strategy_id, strategy.version, symbol,
        client_order_id=None, requested_qty=qty,
    )
    position_id = record["position_id"]

    with store.locked_position(position_id, lock_timeout=lock_timeout) as locked:
        states.validate_transition(locked["state"], states.ARMED)
        locked["state"] = states.ARMED
        locked["state_history"].append({"state": states.ARMED, "at": _now_iso(), "reason": "entry signal"})

    try:
        _, client_order_id = paper_strategy_order.try_reserve_order(
            symbol, order_date, mode, dry_run, qty=qty, broker=broker,
        )
    except Exception as exc:
        with store.locked_position(position_id, lock_timeout=lock_timeout) as locked:
            states.validate_transition(locked["state"], states.REJECTED)
            locked["state"] = states.REJECTED
            locked["exit_reason"] = f"ENTRY_RESERVATION_FAILED: {exc}"
            locked["state_history"].append(
                {"state": states.REJECTED, "at": _now_iso(), "reason": str(exc)}
            )
        return store.load_position(position_id)

    with store.locked_position(position_id, lock_timeout=lock_timeout) as locked:
        locked["client_order_id"] = client_order_id
        states.validate_transition(locked["state"], states.ENTRY_RESERVED)
        locked["state"] = states.ENTRY_RESERVED
        locked["state_history"].append(
            {"state": states.ENTRY_RESERVED, "at": _now_iso(), "reason": "ledger reserved"}
        )

        response = paper_strategy_order.submit_order(
            symbol, qty=qty, broker=broker, client_order_id=client_order_id, side="buy",
        )
        ledger_path, ledger_lock_path = paper_strategy_order._intent_ledger_paths()
        accepted = response.status_code in (200, 201)
        if accepted:
            import order_intent_ledger
            order_intent_ledger.commit(ledger_path, ledger_lock_path, client_order_id)
            paper_strategy_order.update_order_status(symbol, order_date, "SUBMITTED")
            states.validate_transition(locked["state"], states.ENTRY_SUBMITTED)
            locked["state"] = states.ENTRY_SUBMITTED
            locked["broker_order_id"] = (response.data or {}).get("id") if isinstance(response.data, dict) else None
            locked["stop_price"] = evaluation.stop_price
            locked["target_1_price"] = evaluation.target_1
            locked["target_2_price"] = evaluation.target_2
            locked["entry_time"] = _now_iso()
            locked["state_history"].append(
                {"state": states.ENTRY_SUBMITTED, "at": _now_iso(),
                 "reason": f"broker accepted, status_code={response.status_code}"}
            )
            reservation_id = (
                response.data.get("live_entry_reservation_id")
                if isinstance(response.data, dict) else None
            )
            if reservation_id:
                # CODEX-031: link this position to its durable budget
                # reservation so entry_reservation_ledger.build_snapshot()
                # can later tell whether the position it funded has since
                # closed (used for the concurrent-open-position count,
                # not the cumulative 30K notional ceiling -- see that
                # module's docstring for why the two are scoped
                # differently). Best-effort: a failure here just means
                # this reservation stays counted as open a bit longer
                # than strictly necessary -- fail-closed (over-counts),
                # never fail-open.
                try:
                    conn = state_db.open_db()
                    try:
                        eil_entry.link_position(conn, reservation_id, position_id)
                    finally:
                        conn.close()
                except Exception:
                    pass
        else:
            import order_intent_ledger
            order_intent_ledger.abort(ledger_path, ledger_lock_path, client_order_id)
            paper_strategy_order.update_order_status(symbol, order_date, "SUBMISSION_FAILED")
            states.validate_transition(locked["state"], states.REJECTED)
            locked["state"] = states.REJECTED
            locked["exit_reason"] = f"BROKER_REJECTED: status_code={response.status_code}"
            locked["state_history"].append(
                {"state": states.REJECTED, "at": _now_iso(),
                 "reason": f"broker rejected, status_code={response.status_code}"}
            )

    return store.load_position(position_id)


# ---------------------------------------------------------------------------
# Fill tracking
# ---------------------------------------------------------------------------

def record_fill(position_id, filled_qty, average_fill_price, *, lock_timeout=store.LOCK_TIMEOUT_SECONDS):
    """Record a (possibly partial) fill against the entry order. Once
    filled_qty reaches requested_qty, transitions FILLED -> STOP_ACTIVE
    automatically (the constitution's "자동 손절" -- a stop is considered
    active the instant the position is fully filled, not something a
    separate call arms later).

    `filled_qty` is the new *cumulative* filled quantity (CODEX-027):
    validated via fill_validation.validate_cumulative_fill() before
    anything is mutated -- negative/NaN/Infinity/non-numeric quantities
    or prices, a quantity exceeding requested_qty, or a quantity that
    regresses below the previously recorded cumulative fill all raise
    InvalidFillError and leave the position record untouched. A repeat
    observation of the exact same cumulative (filled_qty,
    average_fill_price) already on record is a no-op (idempotent), not
    an error -- the same broker fill event may be delivered more than
    once.
    """
    with store.locked_position(position_id, lock_timeout=lock_timeout) as locked:
        if locked["state"] not in (states.ENTRY_SUBMITTED, states.PARTIALLY_FILLED):
            raise PositionLifecycleError(
                f"Cannot record a fill for position {position_id!r} in state "
                f"{locked['state']!r} (expected ENTRY_SUBMITTED or PARTIALLY_FILLED)"
            )
        previous_filled_qty = locked["filled_qty"] or 0
        is_duplicate_observation = (
            filled_qty == previous_filled_qty and average_fill_price == locked["average_fill_price"]
        )
        if not is_duplicate_observation:
            fill_validation.validate_cumulative_fill(
                locked["requested_qty"], previous_filled_qty, filled_qty, average_fill_price
            )

            locked["filled_qty"] = filled_qty
            locked["remaining_qty"] = filled_qty
            locked["average_fill_price"] = average_fill_price
            locked["last_reconciled_at"] = _now_iso()

            if filled_qty >= locked["requested_qty"]:
                states.validate_transition(locked["state"], states.FILLED)
                locked["state"] = states.FILLED
                locked["state_history"].append(
                    {"state": states.FILLED, "at": _now_iso(), "reason": f"filled_qty={filled_qty}"}
                )
                states.validate_transition(locked["state"], states.STOP_ACTIVE)
                locked["state"] = states.STOP_ACTIVE
                locked["state_history"].append(
                    {"state": states.STOP_ACTIVE, "at": _now_iso(), "reason": "fully filled, stop armed"}
                )
            else:
                states.validate_transition(locked["state"], states.PARTIALLY_FILLED)
                locked["state"] = states.PARTIALLY_FILLED
                locked["state_history"].append(
                    {"state": states.PARTIALLY_FILLED, "at": _now_iso(), "reason": f"filled_qty={filled_qty}"}
                )
    return store.load_position(position_id)


# ---------------------------------------------------------------------------
# PnL
# ---------------------------------------------------------------------------

def _add_realized_pnl(record, exit_qty, exit_price):
    entry_price = record["average_fill_price"]
    record["realized_pnl"] = (record["realized_pnl"] or 0.0) + (exit_price - entry_price) * exit_qty


def compute_unrealized_pnl(record, current_price):
    """Read-only: does not touch the store. `current_price` must be
    supplied by the caller -- this module never fetches live prices
    (Phase 3/out of scope)."""
    if record["remaining_qty"] in (None, 0) or record["average_fill_price"] is None:
        return 0.0
    return (current_price - record["average_fill_price"]) * record["remaining_qty"]


# ---------------------------------------------------------------------------
# Exit submission (shared by every exit path below) -- CODEX-023/024
#
# CODEX-023: broker order *acceptance* (HTTP 200/201, status="accepted"/
# "new"/etc.) is never treated as a fill. Only a broker order status of
# "filled"/"partially_filled" ever changes remaining_qty or realized PnL;
# an accepted-but-unfilled exit leaves the position in a submitted state
# (EXIT_SUBMITTED/PARTIAL_EXIT_SUBMITTED) until a later reconciliation
# confirms an actual fill.
#
# CODEX-024: every exit reserves a durable SQLite exit_intents row (see
# state_store/exit_intent_ledger.py) BEFORE the broker is ever called.
# That reservation, together with the position's own state transition, is
# committed to disk before Phase B's broker call -- so a crash mid-call
# leaves a record a restart can reconcile, rather than silently reverting
# to the pre-exit state and letting a naive retry submit a second sell.
# ---------------------------------------------------------------------------

def _open_exit_db():
    return state_db.open_db()


def _target_submitted_state(on_fully_filled_state):
    return states.PARTIAL_EXIT_SUBMITTED if on_fully_filled_state == states.PARTIAL_EXITED else states.EXIT_SUBMITTED


def _transition_to_submitted(locked, on_fully_filled_state, client_order_id, note):
    """Move `locked` toward its submitted state, routing through
    TARGET_1_ACTIVE first when required (STOP_ACTIVE has no direct edge
    to PARTIAL_EXIT_SUBMITTED -- see positions/states.py's TRANSITIONS)."""
    target_state = _target_submitted_state(on_fully_filled_state)
    if target_state == states.PARTIAL_EXIT_SUBMITTED and locked["state"] == states.STOP_ACTIVE:
        states.validate_transition(locked["state"], states.TARGET_1_ACTIVE)
        locked["state"] = states.TARGET_1_ACTIVE
        locked["state_history"].append(
            {"state": states.TARGET_1_ACTIVE, "at": _now_iso(), "reason": "target_1 price reached"}
        )
    states.validate_transition(locked["state"], target_state)
    locked["state"] = target_state
    locked["client_order_id"] = client_order_id
    locked["state_history"].append({"state": target_state, "at": _now_iso(), "reason": note})


def _apply_exit_fill_progress(conn, locked, intent, fill_state, confirmed_qty, confirmed_price,
                               *, reason, on_fully_filled_state):
    """Phase C's core: apply whatever Phase B learned about the intent's
    broker order to the locked position record, keyed off the
    *cumulative* confirmed_filled_qty already recorded on the intent so a
    repeated observation of the same broker event never double-applies
    PnL (CODEX-023's "filled 이벤트 반복 → 중복 PnL 반영 없음" /
    CODEX-027's monotonic-fill discipline, reused here for exits)."""
    already_applied = intent["confirmed_filled_qty"] or 0
    requested_qty = intent["requested_qty"]

    if fill_state == order_status.FILL_STATE_FILLED:
        target_cumulative = requested_qty
    elif fill_state == order_status.FILL_STATE_PARTIALLY_FILLED:
        target_cumulative = confirmed_qty if confirmed_qty is not None else already_applied
    else:
        target_cumulative = already_applied  # NOT_FILLED / UNKNOWN -- no quantity change

    # CODEX-027 discipline applied to the exit path too: a cumulative fill
    # observation must never regress below what's already been applied,
    # and never exceed what this intent actually requested. Silently
    # clamping either direction would be exactly the kind of quiet
    # data-quality bug CODEX-027 exists to make impossible.
    if target_cumulative < already_applied:
        raise fill_validation.InvalidFillError(
            f"exit fill regressed from {already_applied!r} to {target_cumulative!r} "
            f"for intent {intent['intent_id']!r}"
        )
    if target_cumulative > requested_qty:
        raise fill_validation.InvalidFillError(
            f"exit fill {target_cumulative!r} exceeds intent's requested_qty {requested_qty!r} "
            f"for intent {intent['intent_id']!r}"
        )

    delta = target_cumulative - already_applied
    if delta > 0:
        fill_validation.validate_exit_qty(locked["remaining_qty"], delta)
        price = confirmed_price if confirmed_price is not None else (
            locked.get("target_1_price") if on_fully_filled_state == states.PARTIAL_EXITED else locked["stop_price"]
        )
        fill_validation.validate_fill_price(price)
        _add_realized_pnl(locked, delta, price)
        locked["remaining_qty"] = max(0, locked["remaining_qty"] - delta)

    # CODEX-028: commit=False -- these exit_intents mutations must land in
    # the SAME SQLite transaction as this call's position/position_events
    # write, which is only true because _apply_exit_fill_progress() always
    # runs inside a store.locked_position(conn=conn) block sharing this
    # exact `conn`. store.locked_position()'s __exit__ is the single commit
    # point for both, closing the gap CODEX-028 exists to fix (a fill
    # progress observation committed to exit_intents with no matching
    # position update ever landing, or vice versa).
    if fill_state == order_status.FILL_STATE_FILLED:
        eil.mark_confirmed(conn, intent["intent_id"], confirmed_filled_qty=target_cumulative, commit=False)
        states.validate_transition(locked["state"], on_fully_filled_state)
        locked["state"] = on_fully_filled_state
        if on_fully_filled_state == states.CLOSED:
            locked["exit_reason"] = reason
        locked["state_history"].append(
            {"state": on_fully_filled_state, "at": _now_iso(), "reason": f"{reason}, fill confirmed"}
        )
    elif fill_state == order_status.FILL_STATE_PARTIALLY_FILLED:
        if delta > 0:
            eil.update_progress(conn, intent["intent_id"], confirmed_filled_qty=target_cumulative, commit=False)
    elif fill_state == order_status.FILL_STATE_NOT_FILLED:
        pass  # accepted/new/etc. -- genuinely nothing to do yet
    else:  # UNKNOWN -- an order status we don't recognize; fail closed
        eil.mark_reconciliation_required(conn, intent["intent_id"], commit=False)
        states.validate_transition(locked["state"], states.MANUAL_REVIEW)
        locked["state"] = states.MANUAL_REVIEW
        locked["state_history"].append(
            {"state": states.MANUAL_REVIEW, "at": _now_iso(),
             "reason": f"{reason} exit: unrecognized broker order status {confirmed_price!r}"}
        )


def reconcile_pending_exit(position_id, *, broker=None, on_fully_filled_state=None,
                            lock_timeout=store.LOCK_TIMEOUT_SECONDS, db_conn=None):
    """Resolve an already-reserved (RESERVED/SUBMITTED/SUBMISSION_UNKNOWN/
    RECONCILIATION_REQUIRED) exit intent for `position_id` against the
    broker's current view of that order -- NEVER submits a new order.
    Returns the (possibly unchanged) position record, or None if there is
    no active exit intent to reconcile.

    This is the function restart recovery and a retried check_and_manage()
    call both end up funneling through for a position that already has a
    pending exit -- see _execute_exit()'s RECONCILE branch, which is a
    thin wrapper around this.
    """
    conn = db_conn or _open_exit_db()
    intent = eil.get_active_intent(conn, position_id)
    if intent is None:
        return None
    reason = intent["reason"]
    fully_filled_state = on_fully_filled_state or (
        states.PARTIAL_EXITED if reason == "PARTIAL_TARGET_1" else states.CLOSED
    )

    if broker is None:
        return store.load_position(position_id)  # nothing to reconcile against

    try:
        broker_order = broker.get_order_by_client_order_id(intent["client_order_id"])
    except Exception:
        # CODEX-024: a broker lookup failure must never trigger a resubmission.
        return store.load_position(position_id)

    if broker_order is None:
        # The broker has never heard of this client_order_id -- most
        # likely a crash happened before the order was ever actually
        # sent. Policy: flag for operator reconciliation, never
        # auto-resubmit. No position write is being made alongside this
        # (the function returns immediately after), so this commits on
        # its own -- there is nothing to keep it atomic with.
        eil.mark_reconciliation_required(conn, intent["intent_id"])
        return store.load_position(position_id)

    order_info = order_status.extract_order_info(broker_order)
    fill_state = order_status.classify_broker_order_status(order_info.get("status"))

    with store.locked_position(position_id, lock_timeout=lock_timeout, conn=conn) as locked:
        current_intent = eil.get_by_id(conn, intent["intent_id"])
        if current_intent is None or current_intent["state"] in eil.TERMINAL_STATES:
            return dict(locked)  # already resolved by a concurrent/prior call
        if locked["state"] not in (states.EXIT_SUBMITTED, states.PARTIAL_EXIT_SUBMITTED):
            _transition_to_submitted(
                locked, fully_filled_state, intent["client_order_id"],
                f"restart recovery: resuming pending exit intent {intent['intent_id']}",
            )
        _apply_exit_fill_progress(
            conn, locked, current_intent, fill_state,
            order_info.get("filled_qty"), order_info.get("filled_avg_price"),
            reason=reason, on_fully_filled_state=fully_filled_state,
        )
    return store.load_position(position_id)


def _execute_exit(position_id, symbol, order_date, broker, reason, qty_selector, lock_timeout,
                   *, on_fully_filled_state, db_conn=None):
    """Durable, duplicate-safe exit submission -- see module section
    docstring above for the full three-phase design."""
    conn = db_conn or _open_exit_db()

    # ---- Phase A: find-or-reserve a durable intent, durably persisted
    # before any broker call. ----
    # `existing_intent` is read WITHOUT holding the position lock -- two
    # concurrent callers can both see None (both racing into the "fresh
    # reservation" branch below, safely resolved there since that branch
    # re-checks reachability AFTER acquiring the lock) or one can see the
    # other's already-reserved intent here. Only the second case is
    # handled in this branch, and the position's CURRENT state (re-read
    # fresh under the lock just below, not this stale existing_intent
    # snapshot) is the only thing ever trusted to decide what to do next.
    existing_intent = eil.get_active_intent(conn, position_id)
    if existing_intent is not None:
        # A prior attempt already reserved (and maybe submitted, maybe by
        # now even fully resolved) an exit for this position -- crash/
        # timeout recovery or a same-process race, not a fresh decision.
        with store.locked_position(position_id, lock_timeout=lock_timeout, conn=conn) as locked:
            if locked["state"] in (states.EXIT_SUBMITTED, states.PARTIAL_EXIT_SUBMITTED):
                pass  # already reflects the pending exit -- nothing to catch up
            elif _exit_states_reachable_from(locked["state"]):
                fully_filled_state = on_fully_filled_state or (
                    states.PARTIAL_EXITED if existing_intent["reason"] == "PARTIAL_TARGET_1" else states.CLOSED
                )
                _transition_to_submitted(
                    locked, fully_filled_state, existing_intent["client_order_id"],
                    f"resuming pending exit intent {existing_intent['intent_id']} instead of resubmitting",
                )
            else:
                # The position has already been resolved (e.g. a
                # concurrent caller finished Phase C, including a full
                # CLOSED, while this thread's stale `existing_intent` read
                # was still in flight) -- there is nothing left to catch
                # up or reconcile, and forcing a transition here would be
                # illegal (e.g. CLOSED -> EXIT_SUBMITTED).
                return dict(locked)
        return reconcile_pending_exit(
            position_id, broker=broker, on_fully_filled_state=on_fully_filled_state,
            lock_timeout=lock_timeout, db_conn=conn,
        )

    intent_id = None
    client_order_id = None
    exit_qty = None
    with store.locked_position(position_id, lock_timeout=lock_timeout, conn=conn) as locked:
        if not _exit_states_reachable_from(locked["state"]):
            return dict(locked)  # already handled by a concurrent/prior call
        exit_qty = qty_selector(locked)
        if exit_qty <= 0:
            if on_fully_filled_state == states.CLOSED:
                states.validate_transition(locked["state"], states.CLOSED)
                locked["state"] = states.CLOSED
                locked["exit_reason"] = reason
                locked["state_history"].append(
                    {"state": states.CLOSED, "at": _now_iso(), "reason": f"{reason} (nothing left to exit)"}
                )
            return dict(locked)
        client_order_id = f"exit-{symbol}-{order_date}-{uuid.uuid4().hex[:10]}"
        try:
            # CODEX-028: commit=False -- this reservation must land in the
            # same SQLite transaction as the position's own transition to
            # its submitted state below, both committed together when this
            # `with` block exits (store.locked_position(conn=conn)'s single
            # commit point). Durable before the broker is ever called, and
            # genuinely atomic with the position write this time.
            intent_id = eil.reserve(conn, position_id, reason, exit_qty, client_order_id, commit=False)
        except Exception:
            # Could not durably record the intent -- refuse to call the broker.
            return dict(locked)
        _transition_to_submitted(
            locked, on_fully_filled_state, client_order_id,
            f"{reason}, exit intent {intent_id} reserved, exiting {exit_qty}",
        )

    # (Phase A's write is now durable on disk -- position and exit intent both,
    # BEFORE the broker is ever called.)

    # ---- Phase B: the actual broker call. ----
    try:
        response = paper_strategy_order.submit_order(
            symbol, qty=exit_qty, broker=broker, client_order_id=client_order_id, side="sell",
        )
    except Exception:
        eil.mark_submission_unknown(conn, intent_id)
        return store.load_position(position_id)

    if response.status_code not in (200, 201):
        # Broker explicitly rejected the order -- it never reached an
        # in-flight state at all, so the intent is aborted outright, not
        # left pending for reconciliation.
        #
        # CODEX-032: mark_aborted() and the position's MANUAL_REVIEW
        # transition must commit together, in the SAME SQLite transaction
        # -- previously mark_aborted() committed on its own (default
        # commit=True) before the position write's own separate
        # transaction even began, so a failure in that second write left
        # a permanently inconsistent pair: exit_intents shows a terminal
        # ABORTED intent (nothing left to reconcile) while the position
        # stays stuck in EXIT_SUBMITTED forever -- invisible to both
        # reconcile_pending_exit() (no active intent to find) and
        # recover_on_restart() (which only routes through
        # reconcile_pending_exit() for positions with an active intent).
        # commit=False here defers the actual commit to
        # store.locked_position(conn=conn)'s single commit point below,
        # exactly like Phase A's eil.reserve(..., commit=False) already
        # does for the reservation side.
        with store.locked_position(position_id, lock_timeout=lock_timeout, conn=conn) as locked:
            eil.mark_aborted(conn, intent_id, commit=False)
            states.validate_transition(locked["state"], states.MANUAL_REVIEW)
            locked["state"] = states.MANUAL_REVIEW
            locked["state_history"].append(
                {"state": states.MANUAL_REVIEW, "at": _now_iso(),
                 "reason": f"{reason} exit broker rejection, status_code={response.status_code}"}
            )
        return store.load_position(position_id)

    order_info = order_status.extract_order_info(response)
    eil.mark_submitted(conn, intent_id, broker_order_id=order_info.get("order_id"))
    fill_state = order_status.classify_broker_order_status(order_info.get("status"))

    # ---- Phase C: apply whatever Phase B learned. ----
    with store.locked_position(position_id, lock_timeout=lock_timeout, conn=conn) as locked:
        current_intent = eil.get_by_id(conn, intent_id)
        _apply_exit_fill_progress(
            conn, locked, current_intent, fill_state,
            order_info.get("filled_qty"), order_info.get("filled_avg_price"),
            reason=reason, on_fully_filled_state=on_fully_filled_state,
        )
    return store.load_position(position_id)


def _exit_states_reachable_from(current_state):
    """States check_and_manage() is willing to act from -- anything with an
    active stop/target, mid-partial-exit-cycle, or already trailing. Not
    ENTRY_SUBMITTED/PARTIALLY_FILLED (no fill yet to exit) and not any
    terminal/exception state."""
    return current_state in (
        states.STOP_ACTIVE, states.TARGET_1_ACTIVE, states.PARTIAL_EXITED, states.TRAILING,
    )


ACTION_NONE = "none"
ACTION_FULL_EXIT = "full_exit"
ACTION_PARTIAL_EXIT = "partial_exit"
ACTION_TRAIL = "trail"


@dataclass(frozen=True)
class ExitDecision:
    """What check_and_manage() would DO for a position, separated from
    doing it (CODEX-049).

    Shadow Mode has to answer "would this position have been exited, and
    why?" without submitting anything. Re-implementing the rules in the
    Shadow service would be a second, divergent copy of the exit policy --
    exactly the kind of duplicate safety-critical logic this project
    forbids. So the decision is extracted here as a pure function and
    check_and_manage() dispatches on its result: live execution and
    Shadow evaluation are guaranteed to agree because they are literally
    the same code."""

    action: str
    reason: Optional[str] = None
    detail: Optional[str] = None


def decide_exit(record, *, current_price, now, enable_partial_profit=True,
                 enable_trailing_stop=True, enable_time_stop=True, enable_eod_exit=True):
    """Pure: no I/O, no state change, no broker. Priority order is
    unchanged from check_and_manage()'s documented order -- EOD >
    time-stop > stop-loss > target_2 > target_1 -- and the four CODEX-046
    feature flags gate exactly the same branches they always did."""
    if not _exit_states_reachable_from(record["state"]):
        return ExitDecision(ACTION_NONE, reason="state is not exit-eligible")

    eod_cutoff = combine_eastern(now.date(), MARKET_REGULAR_END) - timedelta(
        minutes=cfg.EOD_FORCE_CLOSE_MINUTES_BEFORE_CLOSE
    )
    if enable_eod_exit and now >= eod_cutoff:
        return ExitDecision(ACTION_FULL_EXIT, reason="EOD_FORCED_CLOSE")

    entry_time = record.get("entry_time")
    if enable_time_stop and entry_time:
        held_minutes = (now - datetime.fromisoformat(entry_time)).total_seconds() / 60.0
        if held_minutes >= cfg.MAX_POSITION_HOLD_MINUTES:
            return ExitDecision(
                ACTION_FULL_EXIT, reason="TIME_STOP", detail=f"held_minutes={held_minutes:.1f}",
            )

    if current_price <= record["stop_price"]:
        return ExitDecision(
            ACTION_FULL_EXIT, reason="STOP_LOSS",
            detail=f"price={current_price} stop={record['stop_price']}",
        )

    if record["state"] == states.STOP_ACTIVE and current_price >= record["target_1_price"]:
        if enable_partial_profit:
            return ExitDecision(ACTION_PARTIAL_EXIT, reason="TARGET_1")
        if current_price >= record["target_2_price"]:
            # Partial profit-taking is disabled -- never leave the
            # position waiting in STOP_ACTIVE for a partial-exit branch
            # that will never fire once target_2 is already reached.
            return ExitDecision(ACTION_FULL_EXIT, reason="TARGET_2")

    if record["state"] in (states.TARGET_1_ACTIVE, states.PARTIAL_EXITED, states.TRAILING):
        if current_price >= record["target_2_price"]:
            return ExitDecision(ACTION_FULL_EXIT, reason="TARGET_2")
        if record["state"] == states.PARTIAL_EXITED and enable_trailing_stop:
            return ExitDecision(ACTION_TRAIL, reason="TRAILING_BREAKEVEN")

    return ExitDecision(ACTION_NONE)


def check_and_manage(position_id, *, current_price, bars=None, now=None, clock=None, broker=None,
                      order_date=None, lock_timeout=store.LOCK_TIMEOUT_SECONDS,
                      enable_partial_profit=True, enable_trailing_stop=True,
                      enable_time_stop=True, enable_eod_exit=True):
    """The core "tick" function: given the current price (and optionally
    fresh bars for an invalidation check), decide whether this position
    needs a partial exit, a full exit (target_2/invalidation/time-stop/
    EOD), and submit it if so. Idempotent and safe to call repeatedly --
    a position with nothing to do just returns unchanged (no broker call).

    Priority order (highest first): EOD forced close > time-stop >
    strategy invalidation > target_2 (full exit) > target_1 (partial
    exit) > stop-loss. EOD/time-stop/invalidation/stop-loss all force a
    FULL exit of remaining_qty regardless of which target stage the
    position is in -- they are safety overrides, not another target level.

    CODEX-046: `enable_partial_profit`/`enable_trailing_stop`/
    `enable_time_stop`/`enable_eod_exit` all default to True so every
    EXISTING caller (the Alpaca/paper live-pilot path, and every test
    that doesn't pass them) keeps today's unmodified behavior -- these
    are an opt-OUT a caller (kis_position_manager.py, from
    config/live_exit_flags.py) can set False, never a change to the
    default policy itself. Stop-loss and full take-profit at target_2
    are NOT gated by any flag -- they stay unconditionally active
    regardless of these four. When enable_partial_profit is False, a
    position at target_1 skips the partial exit entirely and instead
    takes a FULL exit the moment price reaches target_2 (never left to
    wait forever in STOP_ACTIVE for a partial-exit path that will never
    fire).

    CODEX-030: `now`, if supplied, must be an explicit timezone-aware
    Eastern-zoned moment (a naive datetime is rejected, not silently
    assumed to already be Eastern -- the ambiguity that let tests
    accidentally depend on wall-clock time is exactly what this guards
    against). Without `now`, `clock` (default: clock.DEFAULT_CLOCK, the
    real wall clock) supplies it via clock.now_eastern() -- production
    behavior is unchanged; tests should pass a clock.FrozenClock (or an
    explicit `now`) fixed to an unambiguous mid-session moment instead of
    relying on whatever time the suite happens to run.
    """
    if now is not None and now.tzinfo is None:
        raise PositionLifecycleError(
            "check_and_manage(now=...) must be timezone-aware; a naive datetime is ambiguous"
        )
    clock = clock or DEFAULT_CLOCK
    now = now if now is not None else clock.now_eastern()
    record = store.load_position(position_id)
    if record is None:
        raise PositionLifecycleError(f"No such position: {position_id!r}")
    if not _exit_states_reachable_from(record["state"]):
        return record  # nothing to manage yet, or already past managing

    symbol = record["symbol"]
    order_date = order_date or now.strftime("%Y-%m-%d")

    decision = decide_exit(
        record, current_price=current_price, now=now,
        enable_partial_profit=enable_partial_profit, enable_trailing_stop=enable_trailing_stop,
        enable_time_stop=enable_time_stop, enable_eod_exit=enable_eod_exit,
    )

    if decision.action == ACTION_FULL_EXIT:
        return _force_full_exit(position_id, symbol, order_date, broker, decision.reason, lock_timeout)
    if decision.action == ACTION_PARTIAL_EXIT:
        return _partial_exit_at_target_1(position_id, symbol, order_date, broker, lock_timeout)
    if decision.action == ACTION_TRAIL:
        # ASSUMPTION (DECISION_LOG.md): minimal trailing rule -- once
        # target_1 fills, move the stop to breakeven (entry price) and
        # enter TRAILING. This is a deliberately simple initial policy,
        # not a full trailing-stop algorithm.
        with store.locked_position(position_id, lock_timeout=lock_timeout) as locked:
            if locked["state"] == states.PARTIAL_EXITED:
                locked["stop_price"] = locked["average_fill_price"]
                states.validate_transition(locked["state"], states.TRAILING)
                locked["state"] = states.TRAILING
                locked["state_history"].append(
                    {"state": states.TRAILING, "at": _now_iso(),
                     "reason": "target_1 filled, stop moved to breakeven"}
                )
        return store.load_position(position_id)

    return record


def check_invalidation(position_id, strategy, bars, *, order_date=None, now=None, clock=None,
                        broker=None, lock_timeout=store.LOCK_TIMEOUT_SECONDS):
    """Separate from check_and_manage() because invalidation needs fresh
    bar data and a strategy instance, whereas check_and_manage() only
    needs a price -- callers that don't have new bars this tick can skip
    this check entirely and still get the price-based exits.

    CODEX-030: same Clock injection as check_and_manage() -- see its
    docstring for the naive-datetime rejection and clock/now precedence.
    """
    record = store.load_position(position_id)
    if record is None or not _exit_states_reachable_from(record["state"]):
        return record
    if not strategy.invalidate(bars, symbol=record["symbol"]):
        return record
    if now is not None and now.tzinfo is None:
        raise PositionLifecycleError(
            "check_invalidation(now=...) must be timezone-aware; a naive datetime is ambiguous"
        )
    clock = clock or DEFAULT_CLOCK
    now = now if now is not None else clock.now_eastern()
    order_date = order_date or now.strftime("%Y-%m-%d")
    return _force_full_exit(position_id, record["symbol"], order_date, broker,
                             "STRATEGY_INVALIDATION", lock_timeout)


def _partial_exit_at_target_1(position_id, symbol, order_date, broker, lock_timeout):
    def _qty_selector(locked):
        fraction = cfg.PARTIAL_EXIT_FRACTION_AT_TARGET_1
        return int(locked["remaining_qty"] * fraction)

    return _execute_exit(
        position_id, symbol, order_date, broker, "PARTIAL_TARGET_1", _qty_selector, lock_timeout,
        on_fully_filled_state=states.PARTIAL_EXITED,
    )


def _force_full_exit(position_id, symbol, order_date, broker, reason, lock_timeout):
    def _qty_selector(locked):
        return locked["remaining_qty"]

    return _execute_exit(
        position_id, symbol, order_date, broker, reason, _qty_selector, lock_timeout,
        on_fully_filled_state=states.CLOSED,
    )


# ---------------------------------------------------------------------------
# Restart recovery
# ---------------------------------------------------------------------------

RECOVERY_STATUS_OK = "OK"
RECOVERY_STATUS_STORE_UNAVAILABLE = "STORE_UNAVAILABLE"


@dataclass
class RestartRecoveryResult:
    """CODEX-025: recover_on_restart()'s return type. Deliberately not a
    bare list -- a bare `[]` is structurally identical whether it means
    "no open positions" or "the store is corrupted and we can't tell,"
    which is exactly the fail-open bug this type exists to make
    impossible to reintroduce by accident at any call site."""
    status: str
    positions: List[dict] = field(default_factory=list)
    reason: Optional[str] = None
    broker_positions: Optional[list] = None


def _escalate_kill_switch_for_store_failure(reason):
    """Best-effort escalation to MANUAL_REVIEW when the position store
    itself is unavailable -- a corrupted store might be hiding a live,
    unmanaged position, so entries must stop until a human has reconciled
    against the broker's own full position list. If the kill switch
    escalation call itself fails (e.g. its own lock times out), that
    failure is swallowed here rather than raised -- the caller's
    RESTART_STATUS_STORE_UNAVAILABLE result is what actually blocks new
    entries (via store.create_position() already refusing to write into a
    corrupted file), so a failed escalation must not prevent
    recover_on_restart() from at least reporting the store failure."""
    try:
        import kill_switch_state
        kill_switch_state.activate(
            kill_switch_state.MANUAL_REVIEW,
            reason=f"position store unavailable on restart: {reason}",
            activated_by="system:recover_on_restart",
        )
    except Exception:
        pass


def recover_on_restart(*, broker=None, lock_timeout=store.LOCK_TIMEOUT_SECONDS):
    """Scan every non-terminal position and reconcile it against the
    broker before anything else touches it. A position already in
    RECOVERY_REQUIRED (e.g. a corrupted record) is left exactly as-is --
    it already fails closed and this function must never "fix" it by
    guessing. Reconciliation failure/inconsistency (broker lookup raises,
    or returns nothing usable) also lands in RECOVERY_REQUIRED, never
    silently resumed as if the process had never restarted.

    CODEX-025: if the *entire* store is unreadable/corrupted
    (store.PositionStoreCorruptedError), this returns
    RestartRecoveryResult(status=STORE_UNAVAILABLE, positions=[]) -- never
    a bare empty result indistinguishable from "no positions ever
    existed" -- and escalates the kill switch to MANUAL_REVIEW so new
    entries stop until an operator has reconciled the broker's actual
    position list by hand. The corrupted file itself is never touched
    (no auto-reinitialization) so it remains available for manual
    recovery/forensics.
    """
    try:
        non_terminal = store.load_non_terminal()
    except store.PositionStoreCorruptedError as exc:
        _escalate_kill_switch_for_store_failure(str(exc))
        # Best-effort: pull the broker's own full position list so an
        # operator has something concrete to reconcile the corrupted local
        # store against. A failure here must not hide the store failure
        # itself -- broker_positions simply stays None.
        broker_positions = None
        if broker is not None:
            try:
                broker_positions = broker.get_positions()
            except Exception:
                broker_positions = None
        return RestartRecoveryResult(
            status=RECOVERY_STATUS_STORE_UNAVAILABLE, positions=[], reason=str(exc),
            broker_positions=broker_positions,
        )

    exit_conn = _open_exit_db()
    results = []
    for position_id, record in non_terminal.items():
        if record["state"] == states.RECOVERY_REQUIRED:
            results.append(record)
            continue

        # CODEX-024: a position with a pending exit intent (or already
        # sitting in EXIT_SUBMITTED/PARTIAL_EXIT_SUBMITTED from a prior
        # process) is reconciled through the same broker-lookup-by-
        # client_order_id path _execute_exit() itself uses on retry --
        # never through the generic entry-reconciliation branch below,
        # and never by resubmitting.
        if eil.get_active_intent(exit_conn, position_id) is not None or record["state"] in (
            states.EXIT_SUBMITTED, states.PARTIAL_EXIT_SUBMITTED,
        ):
            reconciled = reconcile_pending_exit(
                position_id, broker=broker, lock_timeout=lock_timeout, db_conn=exit_conn,
            )
            results.append(reconciled or store.load_position(position_id))
            continue

        with store.locked_position(position_id, lock_timeout=lock_timeout, conn=exit_conn) as locked:
            if locked["state"] == states.RECOVERY_REQUIRED:
                results.append(dict(locked))
                continue
            client_order_id = locked.get("client_order_id")
            broker_order = None
            lookup_failed = False
            if broker is not None and client_order_id:
                try:
                    broker_order = broker.get_order_by_client_order_id(client_order_id)
                except Exception:
                    lookup_failed = True
            if broker is None or client_order_id is None or lookup_failed or broker_order is None:
                states.validate_transition(locked["state"], states.RECOVERY_REQUIRED)
                locked["state"] = states.RECOVERY_REQUIRED
                locked["state_history"].append(
                    {"state": states.RECOVERY_REQUIRED, "at": _now_iso(),
                     "reason": "restart recovery: broker reconciliation inconclusive"}
                )
            else:
                locked["last_reconciled_at"] = _now_iso()
                locked["state_history"].append(
                    {"state": locked["state"], "at": _now_iso(),
                     "reason": "restart recovery: broker reconciliation confirmed current state"}
                )
            results.append(dict(locked))
    return RestartRecoveryResult(status=RECOVERY_STATUS_OK, positions=results, reason=None)
