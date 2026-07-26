"""CODEX-026/CODEX-029/CODEX-031: the final common pre-trade gate for
LIVE-mode entry orders, including symbol-identity enforcement and an
authoritative (caller-independent) 30,000 KRW pilot budget/count check.

Scope decision (recorded here and in DECISION_LOG.md's CODEX-023~033
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
directly is NOT covered by this Python-level gate" -- is closed:
broker/alpaca_client.py::AlpacaBroker.submit_order() itself runs this
exact gate before ever reaching self.session.request().

CODEX-031: `LiveEntryContext.max_order_notional_krw`,
`max_daily_loss_krw`, `max_position_count`, `current_open_position_count`,
`max_daily_entries`, and `today_entry_count` are no longer trusted as
the authoritative source of "how much budget/how many entries/positions
are left" -- a caller reporting inflated limits or stale counters (a
context claiming a 3,000,000 KRW budget, say) used to be approved
outright. This module now computes the actually-consumed portion of the
pilot's fixed 30,000 KRW budget, today's entry count, and open live
position count from live_readiness/entry_reservation_ledger.py's durable
SQLite ledger, and intersects the caller's requested limits with trusted,
code-level constants the caller cannot raise (PILOT_TOTAL_BUDGET_KRW,
MAX_CONCURRENT_LIVE_POSITIONS, MAX_DAILY_LIVE_ENTRIES) -- the caller's
context can only ever tighten these further, never loosen them. A
caller-supplied `available_cash_krw` remains an accepted input (like
price and FX rate) since it reflects real-time broker account state this
codebase has no independent way to recompute locally; what changed is
that it can no longer, by itself, unlock spending beyond the trusted
pilot ceiling.

Every check here fails closed: a missing/unconfigured input (no FX rate,
no allow-list, no available-cash figure, no order_symbol, a symbol that
doesn't match the context, no budget remaining) blocks the order, it
never defaults to "allow."
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from live_readiness import entry_reservation_ledger as ledger
from live_readiness.allowlist import is_symbol_allowed
from live_readiness.sizing import (
    STATUS_OK,
    calculate_micro_order_quantity,
)
from state_store import db as state_db

# Trusted, code-level pilot limits (DECISION_LOG.md CODEX-023~033 section,
# decision on CODEX-031 scope). None of these are settable via
# LiveEntryContext or any other caller input -- only a code change can
# raise them. Matches docs/live_review/LIMITED_LIVE_30K_KRW_PLAYBOOK.md's
# recommended initial limits (§3): 30,000 KRW total, 1 concurrent
# position, up to 2 daily entries.
PILOT_TOTAL_BUDGET_KRW = 30_000
MAX_CONCURRENT_LIVE_POSITIONS = 1
MAX_DAILY_LIVE_ENTRIES = 2


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


@dataclass
class LiveEntryApproval:
    """Returned by validate_and_size_live_entry() on success. `quantity`
    is the fail-closed-validated whole-share order size; `reservation_id`
    identifies the durable budget hold this approval already placed in
    live_readiness/entry_reservation_ledger.py -- the caller is
    responsible for calling ledger.mark_committed() once the broker
    accepts the order, or ledger.mark_released() if it doesn't (see
    paper_strategy_order.submit_order() / broker/alpaca_client.py for the
    canonical usage)."""
    quantity: int
    reservation_id: str


def _now(ctx):
    return ctx.now or datetime.now(timezone.utc)


def validate_and_size_live_entry(ctx: LiveEntryContext, order_symbol: str, *, conn=None) -> LiveEntryApproval:
    """Returns a LiveEntryApproval (fail-closed-validated quantity plus a
    durable reservation already placed for it). Raises
    LiveOrderBlockedError with a specific reason string on any
    violation -- never returns a fallback/default quantity, and never
    leaves a dangling reservation on the failure path (checks that can
    still fail happen before the reservation is made; see below).

    CODEX-029: `order_symbol` -- the symbol the caller is actually about
    to submit an order for -- is a required, separate argument, not
    implicitly trusted to equal `ctx.symbol`. The match required is
    byte-for-byte exact -- deliberately NOT case/whitespace-normalized
    like the allow-list membership check below is, so a case or
    whitespace mutation between context and order is itself treated as a
    mismatch and blocked, never silently equated.

    CODEX-031: `conn`, if supplied, is the SQLite connection used for the
    authoritative budget/count snapshot and reservation (reused so a
    caller already holding one connection open doesn't have to open a
    second); if omitted, a fresh connection is opened. The entire
    read-snapshot-then-reserve span is held under
    entry_reservation_ledger.reservation_lock() so two concurrent entry
    attempts can never both observe "budget available" before either has
    actually reserved it.
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
    now = _now(ctx)
    age_seconds = (now - fx_time).total_seconds()
    if age_seconds < 0 or age_seconds > ctx.max_fx_rate_age_seconds:
        raise LiveOrderBlockedError(
            f"FX rate is stale (age={age_seconds:.0f}s, max={ctx.max_fx_rate_age_seconds}s)"
        )

    if ctx.available_cash_krw <= 0:
        raise LiveOrderBlockedError(f"no available cash ({ctx.available_cash_krw!r} KRW)")

    own_conn = conn is None
    active_conn = conn if conn is not None else state_db.open_db()
    try:
        with ledger.reservation_lock():
            snapshot = ledger.build_snapshot(active_conn, now=now)

            # CODEX-031: the caller's requested limits can only ever
            # TIGHTEN these trusted ceilings, never loosen them --
            # min(), not the caller's raw value.
            effective_max_position_count = min(ctx.max_position_count, MAX_CONCURRENT_LIVE_POSITIONS)
            if snapshot.active_position_count >= effective_max_position_count:
                raise LiveOrderBlockedError(
                    f"max concurrent positions reached "
                    f"({snapshot.active_position_count}/{effective_max_position_count}, authoritative)"
                )

            effective_max_daily_entries = min(ctx.max_daily_entries, MAX_DAILY_LIVE_ENTRIES)
            if snapshot.today_entry_count >= effective_max_daily_entries:
                raise LiveOrderBlockedError(
                    f"max daily entries reached "
                    f"({snapshot.today_entry_count}/{effective_max_daily_entries}, authoritative)"
                )

            effective_ceiling_krw = min(ctx.max_order_notional_krw, PILOT_TOTAL_BUDGET_KRW)
            remaining_pilot_budget_krw = PILOT_TOTAL_BUDGET_KRW - snapshot.active_notional_krw
            if remaining_pilot_budget_krw <= 0:
                raise LiveOrderBlockedError(
                    f"pilot budget exhausted (active={snapshot.active_notional_krw:.0f} KRW / "
                    f"{PILOT_TOTAL_BUDGET_KRW} KRW ceiling, authoritative)"
                )

            budget_krw = min(ctx.available_cash_krw, effective_ceiling_krw, remaining_pilot_budget_krw)
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

            # No separate "computed notional exceeds effective_ceiling_krw"
            # check here: budget_krw was already capped to
            # min(available_cash_krw, effective_ceiling_krw,
            # remaining_pilot_budget_krw) above, and
            # calculate_micro_order_quantity() never returns a cost
            # exceeding the budget it was given, so that violation is
            # structurally impossible to reach at this point.

            effective_max_daily_loss_krw = min(ctx.max_daily_loss_krw, PILOT_TOTAL_BUDGET_KRW)
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
                if risk_amount_krw > effective_max_daily_loss_krw:
                    raise LiveOrderBlockedError(
                        f"stop-loss risk {risk_amount_krw:.0f} KRW exceeds max_daily_loss_krw "
                        f"{effective_max_daily_loss_krw!r} (authoritative ceiling)"
                    )

            # Every check passed -- reserve the notional durably, still
            # under the same lock, before returning control to the
            # caller (who calls the broker next). CODEX-031's atomicity
            # requirement: no other concurrent attempt can observe stale
            # "budget available" between this snapshot and this reservation.
            reservation_notional_krw = (sizing.estimated_cost_usd or 0.0) * ctx.fx_rate_krw_per_usd
            reservation_id = ledger.reserve(active_conn, ctx.symbol, reservation_notional_krw)

        return LiveEntryApproval(quantity=sizing.quantity, reservation_id=reservation_id)
    finally:
        if own_conn:
            active_conn.close()
