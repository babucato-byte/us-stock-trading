"""Pre-trade watchlist affordability filter (percent-of-balance model).

Companion to order_gateway.py's final broker-boundary gate: this module
answers a DIFFERENT question, earlier in the pipeline -- "of today's
strategy/volume/liquidity candidates, which ones could the account
actually afford to enter right now, given the live cash balance?" -- so
candidates that can never be filled are never even carried forward to
strategy-entry monitoring.

This is a pure calculation module (no SQLite, no broker calls, no file
I/O) -- like live_readiness/order_gateway.py and sizing.py, it is a
building block. It is NOT wired into daily_candidate_scanner.py or
scalping_watchlist/pipeline.py in this change; splicing a live-cash
filter into the already-reviewed Paper-mode candidate pipeline is a
separate, explicit decision (see DECISION_LOG.md's CODEX-034 section) --
this module only needs to exist and be correct on its own for now.

Formula (identical to order_gateway.py -- see that module's docstring for
the full rationale; duplicated here only as plain dataclass math, not
re-implemented against the durable reservation ledger, since a caller
scanning N candidates should compute the account-wide ceiling ONCE and
reuse it across every candidate, not re-touch SQLite per symbol):

    max_allocatable_cash = current_available_cash * (cash_usage_percent / 100)
    available_for_new_order = max_allocatable_cash
        - pending_buy_reservations
        - unknown_submission_reservations
        - current_open_position_cost

A symbol is NEVER excluded merely because one whole share costs more than
the available balance -- if `fractionable` is True and the minimum order
amount is still affordable, it stays a candidate (AFFORDABLE_FRACTIONAL).
Only `fractionable=False` symbols are excluded for that specific reason
(NOT_FRACTIONABLE, distinct from a plain INSUFFICIENT_BALANCE so a caller
can tell "no live budget at all" apart from "budget exists, but this
specific illiquid-fractional symbol can't be split").
"""

import math
from dataclasses import dataclass, field
from typing import List, Optional

from live_readiness.sizing import (
    DEFAULT_MIN_ORDER_AMOUNT_USD,
    STATUS_BELOW_MINIMUM_ORDER_AMOUNT,
    STATUS_OK,
    calculate_micro_order_quantity,
)

STATUS_AFFORDABLE_WHOLE_SHARE = "AFFORDABLE_WHOLE_SHARE"
STATUS_AFFORDABLE_FRACTIONAL = "AFFORDABLE_FRACTIONAL"
STATUS_INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
STATUS_NOT_FRACTIONABLE = "NOT_FRACTIONABLE"
STATUS_BELOW_MINIMUM_ORDER = "BELOW_MINIMUM_ORDER"
STATUS_UNKNOWN_ACCOUNT_STATE = "UNKNOWN_ACCOUNT_STATE"

AFFORDABLE_STATUSES = {STATUS_AFFORDABLE_WHOLE_SHARE, STATUS_AFFORDABLE_FRACTIONAL}


