"""Read-only parity evaluation for the deployed S6 ORB scanner.

This module is intentionally an adapter, not a strategy implementation.  It
normalizes REST candle records and calls ``OpeningRangeBreakoutScanner`` plus
``build_rows`` directly.  It has no broker/order imports and never reads a
WebSocket trade stream: Toss volume comes only from its verified REST candles.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

from brokers.toss_shadow_adapter import TossShadowAdapter
from scanners.base.market_data_provider import SymbolData
from scanners.base.models import ScannerDataError
from scanners.orb.scanner import OpeningRangeBreakoutScanner
from scanners.publish.candidates import build_rows
from validation.broker_shadow_compare import parse_toss_candles


PHASE_A_SYMBOLS = ("AAPL", "NVDA", "QQQ", "AMD", "PLTR", "TSLA", "MSFT", "AMZN", "META", "AVGO")
EXECUTION_BLOCKED = "TOSS_SHADOW_EXECUTION_BLOCKED"
ALIGNMENT_FAILED = "BAR_ALIGNMENT_FAILED"
PROVIDER_DATA_GAP = "PROVIDER_DATA_GAP"

DATA_REQUIREMENTS = (
    {"field": "OHLCV completed 1m bars", "production_source": "OpeningRangeBreakoutScanner.check",
     "kis_source": "market_data.kis_minute_chart.fetch", "toss_source": "GET /api/v1/candles (REST)",
     "normalization": "UTC minute, strict identical grid", "verified": True},
    {"field": "session / opening range", "production_source": "scanners.base.session.slice_session",
     "kis_source": "bar timestamps", "toss_source": "candle timestamp", "normalization": "scanner converts to America/New_York", "verified": True},
    {"field": "VWAP / EMA9 / EMA21 / volume expansion", "production_source": "scanners.orb.scanner + scanners.base.indicators",
     "kis_source": "completed REST candle OHLCV", "toss_source": "completed REST candle OHLCV", "normalization": "same normalized DataFrame", "verified": True},
    {"field": "daily shared features", "production_source": "scanners.base.features.build_features",
     "kis_source": "configured existing daily fallback", "toss_source": "not exposed by verified Toss 1m endpoint",
     "normalization": "one declared common daily frame; not an ORB gate input", "verified": False},
)


@dataclass(frozen=True)
class NormalizedBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Evaluation:
    symbol: str
    provider: str
    qualified: bool
    score: float | None
    signal: Any | None
    rejection: str | None = None
    error: str | None = None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _minute(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc).replace(second=0, microsecond=0)


def normalize_bars(rows: Sequence[Mapping[str, Any]], *, now: datetime | None = None) -> list[NormalizedBar]:
    """Normalize only completed bars. Invalid/OHLC-incomplete rows are refused."""
    cutoff = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(second=0, microsecond=0)
    normalized: dict[datetime, NormalizedBar] = {}
    for row in rows or ():
        stamp = _minute(row.get("at", row.get("timestamp"))) if isinstance(row, Mapping) else None
        values = [_number(row.get(key)) for key in ("open", "high", "low", "close", "volume")] if isinstance(row, Mapping) else []
        if stamp is None or stamp >= cutoff or any(value is None for value in values):
            continue
        open_, high, low, close, volume = values
        if open_ <= 0 or high <= 0 or low <= 0 or close <= 0 or volume < 0 or low > high:
            continue
        normalized[stamp] = NormalizedBar(stamp, open_, high, low, close, volume)
    return [normalized[key] for key in sorted(normalized)]


def toss_rest_bars(adapter: TossShadowAdapter, symbol: str, *, now: datetime | None = None) -> list[NormalizedBar]:
    """Toss REST only; this is deliberately the sole Toss-volume input."""
    return normalize_bars(parse_toss_candles(adapter.get_1m_candles(symbol).data), now=now)


def align_bars(kis: Sequence[NormalizedBar], toss: Sequence[NormalizedBar]) -> tuple[list[NormalizedBar], list[NormalizedBar], str | None]:
    """Fail closed unless both providers have the exact completed-bar grid.

    We do not fill absent minutes or compute indicators on a shortened common
    intersection, because that changes the ORB/VWAP/EMA input series.
    """
    left, right = [bar.timestamp for bar in kis], [bar.timestamp for bar in toss]
    if not left or not right or left != right:
        return [], [], ALIGNMENT_FAILED
    return list(kis), list(toss), None


def bars_frame(bars: Sequence[NormalizedBar]) -> pd.DataFrame:
    return pd.DataFrame(
        [{"Open": bar.open, "High": bar.high, "Low": bar.low, "Close": bar.close, "Volume": bar.volume} for bar in bars],
        index=pd.DatetimeIndex([bar.timestamp for bar in bars], name="Datetime"),
    )


def _relative(left: float | None, right: float | None) -> float | None:
    if left is None or right is None or left == 0:
        return None
    return abs(left - right) / abs(left) * 100.0


def _classification(diff: float | None) -> str:
    if diff is None:
        return "NOT_COMPARABLE"
    return "MATCH" if diff <= 0.10 else "MINOR_DIFF" if diff <= 0.30 else "MAJOR_DIFF"


def opening_range_summary(bars: Sequence[NormalizedBar], *, minutes: int) -> Mapping[str, Any]:
    if len(bars) < minutes:
        return {"classification": "NOT_COMPARABLE", "reason": "INSUFFICIENT_OPENING_BARS"}
    opening = list(bars[:minutes])
    return {"start": opening[0].timestamp.isoformat(), "end": opening[-1].timestamp.isoformat(),
            "high": max(bar.high for bar in opening), "low": min(bar.low for bar in opening), "bar_count": len(opening)}


def compare_opening_range(kis: Sequence[NormalizedBar], toss: Sequence[NormalizedBar], *, minutes: int) -> Mapping[str, Any]:
    left, right = opening_range_summary(kis, minutes=minutes), opening_range_summary(toss, minutes=minutes)
    if "reason" in left or "reason" in right:
        return {"classification": "NOT_COMPARABLE", "kis": left, "toss": right}
    same_grid = (left["start"], left["end"], left["bar_count"]) == (right["start"], right["end"], right["bar_count"])
    high_diff, low_diff = _relative(left["high"], right["high"]), _relative(left["low"], right["low"])
    diff = max(high_diff, low_diff) if high_diff is not None and low_diff is not None else None
    return {"classification": _classification(diff) if same_grid else "NOT_COMPARABLE", "max_relative_diff_percent": diff,
            "kis": left, "toss": right}


def _evaluation(scanner: OpeningRangeBreakoutScanner, symbol: str, provider: str, intraday: pd.DataFrame, daily: pd.DataFrame, *, session: str, trading_day: str) -> Evaluation:
    reasons: list[str] = []
    try:
        signal = scanner.evaluate(SymbolData(symbol=symbol, daily=daily, intraday=intraday, provider_name=provider,
                                              intraday_interval="1m"), trading_day=trading_day, session=session,
                                  reject_sink=lambda _symbol, reason: reasons.append(str(reason)))
    except ScannerDataError as exc:
        return Evaluation(symbol, provider, False, None, None, error=type(exc).__name__)
    except Exception as exc:  # provider-isolated validation error; do not print opaque values
        return Evaluation(symbol, provider, False, None, None, error=type(exc).__name__)
    if signal is None:
        return Evaluation(symbol, provider, False, None, None, rejection=reasons[0] if reasons else "REJECTED")
    return Evaluation(symbol, provider, True, float(signal.scanner_score), signal)


def _metrics(evaluation: Evaluation) -> Mapping[str, Any]:
    return (getattr(evaluation.signal, "metrics", None) or {}) if evaluation.signal else {}


def _indicator_pair(left: Evaluation, right: Evaluation, field: str) -> Mapping[str, Any]:
    left_value, right_value = _metrics(left).get(field), _metrics(right).get(field)
    left_number, right_number = _number(left_value), _number(right_value)
    return {"kis": left_number, "toss": right_number, "absolute_diff": None if left_number is None or right_number is None else abs(left_number - right_number),
            "relative_diff_percent": _relative(left_number, right_number), "classification": _classification(_relative(left_number, right_number))}


def evaluate_symbol(symbol: str, *, kis_rows: Sequence[Mapping[str, Any]], toss_rows: Sequence[Mapping[str, Any]], daily: pd.DataFrame,
                    session: str = "REGULAR", trading_day: str | None = None, now: datetime | None = None) -> Mapping[str, Any]:
    """Evaluate one aligned symbol through the exact deployed S6 scanner."""
    kis, toss = normalize_bars(kis_rows, now=now), normalize_bars(toss_rows, now=now)
    kis, toss, alignment_error = align_bars(kis, toss)
    if alignment_error:
        return {"symbol": symbol, "status": alignment_error, "kis_bars": len(normalize_bars(kis_rows, now=now)),
                "toss_bars": len(normalize_bars(toss_rows, now=now))}
    scanner = OpeningRangeBreakoutScanner()
    day = trading_day or kis[-1].timestamp.date().isoformat()
    left = _evaluation(scanner, symbol, "KIS", bars_frame(kis), daily, session=session, trading_day=day)
    right = _evaluation(scanner, symbol, "TOSS", bars_frame(toss), daily, session=session, trading_day=day)
    opening = compare_opening_range(kis, toss, minutes=scanner.orb_minutes(session))
    def record(item: Evaluation) -> Mapping[str, Any]:
        # Keep the production signal private to this in-memory validation
        # result.  Report writers must omit ``_signal`` before JSON encoding.
        return {"provider": item.provider, "qualified": item.qualified, "score": item.score,
                "rejection": item.rejection, "error": item.error, "signal": item.signal}
    return {"symbol": symbol, "status": "OK", "alignment": {"completed_bars": len(kis)}, "opening_range": opening,
            "kis": record(left), "toss": record(right), "vwap": _indicator_pair(left, right, "vwap"),
            "ema9": _indicator_pair(left, right, "session_ema9"), "ema21": _indicator_pair(left, right, "session_ema21"),
            "volume_expansion": _indicator_pair(left, right, "volume_expansion"),
            "qualification_same": left.qualified == right.qualified,
            "first_divergence": None if left.qualified == right.qualified else (left.rejection or right.rejection or left.error or right.error)}


def candidate_parity(evaluations: Iterable[Mapping[str, Any]], *, trading_day: str, session: str) -> Mapping[str, Any]:
    """Rank qualifying production signals using production ``build_rows``."""
    rows = list(evaluations)
    signals = {provider: [item[provider]["signal"] for item in rows if item.get("status") == "OK" and item[provider]["qualified"]]
               for provider in ("kis", "toss")}
    published = {provider: build_rows(signals[provider], strategy_id="S6_ORB_BREAKOUT_V1", trading_day=trading_day, session=session)
                 for provider in signals}
    symbols = {provider: {row.symbol for row in published[provider]} for provider in published}
    common, union = symbols["kis"] & symbols["toss"], symbols["kis"] | symbols["toss"]
    ranks = {provider: {row.symbol: row.rank for row in published[provider]} for provider in published}
    top = {str(size): len({row.symbol for row in published["kis"][:size]} & {row.symbol for row in published["toss"][:size]}) for size in (10, 20, 50)}
    return {"kis": sorted(symbols["kis"]), "toss": sorted(symbols["toss"]), "intersection": sorted(common),
            "kis_only": sorted(symbols["kis"] - symbols["toss"]), "toss_only": sorted(symbols["toss"] - symbols["kis"]),
            "jaccard": len(common) / len(union) if union else 1.0, "top_overlap": top,
            "rank_delta": {symbol: abs(ranks["kis"][symbol] - ranks["toss"][symbol]) for symbol in common}}


def execution_safety() -> Mapping[str, str]:
    return {"BUY": "BLOCKED", "SELL": "BLOCKED", "CANCEL": "BLOCKED", "MODIFY": "BLOCKED"}


def report(evaluations: Sequence[Mapping[str, Any]], *, trading_day: str, session: str) -> Mapping[str, Any]:
    """Return a credential-safe, JSON-serializable parity report.

    The live active-watch API persists a shared file, so it is deliberately
    not invoked here.  This states the limitation rather than creating a
    synthetic action-pool result.
    """
    valid = [item for item in evaluations if item.get("status") == "OK"]
    same = sum(bool(item.get("qualification_same")) for item in valid)
    score_diffs = [abs(item["kis"]["score"] - item["toss"]["score"])
                   for item in valid if item["kis"].get("score") is not None and item["toss"].get("score") is not None]
    candidates = candidate_parity(valid, trading_day=trading_day, session=session)
    serial_rows = []
    for item in evaluations:
        row = dict(item)
        for provider in ("kis", "toss"):
            if isinstance(row.get(provider), Mapping):
                row[provider] = {key: value for key, value in row[provider].items() if key != "signal"}
        serial_rows.append(row)
    return {
        "identity": {"scanner": "scanners.orb.scanner.OpeningRangeBreakoutScanner", "mode": "SHADOW_READ_ONLY"},
        "provider_data_mapping": list(DATA_REQUIREMENTS), "session": session,
        "gate_parity": {"total": len(valid), "same": same, "diverged": len(valid) - same,
                        "parity_percent": (same / len(valid) * 100.0) if valid else None},
        "score_parity": {"mean_diff": statistics.mean(score_diffs) if score_diffs else None,
                         "median_diff": statistics.median(score_diffs) if score_diffs else None,
                         "max_diff": max(score_diffs) if score_diffs else None},
        "candidate_parity": candidates,
        "action_pool_parity": {"status": "NOT_EVALUATED_RUNTIME_STATE_REQUIRED",
                               "reason": "s6_live.active_watch persists shared runtime state and is not called by validation"},
        "execution_safety": execution_safety(), "rows": serial_rows,
    }
