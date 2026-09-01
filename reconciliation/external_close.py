"""Retire a canonical position the broker no longer holds.

The gap this fills
------------------
On 2026-08-31 the operator sold the remaining S1 holding (TX) by hand,
outside the system. The broker went flat -- 0 positions, 0 open orders --
and the canonical row stayed `OPEN qty 1` for hours.

Nothing existing could close it. `sync_fills` ADOPTS positions the broker
reports; it has no opinion about one the broker has stopped reporting,
deliberately, because "the broker did not mention it" is also what a
failed read looks like. `ownership.release_misattributed` is for a row
that belongs to a different strategy, which this is not.

So the divergence simply persisted, and a phantom position is not
harmless: the S1 watchdog counted it as a held position and armed itself
on it, which is how a strategy holding nothing can still reach for a
kill switch.

Absence is only evidence when everything else agrees
----------------------------------------------------
A missing broker position on its own means very little. It is equally
consistent with a fill that has not landed yet, a submission still in
flight, a cancel mid-flight, or a read that returned a partial view. So
a row is retired only when every independent source agrees there is
nothing left to manage:

  * the broker reports no position in the symbol
  * the broker reports no open order in the symbol
  * the order ledger has no unresolved order for it
  * no exit intent is unresolved
  * the row itself has no exit in flight

Any one of those failing leaves the row exactly where it is. Refusing
costs a stale row that stays visible; retiring wrongly loses a real
position from the book while the shares still exist.

Nothing is invented
-------------------
No exit price, no realized PnL. The system did not sell these shares and
does not know what they fetched; writing a plausible number would put a
fabricated trade in the strategy's performance record. The terminal
state says what actually happened -- someone closed it elsewhere -- and
the evidence for that judgement is recorded alongside it.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: The terminal state for a row the broker stopped reporting.
#:
#: Deliberately not a trading outcome. A normal CLOSED would put a trade
#: in the strategy's realized record that it never made, and -- with no
#: exit price -- one that looks like a scratch.
EXTERNALLY_CLOSED = "EXTERNALLY_CLOSED"

#: Why a row was left alone. Each names the evidence that was missing,
#: because "not retired" alone cannot be acted on.
HELD_BROKER_POSITION = "BROKER_STILL_REPORTS_POSITION"
HELD_OPEN_ORDER = "BROKER_HAS_OPEN_ORDER"
HELD_UNRESOLVED_ORDER = "UNRESOLVED_ORDER_IN_LEDGER"
HELD_UNRESOLVED_INTENT = "UNRESOLVED_EXIT_INTENT"
HELD_EXIT_IN_FLIGHT = "EXIT_ALREADY_SUBMITTED"
HELD_BROKER_UNREADABLE = "BROKER_UNREADABLE"
#: A state from which external closure is not an honest conclusion:
#: either nothing was ever held (pre-fill), or a human is already
#: adjudicating it (MANUAL_REVIEW, RECOVERY_REQUIRED).
HELD_STATE_NOT_RETIRABLE = "STATE_NOT_EXTERNALLY_RETIRABLE"
RETIRED = "RETIRED_EXTERNALLY_CLOSED"


def _symbols_of(entries) -> set:
    found = set()
    for entry in entries or ():
        for key in ("symbol", "pdno", "PDNO", "ovrs_pdno"):
            value = (entry.get(key) if isinstance(entry, dict)
                     else getattr(entry, key, None))
            if value:
                found.add(str(value).upper())
    return found


def _unresolved_order_symbols(conn) -> set:
    try:
        rows = conn.execute(
            "SELECT symbol FROM kis_order_idempotency WHERE status NOT IN "
            "('FILLED', 'REJECTED', 'CANCELLED')").fetchall()
    except Exception:  # noqa: BLE001 - unreadable is not empty
        raise
    return {str((r["symbol"] if hasattr(r, "keys") else r[0]) or "").upper()
            for r in rows}


def _unresolved_intent_positions(conn) -> set:
    try:
        rows = conn.execute(
            "SELECT position_id FROM exit_intents WHERE state NOT IN "
            "('CONFIRMED', 'ABORTED')").fetchall()
    except Exception:  # noqa: BLE001
        raise
    return {str((r["position_id"] if hasattr(r, "keys") else r[0]) or "")
            for r in rows}


def evaluate(row, *, symbol, position_id, broker_positions, broker_orders,
             unresolved_orders, unresolved_intents) -> Optional[str]:
    """Why this row must be kept, or None if it may be retired."""
    if symbol in broker_positions:
        return HELD_BROKER_POSITION
    if symbol in broker_orders:
        return HELD_OPEN_ORDER
    if symbol in unresolved_orders:
        return HELD_UNRESOLVED_ORDER
    if position_id in unresolved_intents:
        return HELD_UNRESOLVED_INTENT
    if row.get("exit_submitted"):
        return HELD_EXIT_IN_FLIGHT
    return None


def retire_externally_closed(conn, broker, *, strategy_id, store,
                             now=None, apply=True) -> List[Dict[str, Any]]:
    """Retire this strategy's rows that the broker no longer holds.

    `apply=False` reports what it would do and changes nothing, which is
    how this should be run the first time against a live account.
    """
    current = now or datetime.now(timezone.utc)
    try:
        broker_positions = _symbols_of(broker.get_positions())
        broker_orders = _symbols_of(broker.get_open_orders())
    except Exception as exc:  # noqa: BLE001 - an unreadable broker is not
        # a flat one; refusing costs a stale row, guessing loses a real
        # position from the book while the shares still exist.
        logger.warning("broker unreadable; retiring nothing: %s", exc)
        return [{"outcome": HELD_BROKER_UNREADABLE, "detail": str(exc)[:200]}]

    try:
        unresolved_orders = _unresolved_order_symbols(conn)
        unresolved_intents = _unresolved_intent_positions(conn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("order state unreadable; retiring nothing: %s", exc)
        return [{"outcome": HELD_UNRESOLVED_ORDER, "detail": str(exc)[:200]}]

    outcomes = []
    for entry in store.load_live(conn) or ():
        row = entry[-1] if isinstance(entry, tuple) else entry
        position_id = (entry[0] if isinstance(entry, tuple)
                       else row.get("position_id"))
        symbol = str(row.get("symbol") or "").upper()

        kept = evaluate(row, symbol=symbol, position_id=position_id,
                        broker_positions=broker_positions,
                        broker_orders=broker_orders,
                        unresolved_orders=unresolved_orders,
                        unresolved_intents=unresolved_intents)

        record = {
            "strategy_id": strategy_id,
            "symbol": symbol,
            "position_id": position_id,
            "previous_status": row.get("status"),
            "previous_quantity": row.get("quantity"),
            "reconciled_at": current.isoformat(),
            # The evidence the judgement rested on, so it can be argued
            # with afterwards rather than taken on trust.
            "broker_positions_seen": sorted(broker_positions),
            "broker_open_orders_seen": sorted(broker_orders),
            "source": EXTERNALLY_CLOSED,
        }
        if kept is not None:
            record["outcome"] = kept
            outcomes.append(record)
            continue

        record["outcome"] = RETIRED
        if apply:
            try:
                _close(store, conn, position_id, reason=EXTERNALLY_CLOSED,
                       now=current)
                logger.warning(
                    "EXTERNALLY_CLOSED %s %s: broker reports no position and "
                    "no open order; retiring the canonical row without an "
                    "exit price -- this system did not sell these shares",
                    strategy_id, symbol)
                _audit(record, now=current)
            except Exception:  # noqa: BLE001
                logger.warning("could not retire %s", position_id,
                               exc_info=True)
                record["outcome"] = "RETIRE_FAILED"
        outcomes.append(record)
    return outcomes


def _close(store, conn, position_id, *, reason, now):
    """Through the store's own transition, never a status write.

    S1's `close_position` takes `exit_reason`; S2's and S6's take
    `reason`. The signature is inspected rather than guessed -- guessing
    raises TypeError in the middle of a repair.

    `exit_price` is deliberately not passed: there is no price to record.
    """
    import inspect

    parameters = inspect.signature(store.close_position).parameters
    keyword = "exit_reason" if "exit_reason" in parameters else "reason"
    return store.close_position(conn, position_id, now=now,
                                **{keyword: reason})


def _audit(record, *, now):
    try:
        import shadow_audit

        shadow_audit.record_event(
            shadow_run_id=shadow_audit.new_run_id(),
            event_type="RECONCILIATION_BLOCKED", result="INFO",
            symbol=record.get("symbol"), side="sell",
            internal_order_id=record.get("position_id"),
            reason_code=EXTERNALLY_CLOSED, payload=record, now=now)
    except Exception:  # noqa: BLE001 - the retirement is durable already;
        # losing its audit row must not undo it.
        logger.warning("external-close audit failed for %s",
                       record.get("position_id"), exc_info=True)


def retire_general_store(broker, conn, *, store=None, now=None,
                         apply=True) -> List[Dict[str, Any]]:
    """The same retirement, for the general lifecycle book.

    There are two books, and S1 writes to both: `positions/store.py` and
    the per-strategy store above. Retiring only the per-strategy row left
    the position visible to `reconciliation.snapshot.load_internal_positions`,
    which reads both -- so the account still reconciled as
    "internal=1 KIS=0" with nothing left to point at.

    The general book is a validated state machine, so this cannot simply
    write a status the way the per-strategy store does. It takes the
    store's own lock, validates the transition, and appends to the
    record's history like every other transition in the system.

    EXIT_SUBMITTED is deliberately NOT retirable: an exit in flight
    settles to CLOSED through the normal path, and a settled exit has a
    real price this must not pre-empt. Nor are MANUAL_REVIEW and
    RECOVERY_REQUIRED, where a human already owns the decision.
    """
    from positions import states, store as position_store

    store = store or position_store
    current = now or datetime.now(timezone.utc)
    stamp = current.isoformat()

    try:
        broker_positions = _symbols_of(broker.get_positions())
        broker_orders = _symbols_of(broker.get_open_orders())
    except Exception as exc:  # noqa: BLE001 - unreadable is not flat
        logger.warning("broker unreadable; retiring nothing: %s", exc)
        return [{"outcome": HELD_BROKER_UNREADABLE, "detail": str(exc)[:200]}]

    try:
        unresolved_orders = _unresolved_order_symbols(conn)
        unresolved_intents = _unresolved_intent_positions(conn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("order state unreadable; retiring nothing: %s", exc)
        return [{"outcome": HELD_UNRESOLVED_ORDER, "detail": str(exc)[:200]}]

    outcomes: List[Dict[str, Any]] = []
    for position_id, record in (store.load_non_terminal() or {}).items():
        if not record.get("remaining_qty"):
            continue
        symbol = str(record.get("symbol") or "").upper()
        state = record.get("state")
        report = {
            "position_id": position_id,
            "strategy_id": record.get("strategy_id"),
            "symbol": symbol,
            "previous_state": state,
            "previous_quantity": record.get("remaining_qty"),
            "reconciled_at": stamp,
            "broker_positions_seen": sorted(broker_positions),
            "broker_open_orders_seen": sorted(broker_orders),
        }

        held = evaluate(
            {"exit_submitted": state == states.EXIT_SUBMITTED},
            symbol=symbol, position_id=position_id,
            broker_positions=broker_positions, broker_orders=broker_orders,
            unresolved_orders=unresolved_orders,
            unresolved_intents=unresolved_intents)
        if held is None and states.EXTERNALLY_CLOSED not in states.TRANSITIONS.get(state, set()):
            # Asked of the state machine rather than a list kept here, so
            # the two cannot drift apart.
            held = HELD_STATE_NOT_RETIRABLE
        if held is not None:
            outcomes.append({**report, "outcome": held})
            continue

        report["outcome"] = RETIRED
        report["source"] = EXTERNALLY_CLOSED
        if not apply:
            outcomes.append(report)
            continue

        logger.warning(
            "%s %s %s: broker reports no position and no open order; "
            "retiring the general-book row without an exit price -- "
            "this system did not sell these shares",
            EXTERNALLY_CLOSED, record.get("strategy_id"), symbol)
        with store.locked_position(position_id, conn=conn) as locked:
            # Re-read under the lock: the state may have moved between the
            # scan above and here, and a position that started exiting in
            # that window must not be retired out from under its exit.
            states.validate_transition(locked["state"], states.EXTERNALLY_CLOSED)
            locked["state"] = states.EXTERNALLY_CLOSED
            locked["state_history"].append({
                "state": states.EXTERNALLY_CLOSED,
                "at": stamp,
                "reason": "broker reports no position and no open order; "
                          "closed outside this system",
            })
        outcomes.append(report)

    return outcomes