def _is_finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass
class AccountState:
    """The account-wide inputs, computed ONCE per scan and shared across
    every candidate evaluate_affordability() call. `available_cash_krw`
    must already be the caller's freshest broker-reported cash figure;
    this dataclass does not itself judge staleness (order_gateway.py's
    validate_and_size_live_entry() re-validates freshness at the actual
    submission boundary -- a watchlist scan is advisory, not final)."""

    available_cash_krw: Optional[float]
    cash_usage_percent: Optional[float]
    fx_rate_krw_per_usd: Optional[float]
    pending_buy_reservations_krw: float = 0.0
    unknown_submission_reservations_krw: float = 0.0
    current_open_position_cost_krw: float = 0.0

    def validation_error(self):
        """Returns a human-readable reason string if this account state is
        unusable for affordability screening, else None. Fail-closed: any
        missing/non-numeric/non-finite/out-of-range input blocks the whole
        scan rather than silently treating candidates as affordable."""
        if self.available_cash_krw is None:
            return "no available cash figure"
        if not _is_finite_number(self.available_cash_krw):
            return f"available cash must be a finite number, got {self.available_cash_krw!r}"
        if self.available_cash_krw < 0:
            return f"available cash must not be negative, got {self.available_cash_krw!r}"
        if self.cash_usage_percent is None or not _is_finite_number(self.cash_usage_percent):
            return f"cash_usage_percent must be a finite number, got {self.cash_usage_percent!r}"
        if not (0 < self.cash_usage_percent <= 100):
            return f"cash_usage_percent must be in (0, 100], got {self.cash_usage_percent!r}"
        if self.fx_rate_krw_per_usd is None or not _is_finite_number(self.fx_rate_krw_per_usd):
            return f"FX rate must be a finite number, got {self.fx_rate_krw_per_usd!r}"
        if self.fx_rate_krw_per_usd <= 0:
            return f"FX rate must be positive, got {self.fx_rate_krw_per_usd!r}"
        for name in ("pending_buy_reservations_krw", "unknown_submission_reservations_krw",
                     "current_open_position_cost_krw"):
            value = getattr(self, name)
            if not _is_finite_number(value) or value < 0:
                return f"{name} must be a non-negative finite number, got {value!r}"
        return None

    @property
    def max_allocatable_cash_krw(self):
        return self.available_cash_krw * (self.cash_usage_percent / 100.0)

    @property
    def available_for_new_order_krw(self):
        return (
            self.max_allocatable_cash_krw
            - self.pending_buy_reservations_krw
            - self.unknown_submission_reservations_krw
            - self.current_open_position_cost_krw
        )


@dataclass
class WatchlistCandidate:
    symbol: str
    latest_price_usd: float
    estimated_entry_price_usd: float
    fractionable: bool
    minimum_order_amount_usd: float = DEFAULT_MIN_ORDER_AMOUNT_USD
    estimated_slippage_usd: float = 0.0


@dataclass
class AffordabilityResult:
    symbol: str
    available_cash_krw: Optional[float]
    cash_usage_percent: Optional[float]
    max_allocatable_cash_krw: Optional[float]
    available_for_new_order_krw: Optional[float]
    fractionable: bool
    minimum_order_amount_usd: float
    whole_share_affordable: bool
    fractional_order_affordable: bool
    estimated_order_quantity: float
    affordability_status: str
    affordability_reason: str

    @property
    def is_affordable(self):
        return self.affordability_status in AFFORDABLE_STATUSES


