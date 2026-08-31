"""Resolve exit intents whose submission was never confirmed.

The jam this clears
-------------------
When a SELL submission returns ambiguously, the exit path does the
careful thing: it marks the intent SUBMISSION_UNKNOWN, re-latches the
position, and refuses to retry. The comment says why -- "reconciliation
decides" -- because re-sending an order that may already be live is how
a position gets sold twice.

Reconciliation never decided. There was no code that looked at
SUBMISSION_UNKNOWN intents at all, so the intent stayed active forever
and `reserve()` refused every subsequent exit for that position:

    S6 exit for s6pos_9e3a61e64e234aca already has an active intent --
    not calling the broker: exitintent_8ec53e6d5a764b22

RIG latched EXIT_PENDING on 2026-08-28 at 19:52, its SELL came back
ambiguous a minute later, and it then held 3 shares that could not be
sold for the rest of the weekend. Reconciliation reported "clean"
throughout, because by its own measure it was: internal and broker
agreed on the position, and there were no open orders. The disagreement
was in a ledger nobody was reading.

What counts as evidence
-----------------------
An intent is only resolved on POSITIVE evidence from KIS:

  the order is in KIS's open orders   -> it landed; mark it SUBMITTED
                                         and let the normal settlement
                                         path take it from there.

  the order appears in KIS's fills    -> it landed and filled; the
                                         position close follows from the
                                         fill, not from here.

  KIS has neither, AND the position   -> the submission never reached
  is still held in full                  the broker. Abort the intent so
                                         a fresh exit can be reserved.

Anything else stays SUBMISSION_UNKNOWN. A partially filled position, a
quantity that does not match, an unreadable broker: all of them leave
the intent exactly where it is. Guessing here re-sends a live order.

This never places or cancels an order. It reads KIS and moves a ledger
row, and the exit runtime does the selling on its next tick.
"""

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

RESOLVED_LANDED = "ORDER_FOUND_AT_BROKER"
RESOLVED_FILLED = "FILL_FOUND_AT_BROKER"
RESOLVED_NEVER_SENT = "NO_ORDER_AT_BROKER_POSITION_INTACT"
UNRESOLVED_AMBIGUOUS = "EVIDENCE_INCONCLUSIVE"
UNRESOLVED_UNREADABLE = "BROKER_UNREADABLE"


def _order_ids(entries) -> set:
    """Every identifier an order might be recognised by."""
    found = set()
    for entry in entries or ():
        for key in ("client_order_id", "ODNO", "odno", "broker_order_id",
                    "order_id"):
            value = (entry.get(key) if isinstance(entry, dict)
                     else getattr(entry, key, None))
            if value:
                found.add(str(value))
    return found


def classify(intent, *, open_order_ids, fill_ids, held_quantity,
             requested_quantity) -> Dict[str, Any]:
    """What the evidence says about one SUBMISSION_UNKNOWN intent."""
    client_order_id = str(intent.get("client_order_id") or "")
    broker_order_id = str(intent.get("broker_order_id") or "")
    identifiers = {i for i in (client_order_id, broker_order_id) if i}

    if identifiers & set(fill_ids or ()):
        return {"resolved": True, "state": "CONFIRMED",
                "reason": RESOLVED_FILLED}
    if identifiers & set(open_order_ids or ()):
        return {"resolved": True, "state": "SUBMITTED",
                "reason": RESOLVED_LANDED}

    # No trace at the broker. That only means "never sent" if the shares
    # are all still here -- a position short of its full quantity may
    # have been partly sold by the very order we cannot find.
    if held_quantity is None or requested_quantity is None:
        return {"resolved": False, "reason": UNRESOLVED_AMBIGUOUS}
    if int(held_quantity) == int(requested_quantity) and held_quantity > 0:
        return {"resolved": True, "state": "ABORTED",
                "reason": RESOLVED_NEVER_SENT}
    return {"resolved": False, "reason": UNRESOLVED_AMBIGUOUS}


def resolve_unknown_exit_intents(conn, broker, *, now=None) -> List[dict]:
    """Every SUBMISSION_UNKNOWN exit intent, decided or left alone.

    Read-only against KIS. Returns what it did, so a pass that changed
    nothing is distinguishable from a pass that did not run.
    """
    from state_store import exit_intent_ledger as ledger

    try:
        rows = conn.execute(
            "SELECT * FROM exit_intents WHERE state = ?",
            (ledger.STATE_SUBMISSION_UNKNOWN,)).fetchall()
    except Exception:  # noqa: BLE001
        logger.warning("exit intents unreadable this pass", exc_info=True)
        return []
    if not rows:
        return []

    try:
        open_orders = broker.get_open_orders() or []
        fills = broker.get_fills() or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("cannot resolve exit intents this pass: %s", exc)
        return [{"intent_id": r["intent_id"], "resolved": False,
                 "reason": UNRESOLVED_UNREADABLE} for r in rows]

    open_ids = _order_ids(open_orders)
    fill_ids = _order_ids(fills)

    outcomes = []
    for row in rows:
        intent = dict(row)
        held = _held_quantity(conn, intent.get("position_id"))
        verdict = classify(intent, open_order_ids=open_ids, fill_ids=fill_ids,
                           held_quantity=held,
                           requested_quantity=intent.get("requested_qty"))
        record = {"intent_id": intent["intent_id"],
                  "position_id": intent.get("position_id"),
                  "resolved": verdict["resolved"],
                  "reason": verdict["reason"]}
        if not verdict["resolved"]:
            logger.info("exit intent %s stays SUBMISSION_UNKNOWN: %s",
                        intent["intent_id"], verdict["reason"])
            outcomes.append(record)
            continue
        try:
            _apply(conn, ledger, intent["intent_id"], verdict["state"])
            record["state"] = verdict["state"]
            logger.warning(
                "EXIT_INTENT_RESOLVED %s -> %s (%s); the position may now be "
                "exited again by the normal runtime",
                intent["intent_id"], verdict["state"], verdict["reason"])
        except Exception:  # noqa: BLE001
            logger.warning("could not move exit intent %s",
                           intent["intent_id"], exc_info=True)
            record["resolved"] = False
        outcomes.append(record)
    return outcomes


def _apply(conn, ledger, intent_id, state):
    """Through the ledger's own transitions, never a status write."""
    if state == "ABORTED":
        ledger.mark_aborted(conn, intent_id)
    elif state == "SUBMITTED":
        ledger.mark_submitted(conn, intent_id)
    elif state == "CONFIRMED":
        # The filled quantity is settled by the fill path, which owns
        # that number; this only records that the submission landed.
        ledger.mark_reconciliation_required(conn, intent_id)
    else:  # pragma: no cover - guarded by classify()
        raise ValueError(f"unexpected resolution state {state!r}")


def _held_quantity(conn, position_id):
    if not position_id:
        return None
    try:
        row = conn.execute(
            "SELECT quantity FROM s6_positions WHERE position_id = ?",
            (position_id,)).fetchone()
    except Exception:  # noqa: BLE001
        return None
    return row["quantity"] if row else None
