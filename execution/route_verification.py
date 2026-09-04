"""One real DAYTIME order, placed to prove a route rather than to trade.

Why this is not the bootstrap
-----------------------------
`live_pilot/bootstrap.py` places a real order to prove the PIPELINE: its
candidate is a genuine published strategy row, its symbol cannot be
passed in, and its price is the strategy's own. That is the right shape
for proving that discovery, qualification, gating and transport work
together.

It is the wrong shape for proving a WIRE VALUE. `TTTS6036U` has never
carried a buy, and it can only be confirmed by one -- but S6-O produced
no candidate at all across a five-hour daytime window (595 of 595 symbols
short of the data to form an opening range), so waiting for the strategy
to offer one is not a route to verification.

So this is the other half, and the difference is deliberate at every
point:

    bootstrap              route verification
    ---------              ------------------
    strategy's candidate   an explicitly named liquid symbol
    strategy's price       a price chosen NOT to fill
    intends to fill        intends to rest, then be cancelled
    becomes a position     intends to leave the account flat

Never an orphan
---------------
The one thing both share is that a fill must never be unowned. SLGN on
2026-09-03 filled 3 @ 41.61 into a position row that had already been
closed BUY_NEVER_FILLED, and the shares sat with no stop and no exit rule
until a human noticed. This module's answer is not to build an exit
engine for a position nobody wants -- it is to not hold one: an
unexpected fill is flattened immediately on the daytime SELL route, which
is the one daytime leg a live response has already confirmed.

The flatten can itself fail, and then there IS exposure. That case hands
the remaining quantity to S6's exit monitor, the only live exit engine
that runs on a schedule, under a marker that keeps it out of S6's
performance record. A fallback, never the plan.

Nothing here is a bypass
------------------------
The capability is an object, not a flag. It names the symbol, side,
quantity, order type and SESSION it authorises; it is minted in exactly
one place; and it is re-validated against the order actually in hand at
the gate. There is no boolean any caller can set.
"""

import os
import secrets
from dataclasses import dataclass
from typing import FrozenSet, Optional

MODE_ROUTE_VERIFICATION = "ROUTE_VERIFICATION"

#: The only shape a verification order may ever have. One share, bought,
#: at a limit, in the one session whose route is unproven.
VERIFICATION_SIDE = "buy"
VERIFICATION_QUANTITY = 1
VERIFICATION_ORDER_TYPE = "limit"
VERIFICATION_SESSION = "OVERNIGHT_DAYTIME"

#: Dedicated flags. Deliberately NOT the bootstrap's: arming one must
#: never arm the other, and an operator reading the environment should be
#: able to tell which one-shot is live from the variable names alone.
FLAG_ENABLED = "ROUTE_VERIFICATION_ENABLED"
FLAG_ACK = "ROUTE_VERIFICATION_ACK"

#: How far below the reference price the resting limit is placed.
#:
#: TICKS, not a percentage. KIS publishes the instrument's own tick as
#: `e_hogau` on the price-detail read, and `config/s1_order_rules.py` is
#: explicit that this codebase must not invent an increment of its own:
#: "no rounding rule is invented… US equities are commonly $0.01 above
#: $1.00, but that is general market knowledge and not an official KIS
#: statement". Two ticks below the lower of the last trade and today's
#: low is therefore expressed entirely in the broker's own units.
#:
#: A percentage offset was considered and rejected. No DAYTIME price band
#: is documented anywhere, so a percentage is a guess about someone
#: else's validation rule wearing the appearance of a safety margin.
OFFSET_TICKS = 2


class RouteVerificationError(Exception):
    """The capability is absent, malformed, or does not authorise this
    order. Always fail-closed: no order is sent."""


class VerificationPriceUnavailable(Exception):
    """The limit price could not be established from KIS's own facts.

    Raised rather than defaulted. A verification order priced from a
    stale or missing quote is an order at a price nobody measured, which
    is the one thing a test of the wire must not introduce.
    """


