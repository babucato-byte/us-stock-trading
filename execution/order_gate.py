"""Order Gate -- the single central safety gate every live order must
pass (spec §12). Pure decision logic: every fact it checks (KIS price,
KIS balance, KIS position, reconciliation status, kill switch state,
allow-list, deployed/validated commit) is supplied already-fetched by
the caller (execution/execution_engine.py) via the two context
dataclasses below -- this module makes zero network/DB calls itself, so
its checks are trivially unit-testable and their ORDER is exactly the
order spec §12 lists (fail on the first violated check, never continue
past it).

Buy and sell are two entirely separate gates (`evaluate_buy_gate()` /
`evaluate_sell_gate()`) because their failure conditions don't overlap
(spec §12's two separate check lists) -- there is no shared "generic
order gate" that would blur the two.
"""

import math
from dataclasses import dataclass
from datetime import datetime
from typing import FrozenSet, Optional

from domain.instrument import Instrument
from domain.order_intent import OrderIntent
from domain.signal import Signal
from execution.secret_redaction import mask_account_number


class OrderGateBlockedError(Exception):
    """Raised with the specific reason the FIRST failing check produced.
    Callers must treat this as a hard block -- zero broker calls happen
    after this is raised (execution_engine.py never calls
    KISBroker.submit_order() unless both gates return cleanly)."""


@dataclass(frozen=True)
class BuyGateContext:
    execution_broker: str
    live_order_enabled: bool
    entry_disabled: bool
    validated_commit: str
    deployed_commit: str
    kis_account_no: str
    allowed_account_no: str
    order_intent: OrderIntent
    instrument: Instrument
    signal: Signal
    is_regular_session: bool
    kis_price_usd: float
    max_price_deviation_percent: float
    usd_orderable_cash: float
    has_open_order_for_symbol: bool
    has_order_for_signal_id: bool
    allowed_symbols: FrozenSet[str]
    reconciliation_ok: bool
    has_unknown_order: bool
    now: datetime


@dataclass(frozen=True)
class SellGateContext:
    execution_broker: str
    live_order_enabled: bool
    order_intent: OrderIntent
    instrument: Instrument
    kis_position_quantity: int
    position_source: str
    has_existing_sell_order_for_symbol: bool
    reconciliation_ok: bool
    has_unknown_order: bool


def _is_finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def evaluate_buy_gate(ctx: BuyGateContext) -> bool:
    """Returns True if the buy order may proceed; raises
    OrderGateBlockedError with the specific reason otherwise. Checks run
    in the exact order spec §12 lists."""
    if ctx.execution_broker != "kis":
        raise OrderGateBlockedError(f"execution broker must be 'kis', got {ctx.execution_broker!r}")
    if not ctx.live_order_enabled:
        raise OrderGateBlockedError("live order flag is not enabled")
    if ctx.entry_disabled:
        raise OrderGateBlockedError("ENTRY_DISABLED is set -- new entries are blocked")
    if ctx.validated_commit != ctx.deployed_commit:
        raise OrderGateBlockedError(
            f"validated commit {ctx.validated_commit!r} does not match deployed commit "
            f"{ctx.deployed_commit!r}"
        )
    if ctx.kis_account_no != ctx.allowed_account_no:
        raise OrderGateBlockedError(
            f"KIS account {mask_account_number(ctx.kis_account_no)!r} is not the allowed account "
            f"{mask_account_number(ctx.allowed_account_no)!r}"
        )
    quantity = ctx.order_intent.quantity
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise OrderGateBlockedError(f"quantity must be an integer, got {quantity!r}")
    if quantity < 1:
        raise OrderGateBlockedError(f"quantity must be >= 1, got {quantity!r}")
    if ctx.order_intent.order_type != "limit":
        raise OrderGateBlockedError(f"only limit orders are permitted, got {ctx.order_intent.order_type!r}")
    if not ctx.is_regular_session:
        raise OrderGateBlockedError("not currently in the US regular trading session")
    if ctx.signal.is_expired(now=ctx.now):
        raise OrderGateBlockedError(f"signal {ctx.signal.signal_id!r} has expired")
    if not _is_finite_number(ctx.kis_price_usd) or ctx.kis_price_usd <= 0:
        raise OrderGateBlockedError(f"KIS price is invalid: {ctx.kis_price_usd!r}")
    deviation_percent = abs(ctx.kis_price_usd - ctx.signal.signal_price) / ctx.signal.signal_price * 100.0
    if deviation_percent > ctx.max_price_deviation_percent:
        raise OrderGateBlockedError(
            f"KIS price {ctx.kis_price_usd!r} deviates {deviation_percent:.4f}% from signal price "
            f"{ctx.signal.signal_price!r}, exceeding the {ctx.max_price_deviation_percent!r}% limit "
            "-- order cancelled, no chase-buy"
        )
    order_notional_usd = quantity * ctx.order_intent.limit_price
    if not _is_finite_number(ctx.usd_orderable_cash) or ctx.usd_orderable_cash < order_notional_usd:
        raise OrderGateBlockedError(
            f"insufficient KIS orderable cash: need ${order_notional_usd:.2f}, "
            f"have ${ctx.usd_orderable_cash!r}"
        )
    if ctx.has_open_order_for_symbol:
        raise OrderGateBlockedError(f"an open (unfilled) order already exists for {ctx.order_intent.symbol!r}")
    if ctx.has_order_for_signal_id:
        raise OrderGateBlockedError(f"an order already exists for signal_id {ctx.signal.signal_id!r}")
    if ctx.order_intent.symbol not in ctx.allowed_symbols:
        raise OrderGateBlockedError(f"{ctx.order_intent.symbol!r} is not in the allowed-symbols list")
    if not ctx.instrument.is_order_eligible:
        raise OrderGateBlockedError(
            f"{ctx.instrument.symbol!r} is not order-eligible (leveraged/inverse/OTC/not tradable)"
        )
    if not ctx.reconciliation_ok:
        raise OrderGateBlockedError("reconciliation is not OK -- new buys blocked until resolved")
    if ctx.has_unknown_order:
        raise OrderGateBlockedError("an UNKNOWN-state order exists -- new buys blocked until reconciled")
    return True


