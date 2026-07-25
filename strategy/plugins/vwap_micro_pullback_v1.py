"""`VWAP_MICRO_PULLBACK_MOMENTUM_V1` -- the project's first (and, per
PROJECT_CONSTITUTION.md's "적용 전략 범위", initially only) concrete
strategy plugin.

Setup, as described in PROJECT_CONSTITUTION.md / SCALPING_V1_ROADMAP.md
Phase 4:
  1. Price above VWAP, EMA9 > EMA21 (trend/momentum filter).
  2. An initial rally (a clear run-up in price).
  3. A shallow pullback off the rally high, with *declining* volume during
     the pullback (selling pressure fading, not accelerating).
  4. A breakout above the prior/pullback high, with volume re-expansion
     (buyers stepping back in) -> entry signal.

Indicators (VWAP, EMA9, EMA21) are computed here with plain pandas -- no
existing reusable helper was found in `indicators.py` (checked first per
the Stage 3 instructions): that module implements HMA/HMA-MACD/SQZMOM for
the *daily-bar* swing scanner, not VWAP or a plain EMA suitable for 1-minute
bars, so nothing there was a fit to reuse.

Input contract: `bars` is a pandas DataFrame of 1-minute OHLCV bars for a
single trading session, in chronological order (oldest first, most recent
bar last), with columns Open/High/Low/Close/Volume (case-insensitive).
Building/polling live 1-minute bars is explicitly out of scope for Stage 3
(roadmap Phase 3) -- this module only operates on whatever DataFrame it is
given, so it is fully testable against constructed fixtures with no network
access and no look-ahead (every computation at row i only ever reads rows
<= i).
"""

from datetime import datetime, timezone

import pandas as pd

from config import scalping_strategy_v1_config as cfg
from scalping_watchlist.models import NOT_EVALUATED, UNKNOWN
from strategy.interface import (
    STATE_ENTRY_SIGNAL,
    STATE_NO_SETUP,
    EvaluationResult,
    TradingStrategy,
)
from strategy.status import STRUCTURED

STRATEGY_ID = "VWAP_MICRO_PULLBACK_MOMENTUM_V1"


def _column(df: pd.DataFrame, name: str) -> pd.Series:
    if name in df.columns:
        return pd.to_numeric(df[name], errors="coerce")
    lower = name.lower()
    if lower in df.columns:
        return pd.to_numeric(df[lower], errors="coerce")
    raise KeyError(f"bars is missing required column {name!r} (or {lower!r})")


def compute_vwap(bars: pd.DataFrame) -> pd.Series:
    """Session-cumulative VWAP: cumsum(typical_price * volume) / cumsum(volume).

    No look-ahead by construction -- each row's value only uses volume/price
    up to and including that row, since pandas cumsum() never reads ahead.
    Assumes `bars` already covers a single session (VWAP resets at session
    start); resetting VWAP across multiple sessions in one DataFrame is not
    handled here and is Stage 3 out-of-scope (bar-feed construction is
    Phase 3's job).
    """
    high = _column(bars, "High")
    low = _column(bars, "Low")
    close = _column(bars, "Close")
    volume = _column(bars, "Volume")
    typical_price = (high + low + close) / 3.0
    cum_volume = volume.cumsum()
    cum_pv = (typical_price * volume).cumsum()
    return cum_pv / cum_volume.replace(0, pd.NA)


def compute_ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def compute_atr(bars: pd.DataFrame, period: int) -> pd.Series:
    high = _column(bars, "High")
    low = _column(bars, "Low")
    close = _column(bars, "Close")
    prev_close = close.shift(1)
    true_range = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(window=period, min_periods=period).mean()


def _find_rally_and_pullback(close: pd.Series, volume: pd.Series):
    """Search the bars *before* the last (candidate breakout) bar for a
    rally high followed by a pullback. Returns a dict describing the shape,
    or None if fewer than MIN_PULLBACK_BARS of pullback exist.

    ASSUMPTION (documented in config/scalping_strategy_v1_config.py): "rally
    high" is simply the highest close among the history bars examined, and
    "pullback" is the bars strictly after that high, up to (not including)
    the breakout bar. This is a deliberately simple, deterministic
    definition -- PROJECT_CONSTITUTION.md describes the shape qualitatively
    but does not specify a peak-detection algorithm.
    """
    lookback = min(len(close), cfg.RALLY_LOOKBACK_BARS)
    history_close = close.iloc[-lookback:-1]  # excludes the breakout candidate bar
    history_volume = volume.iloc[-lookback:-1]

    if len(history_close) < cfg.MIN_PULLBACK_BARS + 1:
        return None

    rally_high_pos = int(history_close.values.argmax())
    rally_high = float(history_close.iloc[rally_high_pos])
    rally_window_low = float(history_close.iloc[: rally_high_pos + 1].min())
    rally_volume = history_volume.iloc[: rally_high_pos + 1]

    pullback_close = history_close.iloc[rally_high_pos + 1 :]
    pullback_volume = history_volume.iloc[rally_high_pos + 1 :]

    if len(pullback_close) < cfg.MIN_PULLBACK_BARS:
        return None  # rally exists, but no (or too-short) pullback yet

    pullback_low = float(pullback_close.min())

    return {
        "rally_high": rally_high,
        "rally_window_low": rally_window_low,
        "rally_volume": rally_volume,
        "pullback_close": pullback_close,
        "pullback_volume": pullback_volume,
        "pullback_low": pullback_low,
    }