@dataclass(frozen=True)
class RouteVerificationCapability:
    """Immutable, per-order authorisation for exactly one DAYTIME BUY."""

    mode: str
    symbol: str
    side: str
    quantity: int
    order_type: str
    session: str
    allowed_symbols: FrozenSet[str]
    token: str

    def describes(self, order_intent) -> bool:
        """True when this capability authorises exactly this order."""
        return (
            self.mode == MODE_ROUTE_VERIFICATION
            and self.symbol == getattr(order_intent, "symbol", None)
            and self.side == getattr(order_intent, "side", None)
            and self.quantity == getattr(order_intent, "quantity", None)
            and self.order_type == getattr(order_intent, "order_type", None)
            and self.session == getattr(order_intent, "session", None)
        )


def _env_true(mapping, name) -> bool:
    return str(mapping.get(name, "") or "").strip().lower() == "true"


def _check_environment(mapping):
    """Both dedicated flags, checked at mint AND again at the gate.

    Necessary, never sufficient: the caller must also hold the
    capability object, and an ordinary trading path has no way to obtain
    one.
    """
    if not _env_true(mapping, FLAG_ENABLED):
        raise RouteVerificationError(f"{FLAG_ENABLED} is not true")
    if not _env_true(mapping, FLAG_ACK):
        raise RouteVerificationError(f"{FLAG_ACK} is not true")


def limit_price_from(detail, *, offset_ticks=OFFSET_TICKS) -> float:
    """The resting limit, from KIS's own published facts only.

        reference = min(last, today_low)
        limit     = reference - offset_ticks * e_hogau

    Every input comes from one `get_price_detail` read, so the price
    cannot be assembled from two moments. Anything missing, non-finite,
    non-positive or not orderable raises: an unestablished price is a
    refusal, never a default.
    """
    if not isinstance(detail, dict):
        raise VerificationPriceUnavailable("no price detail was supplied")

    orderable = str(detail.get("orderable_text") or "").strip()
    if not orderable:
        raise VerificationPriceUnavailable(
            "KIS did not say whether the instrument is orderable (e_ordyn empty)")
    if "가능" not in orderable:
        raise VerificationPriceUnavailable(
            f"KIS reports the instrument is not orderable: {orderable!r}")

    def _positive(name, key):
        value = detail.get(key)
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise VerificationPriceUnavailable(
                f"{name} is missing or unreadable from the KIS price detail")
        if number != number or number in (float("inf"), float("-inf")):
            raise VerificationPriceUnavailable(f"{name} is not a finite number")
        if number <= 0:
            raise VerificationPriceUnavailable(f"{name} is not positive: {number!r}")
        return number

    last = _positive("last", "last")
    today_low = _positive("today's low", "low")
    tick = _positive("tick size (e_hogau)", "tick_size")

    reference = min(last, today_low)
    limit = reference - offset_ticks * tick
    if limit <= 0:
        raise VerificationPriceUnavailable(
            f"reference {reference!r} minus {offset_ticks} ticks of {tick!r} "
            "is not a positive price")

    from brokers.order_price import wire_price

    # The same normalisation every other order goes through. KIS refuses
    # more than two decimals at $1 and above (APTR0057), and subtracting
    # ticks can produce more.
    normalised = float(wire_price(limit, side=VERIFICATION_SIDE))
    if normalised <= 0:
        raise VerificationPriceUnavailable(
            f"the normalised price {normalised!r} is not positive")
    return normalised


def mint(*, symbol, allowed_symbols, env=None) -> RouteVerificationCapability:
    """Create the capability for one DAYTIME verification BUY.

    Called from exactly one place -- the route-verification runner. The
    allow-list must hold exactly this one symbol: a verification
    authorised while two symbols are live-eligible is one that could have
    picked the other.
    """
    mapping = env if env is not None else os.environ
    _check_environment(mapping)

    wanted = str(symbol or "").strip().upper()
    if not wanted:
        raise RouteVerificationError("no symbol was named")

    allowed = frozenset(str(s).strip().upper() for s in (allowed_symbols or ()) if s)
    if len(allowed) != 1:
        raise RouteVerificationError(
            f"the allow-list must hold exactly one symbol, holds {len(allowed)}")
    if wanted not in allowed:
        raise RouteVerificationError(f"{wanted!r} is not the allow-listed symbol")

    return RouteVerificationCapability(
        mode=MODE_ROUTE_VERIFICATION,
        symbol=wanted,
        side=VERIFICATION_SIDE,
        quantity=VERIFICATION_QUANTITY,
        order_type=VERIFICATION_ORDER_TYPE,
        session=VERIFICATION_SESSION,
        allowed_symbols=allowed,
        token=secrets.token_hex(16),
    )


