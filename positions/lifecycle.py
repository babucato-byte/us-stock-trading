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

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import paper_strategy_order
from config import scalping_strategy_v1_config as cfg
from market_hours import MARKET_REGULAR_END, combine_eastern, eastern_now
from positions import fill_validation, states, store
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
# Exit submission (shared by every exit path below)
# ---------------------------------------------------------------------------

def _submit_exit_order(symbol, qty, *, order_date, broker):
    """Submit a real "sell" order through the same safety-gated path as
    entries, WITHOUT going through try_reserve_order() (see module
    docstring for why). client_order_id is exit-specific so it never
    collides with the entry's own reservation."""
    import uuid
    client_order_id = f"exit-{symbol}-{order_date}-{uuid.uuid4().hex[:10]}"
    response = paper_strategy_order.submit_order(
        symbol, qty=qty, broker=broker, client_order_id=client_order_id, side="sell",
    )
    return response, client_order_id


def _exit_states_reachable_from(current_state):
    """States check_and_manage() is willing to act from -- anything with an
    active stop/target, mid-partial-exit-cycle, or already trailing. Not
    ENTRY_SUBMITTED/PARTIALLY_FILLED (no fill yet to exit) and not any
    terminal/exception state."""
    return current_state in (
        states.STOP_ACTIVE, states.TARGET_1_ACTIVE, states.PARTIAL_EXITED, states.TRAILING,
    )


