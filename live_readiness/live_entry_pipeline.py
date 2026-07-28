"""Live entry pipeline -- orchestrates Account Engine -> Risk Engine ->
Sizing Engine -> Affordability Filter -> Execution Engine for ONE live
buy entry, per docs/autonomous/PROJECT_CONSTITUTION.md's 계층 분리 원칙:

    Market Scanner -> Strategy Engine -> Account Engine -> Risk Engine ->
    Sizing Engine -> Affordability Filter -> Execution Engine -> Broker

CODEX-040: this is the function `paper_strategy_order.main()` calls for
a live-mode (`broker.config.is_live_mode`) buy entry -- see
`DECISION_LOG.md`'s Stage 11 wiring cycle section for why Paper-mode
entries do NOT go through this pipeline (a KRW-denominated, percent-of-
balance pilot budget model has no meaningful translation into Paper's
existing USD/equity-ratio sizing, and Paper's order path was never in
scope for any of CODEX-026 through CODEX-040 -- every one of those gates
is explicitly scoped to `side="buy" AND is_live_mode`).

`run_live_entry_pipeline()` raises `LiveEntryPipelineError` -- with ZERO
broker calls -- as soon as any stage blocks. Each stage's own exception
type is caught and re-raised as `LiveEntryPipelineError` so a caller only
needs to catch one exception type, while the original stage-specific
exception is chained (`from exc`) for diagnosis.

Every stage's authoritative inputs (account snapshot, trusted
cash_usage_percent, durable exposure) come from the SAME
`account_engine.AccountSnapshot` built once at the top of this function
-- never re-derived or re-declared by a later stage.

2026-07-28: `fractionable` defaults to `False` and this module's sole
caller (`paper_strategy_order.main()`) never overrides it -- every live
entry is whole-share-only ("소수점 주문 금지"), and Sizing Engine already
floors/blocks accordingly (`sizing_engine.compute_sizing_decision()`
raising below `sizing_decision.actual_qty <= 0` is exactly "this symbol
is not currently affordable for even one whole share", which this
function treats as a hard block, not a fractional fallback).
"""

import uuid
from datetime import datetime, timezone

import risk_config
from live_readiness import account_engine, execution_engine, risk_engine, sizing_engine
from live_readiness import trusted_operator_config
from live_readiness.account_cash import AccountCashSnapshot
from live_readiness.order_gateway import LiveEntryContext
from live_readiness.watchlist_affordability import AccountState, WatchlistCandidate, evaluate_affordability


class LiveEntryPipelineError(Exception):
    """Raised whenever any pipeline stage blocks the entry. Callers must
    treat this as a hard block -- the broker's order-submission method is
    never reached when this is raised."""


