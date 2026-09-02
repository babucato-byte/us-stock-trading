"""Adopt shares S6's own filled order produced but no S6 row tracks.

The problem this exists for
---------------------------
On 2026-09-02 order 0030708837 filled 7 HBAN at 17.01. Nothing recorded
it. `sync_buy_fills` could not run -- the execution lock was held by the
entry cycle for 24 of 25 minutes -- and when a tick finally got in, KIS
had already dropped the order from the open-order book but had not yet
published the fill. `entry_timeout` read "not resting" as "never
filled", wrote BUY_NEVER_FILLED, and closed the row.

The account then held 7 shares that no position tracked. That is the
worst state the system has: the exit runtime cannot see the position, so
no stop, no target and no exit rule applies to it, and reconciliation can
only report the mismatch -- it has no way to repair it. `sync_buy_fills`
never revisits the row because CLOSED is terminal, and S6 had no
adoption path at all: `ownership.may_adopt` existed and only S1 called
it.

What this does
--------------
For a broker holding with no live S6 row, it asks -- and every answer
must be yes before a row is written:

    is it unclaimed?            `ownership.may_adopt`, ledger first
    did an S6 BUY actually fill it?  the order ledger, status FILLED
    is there a REAL fill price? the broker's own average

Then it records the position from that fill, at that price.

Never an invented price
-----------------------
The entry price is what every later decision is measured from -- the
structural stop above all. A position opened at a guessed price looks
correct and is not, which is worse than no position. If the broker
cannot supply a usable average, this refuses and leaves the mismatch
standing for a human.

Strategy context is carried, not re-derived
-------------------------------------------
The wrongly-closed row still holds the range, VWAP and EMA the entry was
evidenced on. Those are copied onto the adopted row so the exit rules
that need them keep working. They are NOT recomputed from today's
market: the position was entered on the values the strategy saw, and
re-deriving them would silently move the stop.

Adopting on doubt is how one position gets two exit engines. Every
refusal here leaves a visible, reportable mismatch instead.
"""

import logging
from typing import Any, Dict, List, Optional

from s6_live import position_store
from s6_live.position_store import STRATEGY_ID

logger = logging.getLogger(__name__)

#: Recorded as the reason the row exists, so an adopted position is never
#: mistaken for one this system decided to open in the normal way.
SOURCE_BROKER_CONFIRMED_FILL = "BROKER_CONFIRMED_FILL"

ADOPTED = "ADOPTED"
SKIPPED_ALREADY_TRACKED = "ALREADY_TRACKED"
SKIPPED_NOT_OURS = "NOT_OURS"
SKIPPED_NO_FILLED_ORDER = "NO_FILLED_ORDER"
SKIPPED_NO_FILL_PRICE = "NO_FILL_PRICE"
SKIPPED_FAILED = "FAILED"


def _filled_buy_order(conn, symbol) -> Optional[Dict[str, Any]]:
    """The S6 BUY order that produced this holding, or None.

    Positive evidence, not absence of contradiction: only a row the
    ledger says reached FILLED counts. An ACCEPTED order may still be
    resting, and adopting from one would record a position for shares
    the account does not hold.
    """
    try:
        row = conn.execute(
            "SELECT broker_order_id, strategy_id, status, requested_quantity "
            "FROM kis_order_idempotency "
            "WHERE side = 'buy' AND UPPER(symbol) = ? AND status = 'FILLED' "
            "ORDER BY rowid DESC LIMIT 1", (str(symbol).upper(),)).fetchone()
    except Exception:  # noqa: BLE001 -- unreadable ledger is not evidence
        logger.warning("S6 adoption: order ledger unreadable for %s", symbol,
                       exc_info=True)
        return None
    if row is None:
        return None
    record = dict(row)
    if record.get("strategy_id") and record["strategy_id"] != STRATEGY_ID:
        return None
    return record