def validate(capability, order_intent, *, env=None) -> bool:
    """Re-check the capability against the order actually in hand.

    Nothing is trusted from mint time. Between minting and this call the
    order passed through sizing, the idempotency ledger and the state
    machine; this assumes none of it and checks again. Raises rather than
    returning False so a caller cannot ignore the answer.
    """
    mapping = env if env is not None else os.environ

    if capability is None:
        raise RouteVerificationError("no route-verification capability supplied")
    if not isinstance(capability, RouteVerificationCapability):
        raise RouteVerificationError(
            "capability must be a RouteVerificationCapability, got "
            f"{type(capability).__name__}")
    if not capability.token:
        raise RouteVerificationError("capability carries no token")

    # The environment must STILL authorise it. An acknowledgement
    # withdrawn between mint and transport withdraws the order too.
    _check_environment(mapping)

    if capability.mode != MODE_ROUTE_VERIFICATION:
        raise RouteVerificationError(f"capability mode is {capability.mode!r}")

    expected = (VERIFICATION_SIDE, VERIFICATION_QUANTITY,
                VERIFICATION_ORDER_TYPE, VERIFICATION_SESSION)
    held = (capability.side, capability.quantity, capability.order_type,
            capability.session)
    if held != expected:
        raise RouteVerificationError(
            f"capability authorises {held}, but a verification order is fixed "
            f"at {expected}")

    if order_intent is None:
        raise RouteVerificationError(
            "no order_intent to validate the capability against")
    if not capability.describes(order_intent):
        actual = (getattr(order_intent, "symbol", None),
                  getattr(order_intent, "side", None),
                  getattr(order_intent, "quantity", None),
                  getattr(order_intent, "order_type", None),
                  getattr(order_intent, "session", None))
        raise RouteVerificationError(
            f"capability authorises {(capability.symbol, *held)} but the order "
            f"is {actual}")

    if (capability.symbol not in capability.allowed_symbols
            or len(capability.allowed_symbols) != 1):
        raise RouteVerificationError(
            "capability symbol is not the single allow-listed symbol")
    return True


#: The marker a position carries when the flatten failed and S6's exit
#: monitor had to take the remaining shares.
#:
#: It exists to keep two facts apart that would otherwise merge: S6 is
#: MANAGING these shares, and S6 did not TRADE them. Without the marker
#: the position would enter S6's performance record as an entry the
#: strategy never made, at a price no S6 signal chose.
ROUTE_VERIFICATION_MARKER = "ROUTE_VERIFICATION"


def is_route_verification(row) -> bool:
    """Whether a position row came from a verification flatten failure.

    Read from the row's own VARIANT -- where `adopt_exposure` writes the
    marker -- rather than inferred from the symbol or the price, so a
    genuine S6 entry in the same name is never mistaken for one of these.

    `s6_live.position_store.record_submission` stamps S6's strategy_id on
    every row it writes, which is right for MANAGEMENT: S6's exit monitor
    loads by strategy and must see these shares. It is wrong for
    PERFORMANCE, and the variant is what keeps the two apart.
    """
    if row is None:
        return False
    for field in ("variant", "entry_reason", "exit_reason", "origin"):
        try:
            value = row.get(field) if hasattr(row, "get") else row[field]
        except (KeyError, IndexError, TypeError):
            value = None
        if value and ROUTE_VERIFICATION_MARKER in str(value):
            return True
    return False


def exclude_from_performance(rows):
    """The rows an S6 performance reader should count.

    A verification position is MANAGED by S6 and was not TRADED by it.
    Anything aggregating realised results filters through this; anything
    deciding whether shares need an exit must NOT, because the whole
    point of the fallback is that they do.
    """
    return [row for row in (rows or ()) if not is_route_verification(row)]