def run_live_entry_pipeline(
    *, symbol, strategy_id, signal_id, entry_price_usd, broker, conn,
    fx_rate_krw_per_usd, fx_rate_as_of, allow_list, client_order_id=None,
    stop_price_usd=None, strategy_max_qty=None, max_daily_loss_krw=None,
    current_daily_loss_krw=0.0, max_risk_per_trade_krw=None,
    max_order_notional_krw=None, fractionable=False, minimum_order_amount_usd=1.0,
    buffer_bps=0.0, slippage_usd=0.0, now=None,
):
    """Returns an `execution_engine.ExecutionResult` on success. `signal_id`/
    `strategy_id` identify which Strategy Engine signal produced this
    attempt (for audit only -- neither is trusted for cash/qty).
    `entry_price_usd` is the Strategy Engine's estimated fill price (a
    legitimate strategy output); `stop_price_usd`, if omitted, is derived
    from `risk_config.STOP_LOSS_RATE` (this codebase's existing global
    stop-loss policy, not a value invented for this pipeline).
    """
    current = now or datetime.now(timezone.utc)

    # 1. Account Engine -- authoritative, never optional in this pipeline.
    try:
        snapshot = account_engine.build_account_snapshot(
            broker, fx_rate_krw_per_usd, conn, now=current,
        )
    except account_engine.AccountEngineError as exc:
        raise LiveEntryPipelineError(f"Account Engine blocked entry for {symbol}: {exc}") from exc

    # 2. Trusted operator config (CODEX-039) -- no caller-declared percent
    # is read or combined here at all.
    try:
        cash_usage_percent = trusted_operator_config.get_cash_usage_percent()
    except trusted_operator_config.TrustedConfigError as exc:
        raise LiveEntryPipelineError(
            f"trusted operator config blocked entry for {symbol}: {exc}"
        ) from exc

    max_allocatable_cash_krw = account_engine.compute_max_allocatable_cash_krw(
        snapshot, cash_usage_percent,
    )
    available_for_new_order_krw = account_engine.compute_available_for_new_order_krw(
        snapshot, cash_usage_percent,
    )

    # 3. Risk Engine -- never uses a strategy-declared quantity (there is
    # none to use).
    effective_stop_price_usd = (
        stop_price_usd if stop_price_usd is not None
        else entry_price_usd * (1 + risk_config.STOP_LOSS_RATE)
    )
    try:
        daily_loss_remaining_krw = risk_engine.compute_daily_loss_remaining_krw(
            max_daily_loss_krw if max_daily_loss_krw is not None else max_allocatable_cash_krw,
            current_daily_loss_krw,
        )
        risk_decision = risk_engine.compute_risk_decision(
            entry_price_usd, effective_stop_price_usd, fx_rate_krw_per_usd,
            daily_loss_remaining_krw, max_risk_per_trade_krw=max_risk_per_trade_krw,
            now=current,
        )
    except risk_engine.RiskEngineError as exc:
        raise LiveEntryPipelineError(f"Risk Engine blocked entry for {symbol}: {exc}") from exc

    # 4. Sizing Engine -- the only place a final quantity is computed.
    buffered_price = sizing_engine.apply_entry_price_buffer(
        entry_price_usd, buffer_bps=buffer_bps, slippage_usd=slippage_usd,
    )
    sizing_budget_krw = available_for_new_order_krw
    if max_order_notional_krw is not None:
        sizing_budget_krw = min(sizing_budget_krw, max_order_notional_krw)
    try:
        sizing_decision = sizing_engine.compute_sizing_decision(
            sizing_budget_krw, buffered_price, fx_rate_krw_per_usd, fractionable,
            risk_based_qty=risk_decision.risk_based_qty, strategy_max_qty=strategy_max_qty,
            min_order_amount_usd=minimum_order_amount_usd,
        )
    except sizing_engine.SizingEngineError as exc:
        raise LiveEntryPipelineError(f"Sizing Engine blocked entry for {symbol}: {exc}") from exc

    if sizing_decision.actual_qty <= 0:
        raise LiveEntryPipelineError(
            f"Sizing Engine produced no affordable quantity for {symbol} "
            f"(balance={sizing_decision.balance_based_qty!r}, "
            f"risk={sizing_decision.risk_based_qty!r}, "
            f"strategy_max={sizing_decision.strategy_max_qty!r})"
        )

    # 5. Affordability Filter (CODEX-041) -- the SAME decision function
    # used for candidate/watchlist screening, re-run here as the final
    # pre-trade check immediately before Execution Engine so the two can
    # never silently disagree.
    account_state = AccountState(
        available_cash_krw=snapshot.effective_cash_krw,
        cash_usage_percent=cash_usage_percent,
        fx_rate_krw_per_usd=fx_rate_krw_per_usd,
        pending_buy_reservations_krw=snapshot.pending_buy_reservations_krw,
        unknown_submission_reservations_krw=snapshot.unknown_submission_reservations_krw,
        current_open_position_cost_krw=snapshot.current_open_position_cost_krw,
        as_of=snapshot.as_of,
    )
    candidate = WatchlistCandidate(
        symbol=symbol, latest_price_usd=entry_price_usd, estimated_entry_price_usd=entry_price_usd,
        fractionable=fractionable, minimum_order_amount_usd=minimum_order_amount_usd,
        estimated_slippage_usd=slippage_usd,
    )
    affordability = evaluate_affordability(candidate, account_state, now=current)
    if not affordability.is_affordable:
        raise LiveEntryPipelineError(
            f"Affordability Filter blocked entry for {symbol}: {affordability.affordability_reason} "
            f"(status={affordability.affordability_status})"
        )

    # 6. Execution Engine -- the sole path to the broker.
    resolved_client_order_id = client_order_id or f"liveentry-{symbol}-{uuid.uuid4().hex[:12]}"
    command = execution_engine.build_validated_order_command(
        signal_id=signal_id, strategy_id=strategy_id, symbol=symbol, side="buy",
        purpose="ENTRY_ORDER", sizing_decision=sizing_decision,
        account_snapshot_id=snapshot.as_of.isoformat(), risk_decision_id=risk_decision.risk_decision_id,
        client_order_id=resolved_client_order_id, now=current,
    )
    live_entry_context = LiveEntryContext(
        symbol=symbol, expected_fill_price_usd=buffered_price, allow_list=allow_list,
        available_cash_krw=snapshot.effective_cash_krw, cash_usage_percent=cash_usage_percent,
        cash_as_of=snapshot.as_of.isoformat(), fx_rate_krw_per_usd=fx_rate_krw_per_usd,
        fx_rate_as_of=fx_rate_as_of,
        max_position_count=trusted_operator_config.get_max_concurrent_live_positions(),
        max_daily_entries=trusted_operator_config.get_max_daily_live_entries(),
        stop_price_usd=effective_stop_price_usd, max_order_notional_krw=max_order_notional_krw,
        max_daily_loss_krw=max_daily_loss_krw, max_risk_per_trade_krw=max_risk_per_trade_krw,
        strategy_max_quantity=strategy_max_qty, min_order_amount_usd=minimum_order_amount_usd,
        fractional_shares_allowed=fractionable, now=current,
    )
    account_cash_snapshot = AccountCashSnapshot(
        cash_krw=snapshot.effective_cash_krw, as_of=snapshot.as_of, source=snapshot.source,
    )

    try:
        return execution_engine.submit_validated_command(
            command, broker, live_entry_context, conn=conn, now=current,
            account_cash_snapshot=account_cash_snapshot,
        )
    except execution_engine.ExecutionEngineError as exc:
        raise LiveEntryPipelineError(f"Execution Engine blocked entry for {symbol}: {exc}") from exc
