"""The one-shot DAYTIME route verification: BUY, cancel, and leave flat.

The flow, and what each step is allowed to conclude
---------------------------------------------------
    mint capability        dedicated flags + a one-symbol allow-list
    read KIS price facts   one read; last / low / e_hogau / e_ordyn
    limit = min(last, low) - 2 ticks        (execution.route_verification)
    BUY  TTTS6036U qty 1 LIMIT              -> proves the buy leg
    verify against KIS's own open-order book
      |- OPEN_UNFILLED  -> cancel TTTS6038U -> proves the cancel legs
      `- FILLED         -> flatten TTTS6037U for the ACTUAL filled qty

Nothing infers a conclusion from the previous step's hope. The submit
response says only that KIS accepted the message; whether the order is
resting or filled is asked of KIS, and the answer decides which branch
runs. That is `bootstrap.verify_buy`'s contract and it is reused rather
than restated.

Why the flatten instead of an exit engine
-----------------------------------------
This order does not want the shares. Building a scheduled exit engine for
a position nobody intends to hold would be machinery to babysit an
accident. So an unexpected fill is flattened immediately on the daytime
SELL route -- TTTS6037U, the one daytime leg a live response has already
confirmed (2026-08-27, odno 0000001014, filled 1 @ 51.61).

When the flatten itself fails
-----------------------------
Then there IS real exposure, and the intent stops mattering. The
remaining quantity is handed to S6's exit monitor -- the only live exit
engine that runs on a schedule -- carrying ROUTE_VERIFICATION so it is
managed without entering S6's performance record. A fallback, never the
plan, and it is the reason this module refuses to end quietly: an
unflattened fill raises, and the caller alerts.

One of each verb, ever
----------------------
`_TransportBudget` allows at most one submit_order, one cancel_order and
one flatten for the lifetime of the process. It is a property of the
object the engine holds, not of anyone remembering not to loop. Nothing
here retries: an ambiguous response to any verb is terminal.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from execution import route_verification as capability_mod

logger = logging.getLogger(__name__)

#: Recorded as the strategy on the order ledger row. NOT an S6 id: the
#: ledger is what `reconciliation/ownership.claimant_from_ledger` reads,
#: and attributing this to S6 would make S6 the claimant of a position it
#: never decided to take.
VERIFICATION_STRATEGY_ID = "ROUTE_VERIFICATION_V1"

#: How long the synthetic signal behind the order stays valid. Short: the
#: order is meant to exist for seconds.
SIGNAL_VALID_SECONDS = 300

CONCLUSION_CANCELLED = "ROUTE_VERIFIED_CANCELLED"
CONCLUSION_FLATTENED = "ROUTE_VERIFIED_FLATTENED"
CONCLUSION_EXPOSED = "FLATTEN_FAILED_EXPOSURE"


class RouteVerificationBlocked(Exception):
    """A precondition failed. No order was sent."""

    def __init__(self, message, *, reason_codes=()):
        super().__init__(message)
        self.reason_codes = tuple(reason_codes)


class RouteVerificationExposed(Exception):
    """The BUY filled and the flatten did not complete.

    Terminal and loud. The remaining quantity has been handed to S6's
    exit monitor under the ROUTE_VERIFICATION marker, and a human must
    know that a route test left real shares behind.
    """

    def __init__(self, message, *, remaining_qty=None, position_id=None):
        super().__init__(message)
        self.remaining_qty = remaining_qty
        self.position_id = position_id


class _TransportBudget:
    """One submit, one cancel, one flatten. Spent BEFORE the call."""

    def __init__(self, broker):
        self._broker = broker
        self.submit_calls = 0
        self.cancel_calls = 0
        self.flatten_calls = 0

    def __getattr__(self, item):
        return getattr(self._broker, item)

    def submit_order(self, order_intent, instrument, *args, **kwargs):
        side = getattr(order_intent, "side", None)
        if side == "sell":
            if self.flatten_calls:
                raise RouteVerificationBlocked(
                    "the flatten budget is spent; refusing a second SELL")
            self.flatten_calls += 1
        else:
            if self.submit_calls:
                raise RouteVerificationBlocked(
                    "the BUY budget is spent; refusing a second BUY")
            if getattr(order_intent, "quantity", None) != \
                    capability_mod.VERIFICATION_QUANTITY:
                raise RouteVerificationBlocked(
                    "a verification BUY is exactly one share")
            self.submit_calls += 1
        return self._broker.submit_order(order_intent, instrument, *args, **kwargs)

    def cancel_order(self, *args, **kwargs):
        if self.cancel_calls:
            raise RouteVerificationBlocked(
                "the cancel budget is spent; refusing a second cancel")
        self.cancel_calls += 1
        return self._broker.cancel_order(*args, **kwargs)


def _now(now=None):
    return now or datetime.now(timezone.utc)


def price_facts(broker, instrument) -> Dict[str, Any]:
    """One KIS price-detail read, whole. Raises if it cannot be had."""
    try:
        return broker.get_price_detail(instrument)
    except Exception as exc:  # noqa: BLE001
        raise RouteVerificationBlocked(
            f"KIS price detail unavailable: {exc}",
            reason_codes=("PRICE_DETAIL_UNAVAILABLE",)) from exc


def build_intent(*, symbol, instrument, limit_price, now):
    """The order intent. Session is stamped, never left to a default."""
    from domain.order_intent import OrderIntent

    return OrderIntent(
        internal_order_id=f"rtverify-{symbol}-{uuid.uuid4().hex[:12]}",
        signal_id=f"rtverify-{symbol}-{uuid.uuid4().hex[:8]}",
        strategy_id=VERIFICATION_STRATEGY_ID,
        symbol=symbol, exchange=instrument.exchange,
        side=capability_mod.VERIFICATION_SIDE,
        quantity=capability_mod.VERIFICATION_QUANTITY,
        order_type=capability_mod.VERIFICATION_ORDER_TYPE,
        limit_price=limit_price, stop_price=None, target_price=None,
        created_at=now, session=capability_mod.VERIFICATION_SESSION,
    )


def adopt_exposure(conn, *, symbol, quantity, basis, broker_order_id,
                   client_order_id, now=None):
    """Hand unflattened shares to S6's exit monitor, marked.

    The ONLY path by which a verification order becomes a managed
    position, and it runs only after a flatten has failed. The marker
    travels on the row so every S6 performance reader can exclude it: S6
    is managing these shares, and S6 did not trade them.

    The basis is the broker's own average. Never a guess -- a position
    opened at an invented price looks correct and is not, and the stop
    would be measured from it.
    """
    from s6_live import position_store

    current = _now(now)
    position_id = position_store.record_submission(
        conn, symbol=symbol, variant=capability_mod.ROUTE_VERIFICATION_MARKER,
        entry_session=capability_mod.VERIFICATION_SESSION,
        client_order_id=client_order_id, now=current)
    # The broker's OWN average, through the store's own refusal of an
    # unusable price. A basis that cannot be established must not become
    # a position: the stop is measured from it.
    position_store.open_from_fill(
        conn, position_id, quantity=quantity, average_fill_price=basis,
        entry_order_id=broker_order_id, now=current)
    logger.error(
        "ROUTE_VERIFICATION_EXPOSURE symbol=%s qty=%s basis=%s position=%s -- "
        "the verification BUY filled and could not be flattened; S6's exit "
        "monitor now owns it under the %s marker and it is excluded from S6 "
        "performance", symbol, quantity, basis, position_id,
        capability_mod.ROUTE_VERIFICATION_MARKER)
    return position_id
