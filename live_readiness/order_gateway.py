"""CODEX-026/CODEX-029/CODEX-031/CODEX-034: the final common pre-trade
gate for LIVE-mode entry orders -- symbol-identity enforcement, an
authoritative (caller-independent) cash-usage ledger, and durable
ambiguous-failure handling.

Scope decision (recorded here and in DECISION_LOG.md's CODEX-023~034
section): this gate activates only when broker.config.is_live_mode is
True AND side == "buy" -- i.e. it constrains entries in live trading
specifically, never Paper trading. Rationale:

  - The cash-usage/allow-list/FX-rate requirements are meaningless for
    Paper trading (no real money, no real FX conversion) -- Paper's
    existing, extensively tested order path is unaffected and unchanged.
  - Exits are never gated here (mirrors kill_switch_state's own
    ACTIVE-vs-ENTRY_DISABLED asymmetry: an existing position must always
    be closeable, regardless of whether new entries are currently
    allowed).

broker/alpaca_client.py::AlpacaBroker.submit_order() itself runs this
exact gate before ever reaching self.session.request(), so the check
applies at the true final network boundary. paper_strategy_order.
submit_order() also keeps its own copy for defense-in-depth and because
it is broker-agnostic (test doubles that aren't AlpacaBroker instances
don't inherit AlpacaBroker's gate).

CODEX-031/CODEX-034 (percent-of-live-balance sizing, not a fixed pilot
ceiling): there is NO hardcoded absolute KRW budget anywhere in this
module. 30,000 KRW was always only an *example* of the initial balance a
user might fund a live account with for their first live test -- it was
never meant to be baked into the code as a permanent ceiling, and isn't.
Instead:

    max_allocatable_cash = current_available_cash * (cash_usage_percent / 100)
    available_for_new_order = max_allocatable_cash
        - pending_buy_reservations
        - unknown_submission_reservations
        - current_open_position_cost

`current_available_cash` (LiveEntryContext.available_cash_krw) must be a
FRESH, non-stale, non-negative, finite figure the caller obtained from
the broker just before calling -- exactly like `fx_rate_krw_per_usd`/
`fx_rate_as_of` already worked, now mirrored for cash via
`cash_as_of`/`max_cash_age_seconds`. `cash_usage_percent` is a required,
validated (1-100, no NaN/Infinity/bool/string/None) setting; there is no
implicit default -- an operator must state it explicitly, and CHANGING
its deployed value is an operational decision outside this code, not
something a caller can bump per-call to unlock more spending.

CODEX-036: validated in-range alone is NOT the full protection -- a
caller can still simply lie about `available_cash_krw` (declare a real
30,000 KRW account as 3,000,000) or about `cash_usage_percent` (declare
100% when the operator only approved a lower figure), and nothing before
CODEX-036 compared either value against anything authoritative. Two
independent, caller-untightenable ceilings now apply, both via `min()`
(caller can only ever ask for LESS, never more):

  - `cash_usage_percent` is capped against `live_readiness.account_cash.
    TRUSTED_CASH_USAGE_PERCENT_CEILING`, a trusted CODE constant --
    exactly the same pattern as `MAX_CONCURRENT_LIVE_POSITIONS`/
    `MAX_DAILY_LIVE_ENTRIES` below. This is unconditional, whether or not
    a real broker snapshot is available.
  - `available_cash_krw` is capped against the caller-supplied
    `account_cash_snapshot` parameter (a `live_readiness.account_cash.
    AccountCashSnapshot`, the ONLY type `fetch_account_cash_snapshot()`
    can produce -- see that module's docstring for why this gate does
    NOT fetch one itself). This is OPT-IN: if omitted, `available_cash_krw`
    is used as-is, matching the pre-CODEX-036 behavior -- closing this
    half of the gap end-to-end still requires a future production caller
    to actually supply a snapshot.

The three deductions come from live_readiness/entry_reservation_ledger.py
-- a durable SQLite record of every reservation this gate has ever made,
never from caller input. CODEX-034: `unknown_submission_reservations`
exists specifically because a broker call whose response was lost
(timeout/connection reset) is NOT proof the order was never received --
releasing that reservation (the bug CODEX-034 found) would let a retry
double-submit while the authoritative snapshot under-counts real
exposure. Such reservations stay counted until reconciled.

Final quantity: the balance ceiling above is only ONE of up to three
independent caps on order size -- the actual quantity is always the
smallest of all that apply:

    actual_qty = min(balance_based_qty, risk_based_qty, strategy_max_qty)

  - balance_based_qty: what `available_for_new_order` (capped further by
    the optional `max_order_notional_krw`) affords at
    `expected_fill_price_usd`, via calculate_micro_order_quantity().
  - risk_based_qty: only constrained when `stop_price_usd` is given --
    the largest quantity whose (expected_fill_price - stop_price) * qty
    stays within `max_risk_per_trade_krw` (falling back to
    `max_daily_loss_krw`, then to the balance ceiling itself, if unset).
    Unlike the pre-CODEX-034-followup design, an order is no longer
    rejected outright just because its balance-sized quantity implies
    more risk than allowed -- it is resized down to fit, and only
    rejected if that leaves zero affordable shares.
  - strategy_max_qty: an optional caller-supplied ceiling
    (`strategy_max_quantity`) from the strategy's own position-sizing
    logic; unconstrained if omitted.

Whichever cap binds, the reservation notional durably recorded always
matches the ACTUAL resized quantity, never the unconstrained
balance-based one.
"""

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from live_readiness import entry_reservation_ledger as ledger
from live_readiness.account_cash import (
    TRUSTED_CASH_USAGE_PERCENT_CEILING,
    AccountCashSnapshot,
)
from live_readiness.allowlist import is_symbol_allowed
from live_readiness.sizing import (
    STATUS_OK,
    calculate_micro_order_quantity,
)
from live_readiness.trusted_operator_config import (
    get_max_concurrent_live_positions,
    get_max_daily_live_entries,
)
from state_store import db as state_db

