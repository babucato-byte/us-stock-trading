"""Run the whole S1 pre-order chain without a broker and without an order.

    cash pool -> risk guards -> per-candidate freshness / re-entry
              -> allocation -> whole-share sizing

Every input is an argument. Nothing here opens a socket, and the module
does not import the execution engine, the broker or the order gate -- a
test asserts that, so "the dry run cannot place an order" is a property
of the import graph rather than a promise.

Order of evaluation, and why
----------------------------
Account-wide guards run FIRST and once. If today's loss budget is
exhausted, no per-candidate work is worth doing and no candidate should
appear in the plan as "would have been funded" -- that would read as an
opportunity the guard cost, when in fact the guard is the point.

Per-candidate guards then run BEFORE allocation, so a symbol that is
already held or whose signal is stale never consumes budget. Reversing
those two would reserve cash for a candidate that was never eligible,
and every later candidate would see a smaller pool than it really had.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config import s1_allocation
from s1_live import allocator, cash_pool, freshness, reentry, risk_guards

logger = logging.getLogger(__name__)

BLOCKED_BY_ACCOUNT_GUARD = "ACCOUNT_GUARD"
BLOCKED_BY_CANDIDATE_GUARD = "CANDIDATE_GUARD"


@dataclass
class DryRunResult:
    trading_day: str
    allocation_version: str
    cash_pool: Dict[str, Any]
    account_guards: List[Dict[str, Any]] = field(default_factory=list)
    account_allowed: bool = False
    eligible: List[str] = field(default_factory=list)
    rejected: List[Dict[str, Any]] = field(default_factory=list)
    plan: Optional[Dict[str, Any]] = None
    observations: List[Dict[str, Any]] = field(default_factory=list)
    #: The durable PHASE 4B state, when one was supplied.
    risk_state: Optional[Dict[str, Any]] = None

    @property
    def would_submit(self) -> int:
        """How many orders a live run WOULD have submitted. Always
        reported so a dry run can never be mistaken for a live one."""
        if not self.plan:
            return 0
        return sum(1 for item in self.plan["allocations"]
                   if item["status"] == allocator.STATUS_ALLOCATED and item["quantity"] > 0)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "mode": "DRY_RUN",
            "trading_day": self.trading_day,
            "allocation_version": self.allocation_version,
            "allocation_config": s1_allocation.as_dict(),
            "cash_pool": self.cash_pool,
            "account_allowed": self.account_allowed,
            "account_guards": self.account_guards,
            "risk_state": self.risk_state,
            "eligible": self.eligible,
            "rejected": self.rejected,
            "plan": self.plan,
            "observations": self.observations,
            "would_submit": self.would_submit,
            "orders_submitted": 0,
        }


def simulate(*, trading_day, candidates, cash_pool_usd=None, pool=None,
             price_lookup, orderable_lookup=None, reserved_usd=0.0,
             symbol_state_lookup=None, now=None,
             pnl_today_usd=None, basis_equity_usd=None,
             equity_usd=None, peak_equity_usd=None,
             consecutive_losses=0, consecutive_loss_limit=None,
             cooldown_seconds=None, max_signal_age_seconds=None,
             risk_state=None) -> DryRunResult:
    """Simulate one S1 entry cycle. Places nothing.

    `candidates` are validated S1 candidate rows in rank order.
    `pool` may be supplied directly; otherwise `cash_pool_usd` is wrapped.
    """
    resolved = pool if pool is not None else cash_pool.from_amount(
        cash_pool_usd, source="dry_run", now=now)

    result = DryRunResult(
        trading_day=str(trading_day),
        allocation_version=s1_allocation.ALLOCATION_VERSION,
        cash_pool=resolved.as_dict(),
    )

    if risk_state is not None:
        # PHASE 4B: the durable state is authoritative when supplied. Its
        # start equity is the one captured at the open and never
        # recomputed, so a mid-day restart cannot widen the loss budget.
        result.risk_state = risk_state.as_dict()
        allowed = risk_state.entries_allowed
        guards = [
            risk_guards.check_daily_loss(
                pnl_today_usd=(None if risk_state.current_equity is None
                               or risk_state.start_equity is None
                               else risk_state.current_equity - risk_state.start_equity),
                basis_equity_usd=risk_state.start_equity),
            risk_guards.check_drawdown(equity_usd=risk_state.current_equity,
                                       peak_equity_usd=risk_state.peak_equity),
            risk_guards.check_consecutive_losses(
                consecutive_losses=consecutive_losses, limit=consecutive_loss_limit),
        ]
    else:
        allowed, guards = risk_guards.evaluate_all(
            pnl_today_usd=pnl_today_usd, basis_equity_usd=basis_equity_usd,
            equity_usd=equity_usd, peak_equity_usd=peak_equity_usd,
            consecutive_losses=consecutive_losses,
            consecutive_loss_limit=consecutive_loss_limit)
    result.account_guards = [item.as_dict() for item in guards]
    result.account_allowed = allowed

    if not allowed:
        result.rejected = [{
            "symbol": str(row.get("symbol")), "stage": BLOCKED_BY_ACCOUNT_GUARD,
            "reason_code": next((item.reason_code for item in guards
                                 if not item.allows_entry), None),
        } for row in candidates]
        return result

    if not resolved.available:
        result.rejected = [{
            "symbol": str(row.get("symbol")), "stage": BLOCKED_BY_ACCOUNT_GUARD,
            "reason_code": resolved.reason_code,
        } for row in candidates]
        return result

    eligible_rows = []
    for row in candidates:
        symbol = str(row.get("symbol") or "").upper()
        verdict = freshness.check(
            signal_timestamp=row.get("signal_timestamp"),
            signal_trading_day=row.get("trading_day"),
            expected_trading_day=trading_day, now=now,
            max_age_seconds=max_signal_age_seconds)
        if not verdict.allows_entry:
            result.rejected.append({"symbol": symbol, "stage": BLOCKED_BY_CANDIDATE_GUARD,
                                    "reason_code": verdict.reason_code,
                                    "detail": verdict.detail})
            continue

        if symbol_state_lookup is not None:
            state = symbol_state_lookup(symbol)
            re_verdict = reentry.check(
                state=state, source_signal_id=row.get("signal_id"),
                source_signal_timestamp=row.get("signal_timestamp"),
                now=now, cooldown_seconds=cooldown_seconds)
            if not re_verdict.allows_entry:
                result.rejected.append({"symbol": symbol,
                                        "stage": BLOCKED_BY_CANDIDATE_GUARD,
                                        "reason_code": re_verdict.reason_code,
                                        "detail": re_verdict.detail})
                continue

        current_price = price_lookup(symbol)
        result.observations.append({
            "symbol": symbol,
            "signal_age_seconds": verdict.age_seconds,
            **freshness.measure_extension(row.get("signal_price"), current_price),
        })
        eligible_rows.append(row)

    result.eligible = [str(row.get("symbol")).upper() for row in eligible_rows]
    plan = allocator.allocate(
        eligible_rows, cash_pool_usd=resolved.require(),
        price_lookup=price_lookup, orderable_lookup=orderable_lookup,
        reserved_usd=reserved_usd)
    result.plan = plan.as_dict()
    return result
