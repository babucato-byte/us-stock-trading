"""CODEX-043: `AuthorizedExecution` -- the token `brokers/kis_broker.py`'s
`submit_order()`/`cancel_order()` require before ever reaching the KIS
network transport. This is NOT "protection by underscore": a caller
cannot construct a valid, usable `AuthorizedExecution` by hand -- the
token field is a `secrets.token_hex()` value minted and registered ONLY
by `authorize_new_order()`/`authorize_cancel()` below, and `consume()`
checks membership in that in-process registry (removing it on use, so
each token is single-use) rather than trusting anything on the object
itself. A caller who fabricates an `AuthorizedExecution` with a made-up
`token` string fails `consume()`'s registry check.

Two authorization paths, matching spec §3's HALT policy:

- `authorize_new_order()` (buy/sell): checks HALT first (blocks if
  halted), then runs the caller's order_gate evaluation function. Used
  by `execution/execution_engine.py`'s `submit_buy_order()`/
  `submit_sell_order()`.
- `authorize_cancel()`: does NOT check HALT -- spec explicitly allows
  cancelling an existing unfilled order during HALT (risk-reducing, not
  a new order), a policy fixed here in code, not left to caller
  discretion. Still runs `order_gate.evaluate_cancel_gate()` (target
  order exists, is genuinely open, account/symbol match, not already
  being cancelled).
"""

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from operations import kill_switch as ops_kill_switch

# In-process registry of currently-valid, single-use tokens. Module-
# private in spirit (not exported, not documented as public API) --
# consume() is the only sanctioned way to check/remove from it.
_VALID_TOKENS = set()


class UnauthorizedExecutionError(Exception):
    """Raised whenever an order/cancel attempt lacks a currently-valid
    AuthorizedExecution token. Callers (brokers/kis_broker.py) must treat
    this as an unconditional hard block -- there is no fallback."""


@dataclass(frozen=True)
class AuthorizedExecution:
    internal_order_id: str
    side: str
    action: str  # "order" or "cancel"
    token: str
    authorized_at: datetime


def authorize_new_order(order_intent, gate_context_builder, gate_fn, *, now=None):
    """The ONLY way to mint an AuthorizedExecution for a NEW buy/sell
    order. Raises UnauthorizedExecutionError if HALT is active (spec:
    HALT stops ALL new order submission, buy or sell); raises whatever
    `gate_fn` raises (order_gate.OrderGateBlockedError) if the gate
    itself blocks. Returns an AuthorizedExecution only if both pass."""
    if not ops_kill_switch.is_automatic_order_allowed():
        raise UnauthorizedExecutionError(
            f"HALT is active -- cannot authorize a new {order_intent.side} order for "
            f"{order_intent.symbol!r}"
        )
    ctx = gate_context_builder()
    gate_fn(ctx)
    token = secrets.token_hex(16)
    _VALID_TOKENS.add(token)
    return AuthorizedExecution(
        internal_order_id=order_intent.internal_order_id, side=order_intent.side,
        action="order", token=token, authorized_at=now or datetime.now(timezone.utc),
    )


def authorize_cancel(order_intent, gate_context_builder, gate_fn, *, now=None):
    """Mints an AuthorizedExecution for a CANCEL -- deliberately does NOT
    check HALT (spec: existing unfilled orders may still be cancelled
    during HALT to reduce risk). Still requires `gate_fn` (order_gate.
    evaluate_cancel_gate()) to pass -- a cancel is not unconditionally
    authorized, only unconditionally un-blocked by HALT specifically."""
    ctx = gate_context_builder()
    gate_fn(ctx)
    token = secrets.token_hex(16)
    _VALID_TOKENS.add(token)
    return AuthorizedExecution(
        internal_order_id=order_intent.internal_order_id, side=order_intent.side,
        action="cancel", token=token, authorized_at=now or datetime.now(timezone.utc),
    )


def consume(authorization, order_intent, *, expected_action):
    """Validates and CONSUMES (single-use) the token -- the ONLY check
    brokers/kis_broker.py relies on before reaching the network. Raises
    UnauthorizedExecutionError if the token was never minted, has
    already been consumed, doesn't match this exact order_intent/side/
    action, or `authorization` isn't even an AuthorizedExecution
    instance (e.g. a caller passed None or a hand-built duck-typed
    stand-in)."""
    if not isinstance(authorization, AuthorizedExecution):
        raise UnauthorizedExecutionError(
            f"no valid AuthorizedExecution supplied for {order_intent.internal_order_id!r}"
        )
    if authorization.internal_order_id != order_intent.internal_order_id:
        raise UnauthorizedExecutionError(
            f"authorization internal_order_id {authorization.internal_order_id!r} does not match "
            f"order_intent {order_intent.internal_order_id!r}"
        )
    if authorization.side != order_intent.side:
        raise UnauthorizedExecutionError(
            f"authorization side {authorization.side!r} does not match order_intent side "
            f"{order_intent.side!r}"
        )
    if authorization.action != expected_action:
        raise UnauthorizedExecutionError(
            f"authorization action {authorization.action!r} does not match expected {expected_action!r}"
        )
    if authorization.token not in _VALID_TOKENS:
        raise UnauthorizedExecutionError(
            f"authorization token for {order_intent.internal_order_id!r} is invalid, expired, or "
            "already used"
        )
    _VALID_TOKENS.discard(authorization.token)
