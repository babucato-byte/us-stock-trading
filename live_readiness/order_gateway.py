"""CODEX-026/CODEX-029: the final common pre-trade gate for LIVE-mode
entry orders, including symbol-identity enforcement between the approved
context and the actual order submitted.

Scope decision (recorded here and in DECISION_LOG.md's CODEX-023~030
section): this gate activates only when broker.config.is_live_mode is
True AND side == "buy" -- i.e. it constrains entries in live trading
specifically, never Paper trading. Rationale:

  - The 30,000 KRW budget, symbol allow-list, and FX-rate requirements
    are meaningless for Paper trading (no real money, no real FX
    conversion) -- Paper's existing, extensively tested order path
    (scalping_watchlist's broader universe, order_safety.py's existing
    limits) is unaffected and unchanged, so this fix carries none of the
    regression risk a Paper-path change would.
  - Exits are never gated here (mirrors kill_switch_state's own
    ACTIVE-vs-ENTRY_DISABLED asymmetry, already established throughout
    this project: an existing position must always be closeable,
    regardless of whether new entries are currently allowed).

CODEX-026's original residual risk -- "a caller that bypasses
paper_strategy_order.submit_order() and calls broker.submit_order()
directly is NOT covered by this Python-level gate" -- is now closed:
broker/alpaca_client.py::AlpacaBroker.submit_order() itself runs this
exact gate (validate_and_size_live_entry(), including the CODEX-029
symbol-identity check below) before ever reaching self.session.request(),
so the check applies at the true final network boundary regardless of
which caller reaches it. paper_strategy_order.submit_order() also keeps
its own copy of this gate for defense-in-depth and because it is broker-
agnostic (test doubles that aren't AlpacaBroker instances don't inherit
AlpacaBroker's gate) -- running it twice on a real AlpacaBroker call is
redundant but harmless, since validate_and_size_live_entry() is a pure
function of its inputs with no side effects.

Every check here fails closed: a missing/unconfigured input (no FX rate,
no allow-list, no available-cash figure, no order_symbol, a symbol that
doesn't match the context) blocks the order, it never defaults to
"allow."
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from live_readiness.allowlist import is_symbol_allowed
from live_readiness.sizing import (
    STATUS_OK,
    calculate_micro_order_quantity,
)


class LiveOrderBlockedError(Exception):
    """Raised by validate_and_size_live_entry() on any failed pre-trade
    check. Callers (paper_strategy_order.submit_order()) must convert
    this into a blocked-order response, never catch-and-submit-anyway."""


@dataclass
class LiveEntryContext:
    symbol: str
    expected_fill_price_usd: float
    allow_list: List[str]
    available_cash_krw: float
    fx_rate_krw_per_usd: Optional[float]
    fx_rate_as_of: Optional[str]  # ISO 8601 timestamp string
    max_order_notional_krw: float
    max_daily_loss_krw: float
    max_position_count: int
    current_open_position_count: int
    max_daily_entries: int
    today_entry_count: int
    stop_price_usd: Optional[float] = None
    min_order_amount_usd: float = 1.0
    fractional_shares_allowed: bool = False
    max_fx_rate_age_seconds: int = 300
    now: Optional[datetime] = None  # injectable for tests; defaults to real UTC now


def _now(ctx):
    return ctx.now or datetime.now(timezone.utc)


def validate_and_size_live_entry(ctx: LiveEntryContext, order_symbol: str) -> int:
    """Returns the fail-closed-validated whole-share quantity to submit.
    Raises LiveOrderBlockedError with a specific reason string on any
    violation -- never returns a fallback/default quantity.

    CODEX-029: `order_symbol` -- the symbol the caller is actually about
    to submit an order for -- is now a required, separate argument, not
    implicitly trusted to equal `ctx.symbol`. Before this, only
    `ctx.symbol` was checked against the allow-list; nothing compared it
    to the real order about to be placed, so an approved context for one
    symbol (e.g. AAPL) could be paired with a submit_order() call for any
    other symbol (e.g. TSLA) and the mismatch would never be caught. The
    match required here is byte-for-byte exact -- deliberately NOT
    case/whitespace-normalized like the allow-list membership check below
    is, so a case or whitespace mutation between context and order is
    itself treated as a mismatch and blocked, never silently equated.
    """
    if not isinstance(order_symbol, str) or not order_symbol:
        raise LiveOrderBlockedError(f"order symbol is empty or not a string: {order_symbol!r}")
    if not isinstance(ctx.symbol, str) or not ctx.symbol:
        raise LiveOrderBlockedError(f"live entry context symbol is empty or not a string: {ctx.symbol!r}")
    if ctx.symbol != order_symbol:
        raise LiveOrderBlockedError(
            f"order symbol {order_symbol!r} does not match live entry context symbol {ctx.symbol!r}"
        )

    if not is_symbol_allowed(ctx.symbol, ctx.allow_list):
        raise LiveOrderBlockedError(f"symbol {ctx.symbol!r} is not on the allow-list")

    if ctx.current_open_position_count >= ctx.max_position_count:
        raise LiveOrderBlockedError(
            f"max concurrent positions reached ({ctx.current_open_position_count}/{ctx.max_position_count})"
        )

    if ctx.today_entry_count >= ctx.max_daily_entries:
        raise LiveOrderBlockedError(
            f"max daily entries reached ({ctx.today_entry_count}/{ctx.max_daily_entries})"
        )

    if ctx.fx_rate_krw_per_usd is None:
        raise LiveOrderBlockedError("no FX rate available -- refusing to size a live order without one")
    if not isinstance(ctx.fx_rate_krw_per_usd, (int, float)) or isinstance(ctx.fx_rate_krw_per_usd, bool) \
            or not math.isfinite(ctx.fx_rate_krw_per_usd) or ctx.fx_rate_krw_per_usd <= 0:
        raise LiveOrderBlockedError(f"invalid FX rate {ctx.fx_rate_krw_per_usd!r}")
    if ctx.fx_rate_as_of is None:
        raise LiveOrderBlockedError("FX rate has no timestamp -- cannot confirm it isn't stale")
    try:
        fx_time = datetime.fromisoformat(ctx.fx_rate_as_of)
    except ValueError:
        raise LiveOrderBlockedError(f"FX rate timestamp is not a valid ISO 8601 string: {ctx.fx_rate_as_of!r}")
    if fx_time.tzinfo is None:
        raise LiveOrderBlockedError("FX rate timestamp must be timezone-aware")
    age_seconds = (_now(ctx) - fx_time).total_seconds()
    if age_seconds < 0 or age_seconds > ctx.max_fx_rate_age_seconds:
        raise LiveOrderBlockedError(
            f"FX rate is stale (age={age_seconds:.0f}s, max={ctx.max_fx_rate_age_seconds}s)"
        )

    if ctx.available_cash_krw <= 0:
        raise LiveOrderBlockedError(f"no available cash ({ctx.available_cash_krw!r} KRW)")

    budget_krw = min(ctx.available_cash_krw, ctx.max_order_notional_krw)
    if budget_krw <= 0:
        raise LiveOrderBlockedError(
            f"max_order_notional_krw ({ctx.max_order_notional_krw!r}) leaves no budget for this order"
        )

    sizing = calculate_micro_order_quantity(
        budget_krw, ctx.fx_rate_krw_per_usd, ctx.expected_fill_price_usd,
        min_order_amount_usd=ctx.min_order_amount_usd,
        fractional_shares_allowed=ctx.fractional_shares_allowed,
    )
    if sizing.status != STATUS_OK:
        raise LiveOrderBlockedError(f"sizing blocked: {sizing.status} ({sizing.reason})")

    # No separate "computed notional exceeds max_order_notional_krw" check
    # here: budget_krw was already capped to min(available_cash_krw,
    # max_order_notional_krw) above, and calculate_micro_order_quantity()
    # never returns a cost exceeding the budget it was given, so that
    # violation is structurally impossible to reach at this point -- an
    # unreachable check would be untested dead code, not a real guarantee.

    if ctx.stop_price_usd is not None:
        if ctx.stop_price_usd <= 0:
            raise LiveOrderBlockedError(f"invalid stop_price_usd {ctx.stop_price_usd!r}")
        risk_per_share_usd = ctx.expected_fill_price_usd - ctx.stop_price_usd
        if risk_per_share_usd <= 0:
            raise LiveOrderBlockedError(
                f"stop_price_usd {ctx.stop_price_usd!r} is not below expected_fill_price_usd "
                f"{ctx.expected_fill_price_usd!r} -- no defined risk"
            )
        risk_amount_krw = risk_per_share_usd * sizing.quantity * ctx.fx_rate_krw_per_usd
        if risk_amount_krw > ctx.max_daily_loss_krw:
            raise LiveOrderBlockedError(
                f"stop-loss risk {risk_amount_krw:.0f} KRW exceeds max_daily_loss_krw {ctx.max_daily_loss_krw!r}"
            )

    return sizing.quantity
