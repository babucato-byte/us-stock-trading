"""Bar-by-bar backtest/replay engine.

Look-ahead prevention (structural, not a convention strategy authors must
remember): at every decision point the engine only ever passes
`bars.iloc[:i+1]` to the strategy -- bar i is "the current, just-closed
1-minute bar," never a bar beyond it. A 1-minute bar's OHLCV is only
knowable once that minute has fully elapsed, so treating bar i as
available the instant the loop reaches it (never bar i+1 or later) is
exactly "no unfinished/future bar is ever referenced."

Session separation: entries are only evaluated when
get_us_market_session(bar_time) is in config.entry_allowed_sessions
(default: regular only), mirroring paper_strategy_order.py's real
`market_session == "regular"` gate. Premarket bars still flow into the
strategy's indicator warmup (VWAP/EMA/ATR need continuous bars to be
correct) but can never themselves trigger a signal->fill.
"""

from datetime import timedelta

import pandas as pd

from backtest.config import BacktestConfig
from backtest.models import BacktestResult, CostBreakdown, ExitEvent, STATUS_INSUFFICIENT_DATA, STATUS_OK, Trade
from config import scalping_strategy_v1_config as strategy_cfg
from market_hours import MARKET_REGULAR_END, combine_eastern, get_us_market_session


class BacktestError(Exception):
    pass


def _column(bars, name):
    if name in bars.columns:
        return bars[name]
    lower = name.lower()
    if lower in bars.columns:
        return bars[lower]
    raise BacktestError(f"bars is missing required column {name!r} (or {lower!r})")


REQUIRED_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def _validate_bars(bars):
    if not isinstance(bars.index, pd.DatetimeIndex):
        raise BacktestError("bars must have a DatetimeIndex (one row per 1-minute bar)")
    for col in REQUIRED_COLUMNS:
        try:
            _column(bars, col)
        except BacktestError:
            raise
    if not bars.index.is_monotonic_increasing:
        raise BacktestError("bars must be sorted by time ascending (no shuffled/out-of-order bars)")


class _OpenPosition:
    __slots__ = (
        "signal_time", "signal_price", "entry_time", "entry_price", "entry_session",
        "entry_bar_volume", "stop_price", "target_1_price", "target_2_price",
        "requested_qty", "remaining_qty", "filled_qty", "entry_time_dt",
        "exit_events", "costs", "partial_exited",
    )


def _apply_entry_costs(price, qty, side, config):
    """Return (adjusted_price, spread_cost, slippage_cost, fee_cost).
    side='buy' worsens price upward, side='sell' worsens price downward --
    a fill always costs the trader, never favors them."""
    spread_frac = config.spread_bps / 10_000.0
    slippage_frac = config.slippage_bps / 10_000.0
    if side == "buy":
        adjusted = price * (1 + spread_frac + slippage_frac)
    else:
        adjusted = price * (1 - spread_frac - slippage_frac)
    spread_cost = abs(price * spread_frac) * qty
    slippage_cost = abs(price * slippage_frac) * qty
    fee_cost = config.fee_per_share * qty
    return adjusted, spread_cost, slippage_cost, fee_cost


def _volume_capped_qty(desired_qty, bar_volume, config):
    if pd.isna(bar_volume) or bar_volume <= 0:
        return 0
    cap = int(bar_volume * config.max_fill_fraction_of_bar_volume)
    return max(0, min(desired_qty, cap))


def run_backtest(strategy, bars, *, symbol, config=None):
    """Replay `bars` (a DatetimeIndex-indexed OHLCV DataFrame for one
    symbol) through `strategy`, simulating Stage 4's live exit policy
    (1R 50% partial, 2R/stop full exit, time-stop, EOD forced close).

    Returns a BacktestResult. status=INSUFFICIENT_DATA (no trades
    simulated at all) if fewer than config.min_bars_required bars are
    supplied -- a strategy is never scored on a sample too small to mean
    anything, per the requirement that data-poor strategies are reported
    as INSUFFICIENT_DATA rather than given a (misleadingly precise)
    performance number.
    """
    config = config or BacktestConfig()
    _validate_bars(bars)
    n = len(bars)

    if n < config.min_bars_required:
        return BacktestResult(
            strategy_id=strategy.strategy_id, strategy_version=strategy.version, symbol=symbol,
            status=STATUS_INSUFFICIENT_DATA, trades=[], bars_evaluated=n,
            reason=f"{n} bars supplied, {config.min_bars_required} required",
        )

    trades = []
    open_position = None
    i = 0
    while i < n:
        if open_position is None:
            i, open_position = _try_enter(strategy, bars, i, n, symbol, config)
            if open_position is None:
                i += 1
            continue

        closed_trade = _manage_bar(strategy, symbol, bars, i, open_position, config)
        if closed_trade is not None:
            trades.append(closed_trade)
            open_position = None
        i += 1

    return BacktestResult(
        strategy_id=strategy.strategy_id, strategy_version=strategy.version, symbol=symbol,
        status=STATUS_OK, trades=trades, bars_evaluated=n,
    )


