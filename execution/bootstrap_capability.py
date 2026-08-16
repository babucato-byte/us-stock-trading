"""The one thing that lets a LIMITED LIVE bootstrap order past
`KISConfig.validate_live_order_allowed()` while KIS_LIVE_ORDER_ENABLED
stays false.

Why this exists
---------------
The first attempt at a real bootstrap order reached
`KISBroker.submit_order()` and was refused there, because the broker's
last-line guard recognises exactly one authorisation --
KIS_LIVE_ORDER_ENABLED=true -- and the bootstrap posture deliberately
never sets it. Posture and gate said yes; the broker said no. That is
the guard working correctly: it is the last thing between this codebase
and a real order, and it should not accept a posture it was never told
about.

So the bootstrap gets its own authorisation, and it is deliberately NOT
another environment variable.

Why not an environment variable
-------------------------------
`LIVE_BOOTSTRAP_ENABLED=true` is a deployment-wide fact. If the broker
guard accepted it directly, then for as long as it were set, EVERY order
reaching that guard -- from the scanner, the armed runner, a stray
script, a future caller nobody has written yet -- would pass. The blast
radius of one env var would be "all orders", which is precisely what the
guard exists to prevent.

A capability is a per-order object instead. It names the exact symbol,
side, quantity and order type it authorises, it is created in exactly
one place (`live_pilot/bootstrap.py`), and the broker re-validates it
against the order actually in hand. An ordinary code path does not
accidentally acquire one: it has to construct it, and
tests/test_broker_bootstrap_capability.py pins that none of them do.

The environment is still checked -- posture, enabled, ack -- but as a
NECESSARY condition alongside the capability, never as a sufficient one.
Both must hold, at mint time and again at the guard.
"""

import os
import secrets
from dataclasses import dataclass
from typing import FrozenSet

MODE_LIMITED_LIVE_BOOTSTRAP = "LIMITED_LIVE_BOOTSTRAP"

# The only shape a bootstrap order may ever have. Stated here as well as
# in live_pilot/bootstrap.py on purpose: this copy is what the BROKER
# checks, one statement before the wire, against the order it was
# actually handed.
BOOTSTRAP_SIDE = "buy"
BOOTSTRAP_QUANTITY = 1
BOOTSTRAP_ORDER_TYPE = "limit"

FLAG_BOOTSTRAP_ENABLED = "LIVE_BOOTSTRAP_ENABLED"
FLAG_BOOTSTRAP_ACK = "LIVE_BOOTSTRAP_ACK"


class BootstrapCapabilityError(Exception):
    """The capability is absent, malformed, or does not authorise this
    order. Always fail-closed: no order is sent."""


@dataclass(frozen=True)
class BootstrapCapability:
    """Immutable, per-order authorisation for exactly one bootstrap BUY.

    Frozen so that nothing between mint and the wire can widen it -- a
    mutable capability whose `quantity` could be edited downstream would
    be no better than a flag.
    """

    mode: str
    symbol: str
    side: str
    quantity: int
    order_type: str
    allowed_symbols: FrozenSet[str]
    token: str

    def describes(self, order_intent):
        """True when this capability authorises exactly this order."""
        return (
            self.mode == MODE_LIMITED_LIVE_BOOTSTRAP
            and self.symbol == getattr(order_intent, "symbol", None)
            and self.side == getattr(order_intent, "side", None)
            and self.quantity == getattr(order_intent, "quantity", None)
            and self.order_type == getattr(order_intent, "order_type", None)
        )


def _env_true(mapping, name):
    return str(mapping.get(name, "") or "").strip().lower() == "true"