# Trusted, code-level ceilings that a caller's context can only ever
# TIGHTEN, never loosen (min()'d against the caller's own value). Unlike
# the cash budget itself (now dynamic, see module docstring), a hard cap
# on concurrent positions and daily entry attempts remains a meaningful,
# caller-independent safety rail regardless of account size. Matches
# docs/live_review/LIMITED_LIVE_30K_KRW_PLAYBOOK.md's recommended initial
# limits (§3): 1 concurrent position, up to 2 daily entries.
#
# Sourced from live_readiness/trusted_operator_config.py (the SOLE source
# for operator policy constants) -- re-exported here under their original
# names for backward compatibility with existing imports/tests.
MAX_CONCURRENT_LIVE_POSITIONS = get_max_concurrent_live_positions()
MAX_DAILY_LIVE_ENTRIES = get_max_daily_live_entries()


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
    """The caller's current, freshly-queried available cash balance (KRW)
    -- a market/account fact like price or FX, not a risk limit. Must be
    paired with `cash_as_of`."""
    cash_usage_percent: float
    """Required operator setting, 1-100. See module docstring -- this is
    NOT caller-loosenable; changing the deployed value is an operational
    decision outside this code."""
    fx_rate_krw_per_usd: Optional[float]
    fx_rate_as_of: Optional[str]  # ISO 8601 timestamp string
    max_position_count: int
    max_daily_entries: int
    cash_as_of: Optional[str] = None  # ISO 8601 timestamp string for available_cash_krw
    stop_price_usd: Optional[float] = None
    max_order_notional_krw: Optional[float] = None  # optional EXTRA per-order cap, on top of the balance-based one
    max_daily_loss_krw: Optional[float] = None  # optional EXTRA stop-loss-risk cap
    max_risk_per_trade_krw: Optional[float] = None  # optional EXTRA per-trade risk cap (see actual_qty formula below)
    strategy_max_quantity: Optional[float] = None  # optional EXTRA strategy-side quantity cap
    min_order_amount_usd: float = 1.0
    fractional_shares_allowed: bool = False
    max_fx_rate_age_seconds: int = 300
    max_cash_age_seconds: int = 300
    now: Optional[datetime] = None  # injectable for tests; defaults to real UTC now


@dataclass
class LiveEntryApproval:
    """Returned by validate_and_size_live_entry() on success. `quantity`
    is the fail-closed-validated whole-or-fractional order size;
    `reservation_id`/`client_order_id` identify the durable reservation
    this approval already placed in live_readiness/
    entry_reservation_ledger.py -- the caller is responsible for calling
    ledger.mark_committed() on definitive broker acceptance,
    ledger.mark_released() on definitive rejection, or
    ledger.mark_submission_unknown() (CODEX-034) on an ambiguous failure
    (see broker/alpaca_client.py for the canonical usage)."""
    quantity: float
    reservation_id: str
    client_order_id: str


def _now(ctx):
    return ctx.now or datetime.now(timezone.utc)