def check_and_manage(position_id, *, current_price, bars=None, now=None, broker=None,
                      order_date=None, lock_timeout=store.LOCK_TIMEOUT_SECONDS):
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
    """
    now = now or eastern_now()
    record = store.load_position(position_id)
    if record is None:
        raise PositionLifecycleError(f"No such position: {position_id!r}")
    if not _exit_states_reachable_from(record["state"]):
        return record  # nothing to manage yet, or already past managing

    symbol = record["symbol"]
    order_date = order_date or now.strftime("%Y-%m-%d")

    eod_cutoff = combine_eastern(now.date(), MARKET_REGULAR_END) - timedelta(
        minutes=cfg.EOD_FORCE_CLOSE_MINUTES_BEFORE_CLOSE
    )
    if now >= eod_cutoff:
        return _force_full_exit(position_id, symbol, order_date, broker, "EOD_FORCED_CLOSE", lock_timeout)

    entry_time = record.get("entry_time")
    if entry_time:
        held_minutes = (now - datetime.fromisoformat(entry_time)).total_seconds() / 60.0
        if held_minutes >= cfg.MAX_POSITION_HOLD_MINUTES:
            return _force_full_exit(position_id, symbol, order_date, broker, "TIME_STOP", lock_timeout)

    if bars is not None:
        strategy_cls = None  # invalidation is optional and caller-provided; see check_and_manage_with_strategy
    if current_price <= record["stop_price"]:
        return _force_full_exit(position_id, symbol, order_date, broker, "STOP_LOSS", lock_timeout)

    if record["state"] == states.STOP_ACTIVE and current_price >= record["target_1_price"]:
        return _partial_exit_at_target_1(position_id, symbol, order_date, broker, lock_timeout)

    if record["state"] in (states.TARGET_1_ACTIVE, states.PARTIAL_EXITED, states.TRAILING):
        if current_price >= record["target_2_price"]:
            return _force_full_exit(position_id, symbol, order_date, broker, "TARGET_2", lock_timeout)
        if record["state"] == states.PARTIAL_EXITED:
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


def check_invalidation(position_id, strategy, bars, *, order_date=None, broker=None,
                        lock_timeout=store.LOCK_TIMEOUT_SECONDS):
    """Separate from check_and_manage() because invalidation needs fresh
    bar data and a strategy instance, whereas check_and_manage() only
    needs a price -- callers that don't have new bars this tick can skip
    this check entirely and still get the price-based exits."""
    record = store.load_position(position_id)
    if record is None or not _exit_states_reachable_from(record["state"]):
        return record
    if not strategy.invalidate(bars, symbol=record["symbol"]):
        return record
    now = eastern_now()
    order_date = order_date or now.strftime("%Y-%m-%d")
    return _force_full_exit(position_id, record["symbol"], order_date, broker,
                             "STRATEGY_INVALIDATION", lock_timeout)


def _partial_exit_at_target_1(position_id, symbol, order_date, broker, lock_timeout):
    with store.locked_position(position_id, lock_timeout=lock_timeout) as locked:
        if locked["state"] != states.STOP_ACTIVE:
            return  # someone else already handled this (duplicate-exit prevention)
        # STOP_ACTIVE -> TARGET_1_ACTIVE -> PARTIAL_EXIT_SUBMITTED, per TRANSITIONS
        # (no direct STOP_ACTIVE -> PARTIAL_EXIT_SUBMITTED edge).
        states.validate_transition(locked["state"], states.TARGET_1_ACTIVE)
        locked["state"] = states.TARGET_1_ACTIVE
        locked["state_history"].append(
            {"state": states.TARGET_1_ACTIVE, "at": _now_iso(), "reason": "target_1 price reached"}
        )
        fraction = cfg.PARTIAL_EXIT_FRACTION_AT_TARGET_1
        exit_qty = int(locked["remaining_qty"] * fraction)
        if exit_qty <= 0:
            return
        states.validate_transition(locked["state"], states.PARTIAL_EXIT_SUBMITTED)
        locked["state"] = states.PARTIAL_EXIT_SUBMITTED
        locked["state_history"].append(
            {"state": states.PARTIAL_EXIT_SUBMITTED, "at": _now_iso(),
             "reason": f"target_1 reached, exiting {exit_qty}"}
        )
        response, _ = _submit_exit_order(symbol, exit_qty, order_date=order_date, broker=broker)
        if response.status_code in (200, 201):
            _add_realized_pnl(locked, exit_qty, locked["target_1_price"])
            locked["remaining_qty"] -= exit_qty
            states.validate_transition(locked["state"], states.PARTIAL_EXITED)
            locked["state"] = states.PARTIAL_EXITED
            locked["state_history"].append(
                {"state": states.PARTIAL_EXITED, "at": _now_iso(),
                 "reason": f"partial exit filled, remaining_qty={locked['remaining_qty']}"}
            )
        else:
            states.validate_transition(locked["state"], states.MANUAL_REVIEW)
            locked["state"] = states.MANUAL_REVIEW
            locked["state_history"].append(
                {"state": states.MANUAL_REVIEW, "at": _now_iso(),
                 "reason": f"partial exit broker rejection, status_code={response.status_code}"}
            )
    return store.load_position(position_id)


def _force_full_exit(position_id, symbol, order_date, broker, reason, lock_timeout):
    with store.locked_position(position_id, lock_timeout=lock_timeout) as locked:
        if not _exit_states_reachable_from(locked["state"]):
            return  # already exited or being exited by another caller
        exit_qty = locked["remaining_qty"]
        if exit_qty <= 0:
            states.validate_transition(locked["state"], states.CLOSED)
            locked["state"] = states.CLOSED
            locked["exit_reason"] = reason
            locked["state_history"].append(
                {"state": states.CLOSED, "at": _now_iso(), "reason": f"{reason} (nothing left to exit)"}
            )
            return
        states.validate_transition(locked["state"], states.EXIT_SUBMITTED)
        locked["state"] = states.EXIT_SUBMITTED
        locked["state_history"].append(
            {"state": states.EXIT_SUBMITTED, "at": _now_iso(), "reason": f"{reason}, exiting {exit_qty}"}
        )
        response, _ = _submit_exit_order(symbol, exit_qty, order_date=order_date, broker=broker)
        if response.status_code in (200, 201):
            exit_price = (response.data or {}).get("filled_avg_price") if isinstance(response.data, dict) else None
            exit_price = exit_price if exit_price is not None else locked["stop_price"]
            _add_realized_pnl(locked, exit_qty, exit_price)
            locked["remaining_qty"] = 0
            locked["exit_reason"] = reason
            states.validate_transition(locked["state"], states.CLOSED)
            locked["state"] = states.CLOSED
            locked["state_history"].append(
                {"state": states.CLOSED, "at": _now_iso(), "reason": f"{reason}, fill confirmed"}
            )
        else:
            states.validate_transition(locked["state"], states.MANUAL_REVIEW)
            locked["state"] = states.MANUAL_REVIEW
            locked["state_history"].append(
                {"state": states.MANUAL_REVIEW, "at": _now_iso(),
                 "reason": f"{reason} exit broker rejection, status_code={response.status_code}"}
            )
    return store.load_position(position_id)


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

    results = []
    for position_id, record in non_terminal.items():
        if record["state"] == states.RECOVERY_REQUIRED:
            results.append(record)
            continue
        with store.locked_position(position_id, lock_timeout=lock_timeout) as locked:
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