def _check_environment(mapping):
    """Posture, capability flag and acknowledgement. Necessary, never
    sufficient -- the caller must ALSO hold a capability object."""
    from live_pilot import posture as posture_mod

    decision = posture_mod.resolve_posture(mapping)
    if decision.posture != MODE_LIMITED_LIVE_BOOTSTRAP:
        raise BootstrapCapabilityError(
            f"posture is {decision.posture!r}, not {MODE_LIMITED_LIVE_BOOTSTRAP}")
    if not _env_true(mapping, FLAG_BOOTSTRAP_ENABLED):
        raise BootstrapCapabilityError(f"{FLAG_BOOTSTRAP_ENABLED} is not true")
    if not _env_true(mapping, FLAG_BOOTSTRAP_ACK):
        raise BootstrapCapabilityError(f"{FLAG_BOOTSTRAP_ACK} is not true")


def mint(*, symbol, allowed_symbols, env=None):
    """Create the capability for one bootstrap BUY of one share.

    Called from exactly one place -- `live_pilot.bootstrap.
    run_bootstrap_buy()`. Every other caller is a bug, and an
    isolation test enumerates the ordinary trading modules to prove
    none of them reach this function.

    The allow-list must hold exactly this one symbol. Not "contain" it:
    a bootstrap authorised while two symbols are live-eligible is a
    bootstrap that could have picked the other one.
    """
    mapping = env if env is not None else os.environ
    _check_environment(mapping)

    allowed = frozenset(allowed_symbols or ())
    if len(allowed) != 1:
        raise BootstrapCapabilityError(
            f"live allow-list must hold exactly one symbol, holds {len(allowed)}")
    if symbol not in allowed:
        raise BootstrapCapabilityError(
            f"{symbol!r} is not the allow-listed symbol")

    return BootstrapCapability(
        mode=MODE_LIMITED_LIVE_BOOTSTRAP,
        symbol=symbol,
        side=BOOTSTRAP_SIDE,
        quantity=BOOTSTRAP_QUANTITY,
        order_type=BOOTSTRAP_ORDER_TYPE,
        allowed_symbols=allowed,
        token=secrets.token_hex(16),
    )


def validate(capability, order_intent, *, env=None):
    """Re-check the capability at the broker guard, against the order
    actually in hand.

    Everything is re-verified here rather than trusted from mint time.
    Between mint and this call the order passed through the gate, the
    idempotency ledger and the state machine; the guard's job is to
    assume none of that and check again. Raises rather than returning
    False so a caller cannot ignore the answer.
    """
    mapping = env if env is not None else os.environ

    if capability is None:
        raise BootstrapCapabilityError("no bootstrap capability supplied")
    if not isinstance(capability, BootstrapCapability):
        raise BootstrapCapabilityError(
            f"bootstrap capability must be a BootstrapCapability, got "
            f"{type(capability).__name__}")
    if not capability.token:
        raise BootstrapCapabilityError("bootstrap capability carries no token")

    # The environment must STILL authorise a bootstrap. An ack revoked
    # between mint and transport revokes the order too.
    _check_environment(mapping)

    if capability.mode != MODE_LIMITED_LIVE_BOOTSTRAP:
        raise BootstrapCapabilityError(f"capability mode is {capability.mode!r}")

    expected = (BOOTSTRAP_SIDE, BOOTSTRAP_QUANTITY, BOOTSTRAP_ORDER_TYPE)
    held = (capability.side, capability.quantity, capability.order_type)
    if held != expected:
        raise BootstrapCapabilityError(
            f"capability authorises {held}, but a bootstrap order is fixed at {expected}")

    if order_intent is None:
        raise BootstrapCapabilityError("no order_intent to validate the capability against")
    if not capability.describes(order_intent):
        actual = (getattr(order_intent, "symbol", None), getattr(order_intent, "side", None),
                  getattr(order_intent, "quantity", None), getattr(order_intent, "order_type", None))
        raise BootstrapCapabilityError(
            f"capability authorises {(capability.symbol, *held)} but the order is {actual}")

    if capability.symbol not in capability.allowed_symbols or len(capability.allowed_symbols) != 1:
        raise BootstrapCapabilityError(
            "capability symbol is not the single allow-listed symbol")
    return True