def _prior_context(conn, symbol) -> Dict[str, Any]:
    """The strategy context from the most recent row for this symbol.

    Empty when there is none: an adopted position with no range or VWAP
    is still worth managing on its structural stop, and the exit runtime
    already reports the rules it cannot evaluate rather than guessing.
    """
    try:
        row = conn.execute(
            f"SELECT * FROM {position_store.TABLE} WHERE symbol = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (str(symbol).upper(),)).fetchone()
    except Exception:  # noqa: BLE001
        return {}
    return dict(row) if row is not None else {}


def adopt_untracked_fills(conn, *, broker, now=None, apply=True) -> List[Dict[str, Any]]:
    """Record broker holdings that an S6 fill produced and nothing tracks.

    `apply=False` reports what it would do and writes nothing.
    """
    from reconciliation import ownership

    results: List[Dict[str, Any]] = []
    try:
        positions = broker.get_positions() or []
    except Exception as exc:  # noqa: BLE001 -- no holdings read, no adoption
        logger.warning("S6 adoption: broker positions unreadable: %s",
                       type(exc).__name__)
        return results

    for position in positions:
        symbol = str(getattr(position, "symbol", "") or "").upper()
        try:
            quantity = int(getattr(position, "quantity", 0) or 0)
        except (TypeError, ValueError):
            quantity = 0
        average = getattr(position, "average_fill_price", None)
        if not symbol or quantity < 1:
            continue

        if position_store.load_by_symbol(conn, symbol) is not None:
            continue  # a live row already tracks it

        permitted, why = ownership.may_adopt(conn, symbol, strategy_id=STRATEGY_ID)
        if not permitted:
            logger.warning("S6 will not adopt the broker holding of %s: %s",
                           symbol, why)
            results.append({"symbol": symbol, "action": SKIPPED_NOT_OURS,
                            "detail": why})
            continue

        order = _filled_buy_order(conn, symbol)
        if order is None:
            logger.warning(
                "S6 will not adopt %s: no FILLED S6 buy order in the ledger "
                "accounts for it", symbol)
            results.append({"symbol": symbol, "action": SKIPPED_NO_FILLED_ORDER})
            continue

        price = None
        try:
            price = float(average) if average is not None else None
        except (TypeError, ValueError):
            price = None
        if price is None or price <= 0:
            logger.error(
                "S6 will not adopt %s qty=%d: the broker reports no usable "
                "average fill price, and the entry price is what the "
                "structural stop is measured from", symbol, quantity)
            results.append({"symbol": symbol, "action": SKIPPED_NO_FILL_PRICE})
            continue

        if not apply:
            results.append({"symbol": symbol, "action": ADOPTED,
                            "quantity": quantity, "entry_price": price,
                            "applied": False})
            continue

        context = _prior_context(conn, symbol)
        try:
            position_id = position_store.record_submission(
                conn, symbol=symbol,
                variant=context.get("variant"),
                entry_session=context.get("entry_session"),
                client_order_id=context.get("client_order_id"),
                range_minutes=context.get("range_minutes"),
                range_high=context.get("range_high"),
                range_low=context.get("range_low"),
                entry_vwap=context.get("entry_vwap"),
                entry_ema9=context.get("entry_ema9"),
                entry_ema21=context.get("entry_ema21"),
                entry_volume_expansion=context.get("entry_volume_expansion"),
                now=now)
            opened = position_store.open_from_fill(
                conn, position_id, quantity=quantity, average_fill_price=price,
                entry_order_id=order.get("broker_order_id"), now=now)
        except Exception as exc:  # noqa: BLE001 -- a failed adoption must
            # leave the mismatch standing, not a half-written row.
            logger.error("S6 adoption failed for %s: %s", symbol, exc)
            results.append({"symbol": symbol, "action": SKIPPED_FAILED,
                            "detail": type(exc).__name__})
            continue

        if not opened:
            results.append({"symbol": symbol, "action": SKIPPED_FAILED,
                            "detail": "the row could not be promoted to OPEN"})
            continue

        logger.info(
            "S6 adopted a confirmed fill nothing tracked: %s %s qty=%d @ %.4f "
            "order=%s source=%s", position_id, symbol, quantity, price,
            order.get("broker_order_id"), SOURCE_BROKER_CONFIRMED_FILL)
        results.append({"position_id": position_id, "symbol": symbol,
                        "action": ADOPTED, "quantity": quantity,
                        "entry_price": price,
                        "entry_order_id": order.get("broker_order_id"),
                        "source": SOURCE_BROKER_CONFIRMED_FILL,
                        "applied": True})
    return results