class VWAPMicroPullbackV1(TradingStrategy):
    """Concrete strategy plugin for VWAP_MICRO_PULLBACK_MOMENTUM_V1."""

    def __init__(self, version: str = "1.0.0", status: str = STRUCTURED):
        super().__init__(STRATEGY_ID, version, status)

    # -- Stage 3 real logic ------------------------------------------------

    def evaluate_setup(self, bars: pd.DataFrame, *, symbol: str, as_of=None) -> EvaluationResult:
        evaluated_at = as_of or datetime.now(timezone.utc).isoformat()

        if bars is None or len(bars) < cfg.MIN_BARS_FOR_SETUP:
            return EvaluationResult(
                strategy_id=self.strategy_id,
                symbol=symbol,
                evaluated_at=evaluated_at,
                state=STATE_NO_SETUP,
                signal=False,
                rejection_reasons="INSUFFICIENT_BARS",
                input_snapshot={"bar_count": 0 if bars is None else len(bars)},
            )

        close = _column(bars, "Close")
        volume = _column(bars, "Volume")
        vwap = compute_vwap(bars)
        ema9 = compute_ema(close, 9)
        ema21 = compute_ema(close, 21)

        latest_close = float(close.iloc[-1])
        latest_vwap = vwap.iloc[-1]
        latest_ema9 = ema9.iloc[-1]
        latest_ema21 = ema21.iloc[-1]

        snapshot = {
            "latest_close": latest_close,
            "vwap": None if pd.isna(latest_vwap) else float(latest_vwap),
            "ema9": None if pd.isna(latest_ema9) else float(latest_ema9),
            "ema21": None if pd.isna(latest_ema21) else float(latest_ema21),
        }

        rejection_reasons = []

        if pd.isna(latest_vwap) or not (latest_close > latest_vwap):
            rejection_reasons.append("PRICE_NOT_ABOVE_VWAP")
        if pd.isna(latest_ema9) or pd.isna(latest_ema21) or not (latest_ema9 > latest_ema21):
            rejection_reasons.append("EMA9_NOT_ABOVE_EMA21")

        shape = _find_rally_and_pullback(close, volume)
        if shape is None:
            rejection_reasons.append("NO_PULLBACK_STRUCTURE")

        if rejection_reasons:
            return EvaluationResult(
                strategy_id=self.strategy_id,
                symbol=symbol,
                evaluated_at=evaluated_at,
                state=STATE_NO_SETUP,
                signal=False,
                rejection_reasons=";".join(rejection_reasons),
                input_snapshot=snapshot,
            )

        rally_high = shape["rally_high"]
        rally_window_low = shape["rally_window_low"]
        pullback_low = shape["pullback_low"]
        pullback_close = shape["pullback_close"]
        pullback_volume = shape["pullback_volume"]
        rally_volume = shape["rally_volume"]

        snapshot.update(
            {
                "rally_high": rally_high,
                "rally_window_low": rally_window_low,
                "pullback_low": pullback_low,
                "pullback_high": float(pullback_close.max()),
            }
        )

        rally_percent = (
            ((rally_high - rally_window_low) / rally_window_low) * 100.0
            if rally_window_low > 0
            else 0.0
        )
        if rally_percent < cfg.RALLY_MIN_PERCENT:
            rejection_reasons.append("RALLY_TOO_SMALL")

        pullback_depth_percent = (
            ((rally_high - pullback_low) / rally_high) * 100.0 if rally_high > 0 else 0.0
        )
        if not (cfg.MIN_PULLBACK_DEPTH_PERCENT <= pullback_depth_percent <= cfg.MAX_PULLBACK_DEPTH_PERCENT):
            rejection_reasons.append("PULLBACK_DEPTH_OUT_OF_RANGE")

        rally_avg_volume = float(rally_volume.mean()) if len(rally_volume) else 0.0
        pullback_avg_volume = float(pullback_volume.mean()) if len(pullback_volume) else 0.0
        if not (pullback_avg_volume < rally_avg_volume):
            rejection_reasons.append("PULLBACK_VOLUME_NOT_DECLINING")

        breakout_level = max(rally_high, float(pullback_close.max()))
        breakout_volume_threshold = pullback_avg_volume * cfg.VOLUME_EXPANSION_MULTIPLIER
        latest_volume = float(volume.iloc[-1])

        snapshot.update(
            {
                "rally_percent": rally_percent,
                "pullback_depth_percent": pullback_depth_percent,
                "rally_avg_volume": rally_avg_volume,
                "pullback_avg_volume": pullback_avg_volume,
                "breakout_level": breakout_level,
                "latest_volume": latest_volume,
            }
        )

        if not (latest_close > breakout_level):
            rejection_reasons.append("NO_BREAKOUT")
        if not (latest_volume > breakout_volume_threshold):
            rejection_reasons.append("NO_VOLUME_REEXPANSION")

        if rejection_reasons:
            return EvaluationResult(
                strategy_id=self.strategy_id,
                symbol=symbol,
                evaluated_at=evaluated_at,
                state=STATE_NO_SETUP,
                signal=False,
                rejection_reasons=";".join(rejection_reasons),
                input_snapshot=snapshot,
            )

        return EvaluationResult(
            strategy_id=self.strategy_id,
            symbol=symbol,
            evaluated_at=evaluated_at,
            state=STATE_ENTRY_SIGNAL,
            signal=True,
            entry_reason=(
                "price>VWAP, EMA9>EMA21, rally+shallow pullback with declining "
                "volume, breakout with volume re-expansion"
            ),
            input_snapshot=snapshot,
        )

    def generate_entry(self, bars: pd.DataFrame, *, symbol: str, as_of=None) -> EvaluationResult:
        evaluation = self.evaluate_setup(bars, symbol=symbol, as_of=as_of)
        if not evaluation.signal:
            return evaluation

        # ASSUMPTION (DECISION_LOG.md): entry is taken at the breakout bar's
        # close, not at breakout_level + a fixed offset -- the constitution
        # describes the breakout condition but not an exact entry-price
        # rule, and using the actual traded close avoids fabricating a price
        # that never printed.
        entry_price = float(_column(bars, "Close").iloc[-1])
        stop_price = self.calculate_stop(bars, entry_price=entry_price)
        targets = self.calculate_targets(entry_price=entry_price, stop_price=stop_price)

        return EvaluationResult(
            strategy_id=evaluation.strategy_id,
            symbol=evaluation.symbol,
            evaluated_at=evaluation.evaluated_at,
            state=evaluation.state,
            signal=True,
            entry_reason=evaluation.entry_reason,
            entry_price=entry_price,
            stop_price=stop_price,
            target_1=targets["target_1"],
            target_2=targets["target_2"],
            risk_per_share=targets["risk_per_share"],
            confidence_score=NOT_EVALUATED,  # ASSUMPTION: no scoring model defined yet (Phase 6)
            input_snapshot=evaluation.input_snapshot,
        )

    def calculate_stop(self, bars: pd.DataFrame, *, entry_price: float) -> float:
        """Stop = micro-pullback low, widened (if necessary) so the
        entry-to-stop distance is never less than an ATR-based minimum
        buffer (constitution: 손절 위치 = "micro-pullback low (with an
        ATR-based minimum buffer)")."""
        close = _column(bars, "Close")
        volume = _column(bars, "Volume")
        shape = _find_rally_and_pullback(close, volume)
        if shape is None:
            raise ValueError("calculate_stop() requires bars with a detectable pullback structure")
        pullback_low = shape["pullback_low"]

        atr_series = compute_atr(bars, cfg.ATR_PERIOD)
        latest_atr = atr_series.iloc[-1]
        min_buffer = (
            float(latest_atr) * cfg.ATR_STOP_MULTIPLIER if pd.notna(latest_atr) else 0.0
        )

        candidate_stop = pullback_low
        widened_stop = entry_price - min_buffer
        stop_price = min(candidate_stop, widened_stop)

        if stop_price >= entry_price:
            # Degenerate/adversarial input (e.g. entry_price below the
            # pullback low already) -- fail closed rather than return a
            # stop that isn't actually below entry.
            raise ValueError(
                f"Computed stop {stop_price} is not below entry_price {entry_price}"
            )
        return float(stop_price)

    def calculate_targets(self, *, entry_price: float, stop_price: float) -> dict:
        if stop_price >= entry_price:
            raise ValueError("stop_price must be below entry_price for a long position")

        risk_per_share = entry_price - stop_price
        target_1 = entry_price + risk_per_share * cfg.TARGET_1_R_MULTIPLE
        target_2 = entry_price + risk_per_share * cfg.TARGET_2_R_MULTIPLE

        return {
            "risk_per_share": float(risk_per_share),
            "target_1": float(target_1),
            "target_2": float(target_2),
            # ASSUMPTION (DECISION_LOG.md): documented policy is "1R 도달 시
            # 50% 분할 익절" -- the fraction itself (0.5) is explicit in the
            # constitution's flow description; target_2's R-multiple is not
            # and is recorded separately in config/scalping_strategy_v1_config.py.
            "partial_exit_fraction_at_target_1": cfg.PARTIAL_EXIT_FRACTION_AT_TARGET_1,
        }
