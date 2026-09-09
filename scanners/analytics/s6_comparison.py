"""Read-only S1-S5 versus S6 discovery comparison for S6-captured names.

This module is deliberately downstream of every scanner.  It consumes stored
signals/outcomes and never feeds a ranking, watch list, candidate file, or
order path.  Missing timestamps/outcomes remain ``None``/unmeasured.
"""
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

from scanners.analytics.daily_scanner_summary import SCANNER_NUMBERS

S6 = "orb"


def _utc(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _number(value):
    try:
        out = float(value)
        return out if out == out else None
    except (TypeError, ValueError):
        return None


def _mean(values):
    measured = [v for v in values if v is not None]
    return sum(measured) / len(measured) if measured else None


def build(trading_day: str, *, s6_candidates: Iterable[Dict[str, Any]],
          signals: Iterable[Dict[str, Any]], performance: Dict[str, Dict[str, Any]],
          fills: Iterable[Dict[str, Any]] = ()) -> Dict[str, Any]:
    candidates = list(s6_candidates or ())
    captured = {str(r.get("symbol") or "").upper() for r in candidates if r.get("symbol")}
    origin = _utc(f"{trading_day}T04:00:00-04:00")
    fill_symbols = {str(r.get("symbol") or "").upper() for r in fills
                    if r.get("symbol") and (r.get("entry_time") or r.get("entry_filled_at"))}
    rows = []
    details = []
    signal_rows = list(signals or ())
    for symbol in sorted(captured):
        item = {"symbol": symbol}
        for scanner, label in SCANNER_NUMBERS.items():
            stamps = [_utc(r.get("timestamp")) for r in signal_rows
                      if str(r.get("scanner_name")) == scanner and
                      str(r.get("symbol") or "").upper() == symbol]
            stamps = [s for s in stamps if s]
            item[f"{label.lower()}_detected"] = bool(stamps)
            item[f"{label.lower()}_first_detection_at"] = (
                min(stamps).isoformat() if stamps else None)
        own = [r for r in candidates if str(r.get("symbol") or "").upper() == symbol]
        provenance = [dict(r.get("provenance") or {}) for r in own]
        def earliest(*keys):
            values = [_utc(p.get(key)) for p in provenance for key in keys]
            values = [v for v in values if v]
            return min(values).isoformat() if values else None
        item["s6_first_discovery_at"] = earliest("candidate_discovered_at",
                                                  "symbol_evaluated_at")
        item["s6_active_watch_at"] = earliest("watchlist_added_at")
        item["s6_orb5_signal_at"] = earliest("signal_timestamp")
        details.append(item)
    names = list(SCANNER_NUMBERS) + [S6]
    for name in names:
        if name == S6:
            mine = candidates
        else:
            mine = [r for r in signal_rows
                    if str(r.get("scanner_name")) == name and
                    str(r.get("symbol") or "").upper() in captured]
        symbols = {str(r.get("symbol") or "").upper() for r in mine if r.get("symbol")}
        delays = []
        returns = []
        mfes = []
        maes = []
        for row in mine:
            stamp = _utc(row.get("timestamp") or row.get("generated_at") or
                         (row.get("provenance") or {}).get("candidate_published_at"))
            if stamp and origin:
                delays.append((stamp - origin).total_seconds() / 60.0)
            outcome = performance.get(str(row.get("signal_id") or "")) or {}
            returns.append(_number(outcome.get("return_30m")))
            mfes.append(_number(outcome.get("mfe_30m")))
            maes.append(_number(outcome.get("mae_30m")))
        measured_returns = [v for v in returns if v is not None]
        rows.append({
            "scanner_name": name,
            "label": "S6" if name == S6 else SCANNER_NUMBERS[name],
            "s6_captured_symbols": len(captured),
            "candidate_symbols": len(symbols),
            "candidate_ratio": (len(symbols) / len(captured) if captured else None),
            "mean_time_to_candidate_minutes": _mean(delays),
            "fill_conversion": (len(symbols & fill_symbols) / len(symbols)
                                if symbols else None),
            "false_positive_proxy": (sum(v <= 0 for v in measured_returns) /
                                     len(measured_returns) if measured_returns else None),
            "mean_mfe_30m": _mean(mfes),
            "mean_mae_30m": _mean(maes),
            "outcomes_measured": len(measured_returns),
        })
    return {"trading_day": trading_day, "scope": "symbols captured by S6",
            "routing_effect": "NONE_READ_ONLY", "symbols": details,
            "rows": rows}


def load(trading_day: str, *, s6_candidates, fills=()):
    from scanners.base import result_store
    return build(trading_day, s6_candidates=s6_candidates,
                 signals=result_store.read_signal_rows(trading_day),
                 performance=result_store.read_performance(trading_day), fills=fills)
