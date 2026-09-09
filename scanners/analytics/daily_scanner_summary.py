"""The one daily message stock-sanner receives: S1-S5, one trading day.

Where the numbers come from
---------------------------
* signals            result_store.read_signal_rows(day)      -- what each
                     scanner found, deduplicated on signal_id
* scanned symbols    result_store.read_run_manifests(day)     -- symbols_seen
                     per scanner, the largest run of the day
* forward outcomes   result_store.read_performance(day)       -- the
                     tracker's return / MFE / MAE per signal
* live entries       s1_live_trades (S1 is the only S1-S5 scanner with
                     an executor; S2-S5 are DISCOVERY_ONLY and connect to
                     nothing by construction)

What "win" and "loss" mean here
-------------------------------
For a scanner WITH live trades on the day, a win is a closed trade with
positive gross PnL. For every other scanner a candidate is a win when
its forward return at the summary horizon is positive -- the horizon is
the first of return_1h, return_2h, return_30m, return_close, return_1d
that the tracker has measured for that signal, and the message names
the horizon it used. A candidate with no measured return is neither: it
is counted under 미측정 and never invented.

Nothing here reads the order path or changes a scanner.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Scanner name -> S-number. S6 is deliberately absent: it is a trading
#: strategy reported by the trading report, not a scanner summary.
SCANNER_NUMBERS = {
    "hma_early_trend": "S1",
    "accumulation": "S2",
    "breakout_ready": "S3",
    "premarket_momentum": "S4",
    "gap_pullback": "S5",
}
ORDER = ("hma_early_trend", "accumulation", "breakout_ready",
         "premarket_momentum", "gap_pullback")

RETURN_HORIZONS = ("return_1h", "return_2h", "return_30m", "return_close", "return_1d")
MFE_FIELDS = ("mfe_1h", "mfe_30m", "mfe_15m", "mfe_1d")
MAE_FIELDS = ("mae_1h", "mae_30m", "mae_15m", "mae_1d")

NO_DATA = "데이터 없음"


def _num(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _mean(values: List[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _first_measured(record: Dict[str, Any], fields) -> Optional[tuple]:
    for name in fields:
        value = _num(record.get(name))
        if value is not None:
            return name, value
    return None


def _scanned_by_scanner(manifests: List[Dict[str, Any]]) -> Dict[str, int]:
    """Largest symbols_seen per scanner across the day's runs."""
    seen: Dict[str, int] = {}
    for manifest in manifests or []:
        candidates = []
        if isinstance(manifest.get("outcomes"), list):
            candidates += [o for o in manifest["outcomes"] if isinstance(o, dict)]
        if isinstance(manifest.get("scanners"), list):
            candidates += [o for o in manifest["scanners"] if isinstance(o, dict)]
        if manifest.get("scanner_name"):
            candidates.append(manifest)
        for item in candidates:
            name = str(item.get("scanner_name") or "")
            value = item.get("symbols_seen")
            if name in SCANNER_NUMBERS and isinstance(value, int):
                seen[name] = max(seen.get(name, 0), value)
    return seen


def _failures(manifests: List[Dict[str, Any]]) -> List[str]:
    out = []
    for manifest in manifests or []:
        status = str(manifest.get("status") or "")
        if status.startswith("FAILED") or status == "PARTIAL":
            profile = manifest.get("profile") or manifest.get("session") or "run"
            out.append(f"{profile}: {status}")
        for name, reason in (manifest.get("construction_failures") or {}).items():
            out.append(f"{name}: FAILED_NOT_BUILT ({str(reason)[:60]})")
    return out


def _s1_trades(conn, trading_day: str) -> List[Dict[str, Any]]:
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT source_signal_id, entry_filled_at, exit_filled_at, gross_pnl, "
            "entry_price, exit_price, qty FROM s1_live_trades WHERE trading_day = ?",
            (trading_day,)).fetchall()
    except Exception:  # noqa: BLE001
        logger.warning("s1_live_trades unavailable", exc_info=True)
        return []
    keys = ("source_signal_id", "entry_filled_at", "exit_filled_at", "gross_pnl",
            "entry_price", "exit_price", "qty")
    return [dict(zip(keys, row)) for row in rows]