def _try_enter(strategy, bars, i, n, symbol, config):
    """Look for an entry signal at bar i. Returns (next_i, open_position_or_None)."""
    bar_time = bars.index[i]
    session = get_us_market_session(bar_time)
    if session not in config.entry_allowed_sessions:
        return i, None

    visible_bars = bars.iloc[: i + 1]
    evaluation = strategy.generate_entry(visible_bars, symbol=symbol)
    if not evaluation.signal:
        return i, None

    fill_index = i + 1 + config.entry_delay_bars
    if fill_index >= n:
        # Not enough bars left to fill and simulate this trade -- do not
        # fabricate a result for a signal we can't actually replay.
        return n, None

    fill_bar = bars.iloc[fill_index]
    fill_time = bars.index[fill_index]
    fill_session = get_us_market_session(fill_time)
    bar_volume = _column(bars, "Volume").iloc[fill_index]

    # Real position sizing (risk budget / stop distance / 30,000 KRW cap) is
    # Stage 4/Stage 10's concern for live trading, not this engine's -- a
    # backtest measures the strategy's per-share edge using a fixed nominal
    # lot size (config.nominal_qty), volume-capped like any real fill.
    requested_qty = config.nominal_qty
    filled_qty = _volume_capped_qty(requested_qty, bar_volume, config)
    if filled_qty <= 0:
        return fill_index, None  # bar too illiquid to fill even 1 share; look for the next signal

    entry_price, spread_cost, slippage_cost, fee_cost = _apply_entry_costs(
        float(_column(bars, "Open").iloc[fill_index]), filled_qty, "buy", config
    )
    signal_close = float(_column(bars, "Close").iloc[i])
    entry_delay_cost = (float(_column(bars, "Open").iloc[fill_index]) - signal_close) * filled_qty

    pos = _OpenPosition()
    pos.signal_time = str(bar_time)
    pos.signal_price = signal_close
    pos.entry_time = str(fill_time)
    pos.entry_time_dt = fill_time
    pos.entry_price = entry_price
    pos.entry_session = fill_session
    pos.entry_bar_volume = float(bar_volume) if not pd.isna(bar_volume) else 0.0
    pos.stop_price = float(evaluation.stop_price)
    pos.target_1_price = float(evaluation.target_1)
    pos.target_2_price = float(evaluation.target_2)
    pos.requested_qty = requested_qty
    pos.remaining_qty = filled_qty
    pos.filled_qty = filled_qty
    pos.partial_exited = False
    pos.exit_events = []
    pos.costs = CostBreakdown(spread_cost=spread_cost, slippage_cost=slippage_cost,
                               fee_cost=fee_cost, entry_delay_cost=entry_delay_cost)

    return fill_index, pos


def _apply_exit(pos, bars, i, price, reason, config):
    """Exit as much of pos.remaining_qty as this bar's volume allows.
    Returns True if the position is now fully closed (remaining_qty==0)."""
    bar_time = bars.index[i]
    session = get_us_market_session(bar_time)
    bar_volume = _column(bars, "Volume").iloc[i]
    exit_qty = _volume_capped_qty(pos.remaining_qty, bar_volume, config)
    if exit_qty <= 0:
        return False  # too illiquid to exit anything this bar; try again next bar

    fill_price, spread_cost, slippage_cost, fee_cost = _apply_entry_costs(price, exit_qty, "sell", config)
    pos.costs.spread_cost += spread_cost
    pos.costs.slippage_cost += slippage_cost
    pos.costs.fee_cost += fee_cost
    pos.exit_events.append(ExitEvent(time=str(bar_time), price=fill_price, qty=exit_qty, reason=reason, session=session))
    pos.remaining_qty -= exit_qty
    return pos.remaining_qty <= 0