def evaluate_sell_gate(ctx: SellGateContext) -> bool:
    """Returns True if the sell order may proceed; raises
    OrderGateBlockedError otherwise. Unlike the buy gate, sells are
    NEVER blocked by kill-switch-style entry gates (an existing position
    must always be closeable) -- see live_readiness/order_gateway.py's
    module docstring for the same asymmetry already established for the
    Alpaca/Paper path."""
    if ctx.execution_broker != "kis":
        raise OrderGateBlockedError(f"execution broker must be 'kis', got {ctx.execution_broker!r}")
    if not ctx.live_order_enabled:
        raise OrderGateBlockedError("live order flag is not enabled")
    if ctx.position_source != "kis":
        raise OrderGateBlockedError(f"position source must be 'kis', got {ctx.position_source!r}")
    if ctx.kis_position_quantity <= 0:
        raise OrderGateBlockedError(f"no KIS position exists for {ctx.order_intent.symbol!r}")
    quantity = ctx.order_intent.quantity
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise OrderGateBlockedError(f"sell quantity must be a positive integer, got {quantity!r}")
    if quantity > ctx.kis_position_quantity:
        raise OrderGateBlockedError(
            f"sell quantity {quantity!r} exceeds actual KIS position quantity "
            f"{ctx.kis_position_quantity!r}"
        )
    if ctx.has_existing_sell_order_for_symbol:
        raise OrderGateBlockedError(
            f"a sell order already exists for {ctx.order_intent.symbol!r} -- duplicate liquidation blocked"
        )
    if not ctx.reconciliation_ok:
        raise OrderGateBlockedError("reconciliation is not OK -- new sells blocked until resolved")
    if ctx.has_unknown_order:
        raise OrderGateBlockedError("an UNKNOWN-state order exists -- new sells blocked until reconciled")
    return True


@dataclass(frozen=True)
class CancelGateContext:
    execution_broker: str
    broker_order_id: Optional[str]
    is_actually_open: bool
    kis_account_no: str
    allowed_account_no: str
    symbol: str
    has_cancel_already_in_flight: bool


def evaluate_cancel_gate(ctx: CancelGateContext) -> bool:
    """CODEX-043: cancels are NEVER blocked by HALT (spec §3/§20 -- an
    existing unfilled order may always be cancelled to reduce risk), but
    they are not unconditionally authorized either -- these checks still
    apply. `execution/authorization.py::authorize_cancel()` is the only
    caller; HALT itself is checked there (by deliberately NOT checking
    it), not here."""
    if ctx.execution_broker != "kis":
        raise OrderGateBlockedError(f"execution broker must be 'kis', got {ctx.execution_broker!r}")
    if not ctx.broker_order_id:
        raise OrderGateBlockedError("no broker_order_id supplied -- cannot cancel an unknown order")
    if not ctx.is_actually_open:
        raise OrderGateBlockedError(
            f"broker_order_id {ctx.broker_order_id!r} is not an actual open KIS order -- refusing to cancel"
        )
    if ctx.kis_account_no != ctx.allowed_account_no:
        raise OrderGateBlockedError(
            f"KIS account {mask_account_number(ctx.kis_account_no)!r} is not the allowed account "
            f"{mask_account_number(ctx.allowed_account_no)!r}"
        )
    if ctx.has_cancel_already_in_flight:
        raise OrderGateBlockedError(
            f"a cancel is already in flight for {ctx.broker_order_id!r} -- duplicate cancel blocked"
        )
    return True