def build(trading_day: str, *, signals=None, performance=None, manifests=None,
          conn=None) -> Dict[str, Any]:
    """The summary dict. Every value is either measured or None."""
    if signals is None or performance is None or manifests is None:
        from scanners.base import result_store

        signals = result_store.read_signal_rows(trading_day) if signals is None else signals
        performance = (result_store.read_performance(trading_day)
                       if performance is None else performance)
        manifests = result_store.read_run_manifests(trading_day) if manifests is None else manifests

    scanned = _scanned_by_scanner(manifests)
    trades = _s1_trades(conn, trading_day)
    trades_by_signal = {str(t.get("source_signal_id")): t for t in trades
                        if t.get("source_signal_id")}

    rows: List[Dict[str, Any]] = []
    for name in ORDER:
        mine = [s for s in signals if str(s.get("scanner_name")) == name]
        returns, mfes, maes, horizons = [], [], [], set()
        unmeasured = 0
        for signal in mine:
            record = performance.get(str(signal.get("signal_id"))) or {}
            measured = _first_measured(record, RETURN_HORIZONS)
            if measured is None:
                unmeasured += 1
            else:
                horizons.add(measured[0])
                returns.append(measured[1])
            mfe = _first_measured(record, MFE_FIELDS)
            mae = _first_measured(record, MAE_FIELDS)
            if mfe is not None:
                mfes.append(mfe[1])
            if mae is not None:
                maes.append(mae[1])
        connected = [t for t in trades if str(t.get("source_signal_id")) in
                     {str(s.get("signal_id")) for s in mine}]
        connected += [t for t in trades if name == "hma_early_trend"
                      and t not in connected and t.get("entry_filled_at")]
        closed = [t for t in connected if t.get("exit_filled_at") is not None
                  and _num(t.get("gross_pnl")) is not None]
        if closed:
            wins = sum(1 for t in closed if _num(t["gross_pnl"]) > 0)
            losses = sum(1 for t in closed if _num(t["gross_pnl"]) < 0)
            basis = "실거래"
        elif returns:
            wins = sum(1 for r in returns if r > 0)
            losses = sum(1 for r in returns if r < 0)
            basis = "후보 선행수익률"
        else:
            wins = losses = None
            basis = None
        rows.append({
            "scanner_name": name,
            "label": SCANNER_NUMBERS[name],
            "scanned": scanned.get(name),
            "candidates": len(mine) if (mine or name in scanned) else None,
            "entries": len([t for t in connected if t.get("entry_filled_at")]),
            "wins": wins, "losses": losses, "basis": basis,
            "avg_return": _mean(returns),
            "mfe": _mean(mfes), "mae": _mean(maes),
            "unmeasured": unmeasured,
            "horizons": sorted(horizons),
            "discovery_only": name != "hma_early_trend",
        })

    measurable = [r for r in rows if r["avg_return"] is not None]
    best = max(measurable, key=lambda r: r["avg_return"]) if measurable else None
    return {
        "trading_day": trading_day,
        "rows": rows,
        "best": best["label"] if best else None,
        "failures": _failures(manifests),
        "total_signals": len(signals),
    }


def _pct(value) -> str:
    return "-" if value is None else f"{value:+.2f}%"


def format_message(summary: Dict[str, Any]) -> str:
    lines = ["[스캐너 일일 성과]", str(summary.get("trading_day")), ""]
    for row in summary["rows"]:
        lines.append(row["label"])
        if row["candidates"] is None and row["scanned"] is None:
            lines += [f"  {NO_DATA}", ""]
            continue
        lines.append(f"  분석 종목: {row['scanned'] if row['scanned'] is not None else '-'}")
        lines.append(f"  후보: {row['candidates'] if row['candidates'] is not None else 0}")
        entries = f"  매수 연결: {row['entries']}"
        if row["discovery_only"]:
            entries += " (분석 전용)"
        lines.append(entries)
        if row["wins"] is None:
            lines.append(f"  수익/손실: {NO_DATA}")
        else:
            lines.append(f"  수익: {row['wins']}")
            lines.append(f"  손실: {row['losses']}")
            lines.append(f"  기준: {row['basis']}")
        lines.append(f"  평균 수익률: {_pct(row['avg_return'])}")
        lines.append(f"  MFE: {_pct(row['mfe'])}")
        lines.append(f"  MAE: {_pct(row['mae'])}")
        if row["horizons"]:
            lines.append(f"  측정 구간: {', '.join(h.replace('return_', '') for h in row['horizons'])}")
        if row["unmeasured"]:
            lines.append(f"  미측정 후보: {row['unmeasured']}")
        lines.append("")
    lines.append(f"오늘 최고 성과: {summary['best'] or '측정 불가'}")
    if summary.get("failures"):
        lines += ["", "스캐너 장애 (커버리지 영향):"] + [f"- {f}" for f in summary["failures"]]
    return "\n".join(lines)