def _finalize_trade(strategy, symbol, pos, final_reason):
    total_exit_value = sum(e.price * e.qty for e in pos.exit_events)
    total_exit_qty = sum(e.qty for e in pos.exit_events)
    realized_pnl = total_exit_value - pos.entry_price * total_exit_qty - pos.costs.fee_cost
    risk_per_share = pos.entry_price - pos.stop_price
    r_multiple = (realized_pnl / (total_exit_qty * risk_per_share)) if (total_exit_qty > 0 and risk_per_share > 0) else None

    return Trade(
        symbol=symbol, strategy_id=strategy.strategy_id, strategy_version=strategy.version,
        signal_time=pos.signal_time, signal_price=pos.signal_price,
        entry_time=pos.entry_time, entry_price=pos.entry_price, entry_session=pos.entry_session,
        entry_bar_volume=pos.entry_bar_volume,
        stop_price=pos.stop_price, target_1_price=pos.target_1_price, target_2_price=pos.target_2_price,
        requested_qty=pos.requested_qty, filled_qty=pos.filled_qty,
        exit_events=pos.exit_events, exit_reason=final_reason,
        realized_pnl=realized_pnl, r_multiple=r_multiple, costs=pos.costs,
    )


def _manage_bar(strategy, symbol, bars, i, pos, config):
    """Evaluate bar i against an open position. Returns a finalized Trade
    if the position closed this bar, else None (still open)."""
    bar_time = bars.index[i]
    high = float(_column(bars, "High").iloc[i])
    low = float(_column(bars, "Low").iloc[i])
    open_price = float(_column(bars, "Open").iloc[i])
    close_price = float(_column(bars, "Close").iloc[i])

    eod_cutoff = combine_eastern(bar_time.date(), MARKET_REGULAR_END) - timedelta(
        minutes=strategy_cfg.EOD_FORCE_CLOSE_MINUTES_BEFORE_CLOSE
    )
    if bar_time >= eod_cutoff:
        if _apply_exit(pos, bars, i, open_price, "EOD_FORCED_CLOSE", config):
            return _finalize_trade(strategy, symbol, pos, "EOD_FORCED_CLOSE")
        return None

    held_minutes = (bar_time - pos.entry_time_dt).total_seconds() / 60.0
    if held_minutes >= strategy_cfg.MAX_POSITION_HOLD_MINUTES:
        if _apply_exit(pos, bars, i, open_price, "TIME_STOP", config):
            return _finalize_trade(strategy, symbol, pos, "TIME_STOP")
        return None

    visible_bars = bars.iloc[: i + 1]
    if strategy.invalidate(visible_bars, symbol=symbol):
        if _apply_exit(pos, bars, i, close_price, "STRATEGY_INVALIDATION", config):
            return _finalize_trade(strategy, symbol, pos, "STRATEGY_INVALIDATION")
        return None

    stop_hit = low <= pos.stop_price
    target_2_hit = pos.partial_exited and high >= pos.target_2_price
    target_1_hit = (not pos.partial_exited) and high >= pos.target_1_price

    if stop_hit:
        # STOP_FIRST collision policy: if this bar's range also would have
        # touched a target, we still resolve it as a stop -- the
        # conservative assumption per config.same_bar_collision_policy.
        if _apply_exit(pos, bars, i, pos.stop_price, "STOP_LOSS", config):
            return _finalize_trade(strategy, symbol, pos, "STOP_LOSS")
        return None

    if target_2_hit:
        if _apply_exit(pos, bars, i, pos.target_2_price, "TARGET_2", config):
            return _finalize_trade(strategy, symbol, pos, "TARGET_2")
        return None

    if target_1_hit:
        fraction = strategy_cfg.PARTIAL_EXIT_FRACTION_AT_TARGET_1
        partial_qty = max(1, int(round(pos.remaining_qty * fraction)))
        original_remaining = pos.remaining_qty
        exit_qty_before = pos.remaining_qty
        pos.remaining_qty = partial_qty  # temporarily cap desired exit qty at the partial fraction
        closed = _apply_exit(pos, bars, i, pos.target_1_price, "PARTIAL_TARGET_1", config)
        # restore any qty that wasn't part of the intended partial exit
        pos.remaining_qty += (exit_qty_before - partial_qty)
        pos.partial_exited = True
        if closed and pos.remaining_qty <= 0:
            return _finalize_trade(strategy, symbol, pos, "PARTIAL_TARGET_1")
        return None

    return None