def evaluate_affordability(candidate: WatchlistCandidate, account: AccountState) -> AffordabilityResult:
    """Never raises -- every input problem is reported as
    STATUS_UNKNOWN_ACCOUNT_STATE / a specific exclusion status rather than
    an exception, since this runs per-candidate over a whole scan and one
    bad account snapshot must not crash the rest of the scan."""

    def _blocked(status, reason, *, whole=False, fractional=False, qty=0.0,
                 max_cash=None, avail=None):
        return AffordabilityResult(
            symbol=candidate.symbol,
            available_cash_krw=account.available_cash_krw,
            cash_usage_percent=account.cash_usage_percent,
            max_allocatable_cash_krw=max_cash,
            available_for_new_order_krw=avail,
            fractionable=candidate.fractionable,
            minimum_order_amount_usd=candidate.minimum_order_amount_usd,
            whole_share_affordable=whole,
            fractional_order_affordable=fractional,
            estimated_order_quantity=qty,
            affordability_status=status,
            affordability_reason=reason,
        )

    account_error = account.validation_error()
    if account_error is not None:
        return _blocked(STATUS_UNKNOWN_ACCOUNT_STATE, account_error)

    if not _is_finite_number(candidate.estimated_entry_price_usd) or candidate.estimated_entry_price_usd <= 0:
        return _blocked(
            STATUS_UNKNOWN_ACCOUNT_STATE,
            f"invalid estimated_entry_price_usd {candidate.estimated_entry_price_usd!r}",
            max_cash=account.max_allocatable_cash_krw,
            avail=account.available_for_new_order_krw,
        )

    effective_price_usd = candidate.estimated_entry_price_usd + max(candidate.estimated_slippage_usd, 0.0)
    max_cash = account.max_allocatable_cash_krw
    available_krw = account.available_for_new_order_krw

    if available_krw <= 0:
        return _blocked(
            STATUS_INSUFFICIENT_BALANCE,
            f"no cash available for a new order (allocatable={max_cash:.0f} KRW, "
            f"already committed={max_cash - available_krw:.0f} KRW)",
            max_cash=max_cash, avail=available_krw,
        )

    whole_sizing = calculate_micro_order_quantity(
        available_krw, account.fx_rate_krw_per_usd, effective_price_usd,
        min_order_amount_usd=candidate.minimum_order_amount_usd, fractional_shares_allowed=False,
    )
    if whole_sizing.status == STATUS_OK:
        return AffordabilityResult(
            symbol=candidate.symbol,
            available_cash_krw=account.available_cash_krw,
            cash_usage_percent=account.cash_usage_percent,
            max_allocatable_cash_krw=max_cash,
            available_for_new_order_krw=available_krw,
            fractionable=candidate.fractionable,
            minimum_order_amount_usd=candidate.minimum_order_amount_usd,
            whole_share_affordable=True,
            fractional_order_affordable=False,
            estimated_order_quantity=whole_sizing.quantity,
            affordability_status=STATUS_AFFORDABLE_WHOLE_SHARE,
            affordability_reason=f"{whole_sizing.quantity} whole share(s) at ${effective_price_usd:.2f}",
        )

    if whole_sizing.status == STATUS_BELOW_MINIMUM_ORDER_AMOUNT:
        return _blocked(
            STATUS_BELOW_MINIMUM_ORDER, whole_sizing.reason,
            max_cash=max_cash, avail=available_krw,
        )

    # whole_sizing.status == STATUS_INSUFFICIENT_FUNDS: 1 whole share costs
    # more than the currently available budget. Per the explicit
    # requirement, this alone must NEVER exclude a fractionable symbol.
    if not candidate.fractionable:
        return _blocked(
            STATUS_NOT_FRACTIONABLE,
            f"1 whole share costs more than available budget (${available_krw / account.fx_rate_krw_per_usd:.2f} "
            f"< ${effective_price_usd:.2f}) and this symbol does not support fractional orders",
            max_cash=max_cash, avail=available_krw,
        )

    fractional_sizing = calculate_micro_order_quantity(
        available_krw, account.fx_rate_krw_per_usd, effective_price_usd,
        min_order_amount_usd=candidate.minimum_order_amount_usd, fractional_shares_allowed=True,
    )
    if fractional_sizing.status == STATUS_OK:
        return AffordabilityResult(
            symbol=candidate.symbol,
            available_cash_krw=account.available_cash_krw,
            cash_usage_percent=account.cash_usage_percent,
            max_allocatable_cash_krw=max_cash,
            available_for_new_order_krw=available_krw,
            fractionable=True,
            minimum_order_amount_usd=candidate.minimum_order_amount_usd,
            whole_share_affordable=False,
            fractional_order_affordable=True,
            estimated_order_quantity=fractional_sizing.quantity,
            affordability_status=STATUS_AFFORDABLE_FRACTIONAL,
            affordability_reason=f"{fractional_sizing.quantity:.4f} fractional share(s) at ${effective_price_usd:.2f}",
        )

    # fractional_sizing.status == STATUS_BELOW_MINIMUM_ORDER_AMOUNT
    return _blocked(
        STATUS_BELOW_MINIMUM_ORDER, fractional_sizing.reason,
        max_cash=max_cash, avail=available_krw,
    )


def filter_watchlist(candidates: List[WatchlistCandidate], account: AccountState) -> List[AffordabilityResult]:
    """Evaluates every candidate against the SAME account snapshot (the
    caller must compute `account` once per scan, not re-derive it per
    symbol -- see AccountState's docstring) and returns ALL results, not
    just the affordable ones, so a caller can log/audit why any given
    symbol was excluded. Use `[r for r in results if r.is_affordable]` to
    get the final buyable watchlist."""
    return [evaluate_affordability(c, account) for c in candidates]