def _validate_cash_usage_percent(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LiveOrderBlockedError(f"cash_usage_percent must be a number, got {value!r}")
    if not math.isfinite(value):
        raise LiveOrderBlockedError(f"cash_usage_percent must be finite, got {value!r}")
    if value <= 0 or value > 100:
        raise LiveOrderBlockedError(f"cash_usage_percent must be in (0, 100], got {value!r}")


def _validate_available_cash(ctx, now):
    if ctx.available_cash_krw is None:
        raise LiveOrderBlockedError("no available cash figure -- refusing to size a live order without one")
    if isinstance(ctx.available_cash_krw, bool) or not isinstance(ctx.available_cash_krw, (int, float)):
        raise LiveOrderBlockedError(f"available cash must be a number, got {ctx.available_cash_krw!r}")
    if not math.isfinite(ctx.available_cash_krw):
        raise LiveOrderBlockedError(f"available cash must be finite, got {ctx.available_cash_krw!r}")
    if ctx.available_cash_krw < 0:
        raise LiveOrderBlockedError(f"available cash must not be negative, got {ctx.available_cash_krw!r}")
    if ctx.available_cash_krw == 0:
        raise LiveOrderBlockedError("no available cash (0 KRW)")
    if ctx.cash_as_of is None:
        raise LiveOrderBlockedError("available cash has no timestamp -- cannot confirm it isn't stale")
    try:
        cash_time = datetime.fromisoformat(ctx.cash_as_of)
    except ValueError:
        raise LiveOrderBlockedError(f"available cash timestamp is not a valid ISO 8601 string: {ctx.cash_as_of!r}")
    if cash_time.tzinfo is None:
        raise LiveOrderBlockedError("available cash timestamp must be timezone-aware")
    age_seconds = (now - cash_time).total_seconds()
    if age_seconds < 0 or age_seconds > ctx.max_cash_age_seconds:
        raise LiveOrderBlockedError(
            f"available cash figure is stale (age={age_seconds:.0f}s, max={ctx.max_cash_age_seconds}s)"
        )


def _validate_optional_positive_number(name, value):
    """CODEX-037: fail-closed validation for every OPTIONAL numeric cap
    (max_order_notional_krw/max_daily_loss_krw/max_risk_per_trade_krw/
    strategy_max_quantity/stop_price_usd). `None` (unset -- no cap) is the
    only value that passes silently; anything else must be a real,
    finite, positive number.

    Without this, a NaN cap slipped through undetected: `NaN <= 0` and
    `NaN > 0` are both False in Python, so guard clauses written as `if
    value <= 0: raise ...` never fire for NaN, and `min(x, float("nan"))`
    is order-dependent (sometimes silently returns `x`, sometimes `nan`)
    -- either way a malformed cap could end up NOT constraining the order
    at all instead of blocking it. Every optional cap must be validated
    explicitly, here, before it is ever used in a comparison."""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LiveOrderBlockedError(f"{name} must be a number, got {value!r}")
    if not math.isfinite(value):
        raise LiveOrderBlockedError(f"{name} must be finite, got {value!r}")
    if value <= 0:
        raise LiveOrderBlockedError(f"{name} must be positive, got {value!r}")


def validate_and_size_live_entry(ctx: LiveEntryContext, order_symbol: str, client_order_id=None,
                                  *, conn=None, account_cash_snapshot=None) -> LiveEntryApproval:
    """Returns a LiveEntryApproval (fail-closed-validated quantity plus a
    durable reservation already placed for it). Raises
    LiveOrderBlockedError with a specific reason string on any
    violation -- never returns a fallback/default quantity, and never
    leaves a dangling reservation on any failure path that happens before
    the reservation itself is made.

    CODEX-029: `order_symbol` -- the symbol the caller is actually about
    to submit an order for -- is a required, separate argument, checked
    byte-for-byte exact against `ctx.symbol` (never normalized).

    CODEX-034: `client_order_id`, if supplied, MUST be the exact id the
    caller is about to send to the broker (e.g. one already reserved via
    paper_strategy_order.try_reserve_order()/order_intent_ledger.py for
    the entry-order dedup trail) -- reusing it here (rather than minting
    a second, different id) keeps the durable reservation and the actual
    broker order correlated under one identity. If omitted (a caller that
    doesn't pre-generate one, e.g. a direct AlpacaBroker.submit_order()
    call), this function mints one itself; the approval's
    `client_order_id` field always reports whichever id was actually
    used, and the caller MUST send that exact id to the broker.

    CODEX-031/034: `conn`, if supplied, is the SQLite connection used for
    the authoritative cash-usage snapshot and reservation; if omitted, a
    fresh connection is opened. The entire read-snapshot-then-reserve
    span is held under entry_reservation_ledger.reservation_lock() so two
    concurrent entry attempts can never both observe "cash available"
    before either has actually reserved it. See module docstring for the
    percent-of-balance formula.

    CODEX-036: `account_cash_snapshot`, if supplied, MUST be a
    `live_readiness.account_cash.AccountCashSnapshot` -- the ONLY type
    that can be constructed by `fetch_account_cash_snapshot()` (a real
    `broker.get_account()` query), never a bare number. Its `cash_krw`
    becomes a CEILING on -- never a replacement floor for --
    `ctx.available_cash_krw`: `effective_cash = min(ctx.available_cash_krw,
    snapshot.cash_krw)`. A caller-declared cash figure can therefore never
    exceed what the broker's own account actually reported as of the
    snapshot's `as_of` timestamp (checked for staleness against
    `ctx.max_cash_age_seconds`, same as the FX/cash timestamps above).
    This function does NOT itself fetch a snapshot -- doing so here would
    require a live network call on every validation attempt, including
    from Paper-mode/dry-run/unit-test callers that must never touch a
    real broker. Fetching is the CALLER's responsibility, at whatever
    point in its own flow live network calls are actually permitted (this
    codebase's pre-live safety gate currently disables ALL live-mode
    broker calls regardless of dry-run status -- see
    broker_config.py::validate_order_allowed() -- so no current call site
    can construct a real snapshot yet; this parameter exists so that,
    once live trading is eventually approved, doing so closes the gap
    immediately without another code change here). If omitted, `ctx.
    available_cash_krw` is used as-is, unchanged from prior behavior.
    `cash_usage_percent` is capped the same way against the
    caller-untightenable `TRUSTED_CASH_USAGE_PERCENT_CEILING` code
    constant regardless of whether a snapshot is supplied.
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

    _validate_cash_usage_percent(ctx.cash_usage_percent)
    _validate_available_cash(ctx, now)

    # CODEX-037: validate every optional numeric cap up front, before any
    # of them is ever used in a min()/comparison below.
    _validate_optional_positive_number("max_order_notional_krw", ctx.max_order_notional_krw)
    _validate_optional_positive_number("max_daily_loss_krw", ctx.max_daily_loss_krw)
    _validate_optional_positive_number("max_risk_per_trade_krw", ctx.max_risk_per_trade_krw)
    _validate_optional_positive_number("strategy_max_quantity", ctx.strategy_max_quantity)
    _validate_optional_positive_number("stop_price_usd", ctx.stop_price_usd)

    own_conn = conn is None
    active_conn = conn if conn is not None else state_db.open_db()
    try:
        with ledger.reservation_lock():
            snapshot = ledger.build_snapshot(active_conn, now=now)

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

            # CODEX-036: cash_usage_percent can only ever be tightened by
            # the trusted code ceiling, never loosened by the caller --
            # identical pattern to MAX_CONCURRENT_LIVE_POSITIONS/
            # MAX_DAILY_LIVE_ENTRIES above.
            effective_cash_usage_percent = min(ctx.cash_usage_percent, TRUSTED_CASH_USAGE_PERCENT_CEILING)

            effective_available_cash_krw = ctx.available_cash_krw
            if account_cash_snapshot is not None:
                if not isinstance(account_cash_snapshot, AccountCashSnapshot):
                    raise LiveOrderBlockedError(
                        f"account_cash_snapshot must be an AccountCashSnapshot, "
                        f"got {type(account_cash_snapshot).__name__}"
                    )
                snapshot_age_seconds = (now - account_cash_snapshot.as_of).total_seconds()
                if snapshot_age_seconds < 0 or snapshot_age_seconds > ctx.max_cash_age_seconds:
                    raise LiveOrderBlockedError(
                        f"account cash snapshot is stale (age={snapshot_age_seconds:.0f}s, "
                        f"max={ctx.max_cash_age_seconds}s)"
                    )
                # A caller-declared figure can only ever be reduced by the
                # broker's own real balance, never inflated past it -- the
                # exact CODEX-036 counterexample (declared 3,000,000 vs
                # real 30,000) is closed by this min().
                effective_available_cash_krw = min(ctx.available_cash_krw, account_cash_snapshot.cash_krw)

            max_allocatable_cash_krw = effective_available_cash_krw * (effective_cash_usage_percent / 100.0)
            available_for_new_order_krw = max_allocatable_cash_krw - snapshot.total_committed_krw
            if available_for_new_order_krw <= 0:
                raise LiveOrderBlockedError(
                    f"no cash available for a new order (allocatable={max_allocatable_cash_krw:.0f} KRW, "
                    f"already committed={snapshot.total_committed_krw:.0f} KRW: "
                    f"pending={snapshot.pending_buy_reservations_krw:.0f}, "
                    f"unknown={snapshot.unknown_submission_reservations_krw:.0f}, "
                    f"open_positions={snapshot.current_open_position_cost_krw:.0f})"
                )

            budget_krw = available_for_new_order_krw
            if ctx.max_order_notional_krw is not None:
                budget_krw = min(budget_krw, ctx.max_order_notional_krw)
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

            # No separate "computed notional exceeds budget_krw" check here:
            # budget_krw was already capped above, and
            # calculate_micro_order_quantity() never returns a cost
            # exceeding the budget it was given, so that violation is
            # structurally impossible to reach at this point.

            # actual_qty = min(balance_based_qty, risk_based_qty, strategy_max_qty)
            # -- see module docstring. balance_based_qty is `sizing.quantity`,
            # already fail-closed-validated above; risk_based_qty and
            # strategy_max_qty only ever narrow it further, never widen it.
            balance_based_qty = sizing.quantity
            actual_qty = balance_based_qty

            if ctx.stop_price_usd is not None:
                if ctx.stop_price_usd <= 0:
                    raise LiveOrderBlockedError(f"invalid stop_price_usd {ctx.stop_price_usd!r}")
                risk_per_share_usd = ctx.expected_fill_price_usd - ctx.stop_price_usd
                if risk_per_share_usd <= 0:
                    raise LiveOrderBlockedError(
                        f"stop_price_usd {ctx.stop_price_usd!r} is not below expected_fill_price_usd "
                        f"{ctx.expected_fill_price_usd!r} -- no defined risk"
                    )
                effective_max_risk_krw = max_allocatable_cash_krw
                if ctx.max_daily_loss_krw is not None:
                    effective_max_risk_krw = min(effective_max_risk_krw, ctx.max_daily_loss_krw)
                if ctx.max_risk_per_trade_krw is not None:
                    effective_max_risk_krw = min(effective_max_risk_krw, ctx.max_risk_per_trade_krw)
                risk_based_qty = effective_max_risk_krw / (risk_per_share_usd * ctx.fx_rate_krw_per_usd)
                if not ctx.fractional_shares_allowed:
                    risk_based_qty = math.floor(risk_based_qty)
                actual_qty = min(actual_qty, risk_based_qty)

            if ctx.strategy_max_quantity is not None:
                strategy_max_qty = ctx.strategy_max_quantity
                if not ctx.fractional_shares_allowed:
                    strategy_max_qty = math.floor(strategy_max_qty)
                actual_qty = min(actual_qty, strategy_max_qty)

            if actual_qty <= 0 or (not ctx.fractional_shares_allowed and actual_qty < 1):
                raise LiveOrderBlockedError(
                    f"sizing blocked: risk/strategy caps leave no affordable quantity "
                    f"(balance-based={balance_based_qty!r}, resized to={actual_qty!r})"
                )

            reservation_notional_krw = actual_qty * ctx.expected_fill_price_usd * ctx.fx_rate_krw_per_usd
            if reservation_notional_krw / ctx.fx_rate_krw_per_usd < ctx.min_order_amount_usd:
                raise LiveOrderBlockedError(
                    f"sizing blocked: risk/strategy-resized order value "
                    f"${reservation_notional_krw / ctx.fx_rate_krw_per_usd:.2f} is below the "
                    f"${ctx.min_order_amount_usd:.2f} minimum order amount"
                )

            # Every check passed -- reserve the ACTUAL (possibly
            # risk/strategy-resized) notional durably, still under the
            # same lock, before returning control to the caller (who
            # calls the broker next). CODEX-034: the client_order_id
            # (caller-supplied if given, minted here otherwise) is stored
            # NOW, before any broker call, so an ambiguous failure can
            # later be reconciled by looking this exact order up at the
            # broker.
            resolved_client_order_id = client_order_id or f"liveentry-{ctx.symbol}-{uuid.uuid4().hex[:12]}"
            reservation_id = ledger.reserve(
                active_conn, ctx.symbol, reservation_notional_krw, resolved_client_order_id,
            )

        return LiveEntryApproval(quantity=actual_qty, reservation_id=reservation_id,
                                  client_order_id=resolved_client_order_id)
    finally:
        if own_conn:
            active_conn.close()
