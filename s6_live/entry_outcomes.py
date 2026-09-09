"""What happened after an S6 entry -- real ORB5 fills and ORB15 shadow
signals alike -- measured from the same bars, at the same horizons.

The question this answers
-------------------------
"Did entering on the 5-minute range reduce the loss, or did it just
produce more candidates?" needs, for one opportunity, the ORB5 entry
and the ORB15 signal side by side with what price did afterwards. The
position book gives the real entry (price, time, exit); the range-shadow
log gives the instant the ORB15 watch first said READY; the collected
bars give the path. This module joins them and writes one row per
(symbol, variant) per day:

    mfe_5m / mae_5m / mfe_15m / mae_15m / mfe_30m / mae_30m / mfe_60m / mae_60m
    and, for real fills, realized_pnl, realized_return_pct, exit_reason,
    holding_minutes.

MFE/MAE are measured from the entry price against bar highs/lows in the
window (entry_time, entry_time + horizon]. A horizon whose window has no
bars is None, never zero. A shadow row is marked `shadow: true` and
`order_capable: false`; nothing downstream may count it as a fill.

Bars come from `bars_for(symbol)` -- by default the day's persisted
collector store, optionally a backfill directory -- and only bars AFTER
the entry instant are used, so no outcome can leak into a feature.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

HORIZONS = (5, 15, 30, 60)
SUBDIR = "entry_outcomes"


def log_path(trading_day, *, env=None) -> Optional[Path]:
    env = env if env is not None else os.environ
    root = env.get("ENTRY_OUTCOMES_DIR") or env.get("SCANNER_DATA_ROOT")
    if not root:
        return None
    return Path(root) / SUBDIR / f"{trading_day}.jsonl"


def _utc(moment):
    if moment is None:
        return None
    if isinstance(moment, str):
        moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def excursions(bars: Sequence[Any], *, entry_time, entry_price,
               horizons=HORIZONS) -> Dict[str, Optional[float]]:
    """MFE/MAE in percent per horizon from bars AFTER `entry_time`.

    `bars` are objects or dicts with minute/high/low. Only bars whose
    minute is strictly after the entry instant count, and a horizon with
    no such bars is None.
    """
    start = _utc(entry_time)
    out: Dict[str, Optional[float]] = {}
    if start is None or not entry_price:
        for h in horizons:
            out[f"mfe_{h}m"] = None
            out[f"mae_{h}m"] = None
        return out
    rows = []
    for bar in bars or ():
        minute = _utc(bar.get("minute") if isinstance(bar, dict) else getattr(bar, "minute", None))
        if minute is None or minute <= start:
            continue
        high = bar.get("high") if isinstance(bar, dict) else getattr(bar, "high", None)
        low = bar.get("low") if isinstance(bar, dict) else getattr(bar, "low", None)
        try:
            rows.append((minute, float(high), float(low)))
        except (TypeError, ValueError):
            continue
    for h in horizons:
        end = start + timedelta(minutes=h)
        window = [(hi, lo) for m, hi, lo in rows if m <= end]
        if not window:
            out[f"mfe_{h}m"] = None
            out[f"mae_{h}m"] = None
            continue
        out[f"mfe_{h}m"] = (max(hi for hi, _ in window) / entry_price - 1.0) * 100.0
        out[f"mae_{h}m"] = (min(lo for _, lo in window) / entry_price - 1.0) * 100.0
    return out


def _breakout_age(payload):
    """`breakout_age_minutes` from a stored entry-quality snapshot."""
    if not payload:
        return None
    try:
        data = payload if isinstance(payload, dict) else json.loads(payload)
    except (TypeError, ValueError):
        return None
    value = data.get("breakout_age_minutes")
    return float(value) if isinstance(value, (int, float)) else None


def fill_rows(conn, trading_day: str, *, session="PREMARKET") -> List[Dict[str, Any]]:
    """Real S6 entries of the day with a fill, from the position book."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(s6_positions)")]
    wanted = ["position_id", "symbol", "entry_session", "range_minutes", "entry_price",
              "entry_time", "exit_price", "exit_reason", "closed_at", "quantity",
              "submitted_at", "status"]
    wanted += [c for c in ("scanner_variant", "entry_quality_json") if c in cols]
    rows = conn.execute(
        "SELECT " + ", ".join(wanted) + " FROM s6_positions "
        "WHERE substr(created_at, 1, 10) = ? AND entry_price IS NOT NULL",
        (trading_day,)).fetchall()
    out = []
    for row in rows:
        record = dict(zip(wanted, row))
        if session and str(record.get("entry_session") or "").upper() != session:
            continue
        out.append(record)
    return out


def build(trading_day: str, *, fills: Iterable[Dict[str, Any]],
          shadow_ready: Dict[str, Dict[str, Any]], bars_for: Callable[[str], Sequence[Any]],
          session="PREMARKET") -> List[Dict[str, Any]]:
    """One outcome row per real fill and per ORB15 shadow signal."""
    rows: List[Dict[str, Any]] = []
    for fill in fills:
        symbol = str(fill.get("symbol") or "").upper()
        entry_time = _utc(fill.get("entry_time") or fill.get("submitted_at"))
        entry_price = fill.get("entry_price")
        try:
            bars = bars_for(symbol)
        except Exception:  # noqa: BLE001
            bars = []
        exc = excursions(bars, entry_time=entry_time, entry_price=entry_price)
        exit_price = fill.get("exit_price")
        realized = None
        realized_pct = None
        holding = None
        if exit_price is not None and entry_price:
            realized = (float(exit_price) - float(entry_price)) * int(fill.get("quantity") or 0)
            realized_pct = (float(exit_price) / float(entry_price) - 1.0) * 100.0
            closed = _utc(fill.get("closed_at"))
            if closed and entry_time:
                holding = (closed - entry_time).total_seconds() / 60.0
        rows.append({
            "trading_day": trading_day, "session": session, "symbol": symbol,
            "scanner_variant": fill.get("scanner_variant") or f"S6_ORB{fill.get('range_minutes')}",
            "range_minutes": fill.get("range_minutes"),
            "shadow": False, "order_capable": True,
            "position_id": fill.get("position_id"),
            "entry_time": entry_time.isoformat() if entry_time else None,
            "entry_price": entry_price,
            "realized_pnl": realized, "realized_return_pct": realized_pct,
            "exit_reason": fill.get("exit_reason"), "holding_minutes": holding,
            "bars_available": bool(bars),
            "breakout_age_minutes": _breakout_age(fill.get("entry_quality_json")),
            **exc,
        })
    for symbol, signal in sorted(shadow_ready.items()):
        entry_time = _utc(signal.get("evaluated_at"))
        entry_price = signal.get("price")
        try:
            bars = bars_for(symbol)
        except Exception:  # noqa: BLE001
            bars = []
        exc = excursions(bars, entry_time=entry_time, entry_price=entry_price)
        rows.append({
            "trading_day": trading_day, "session": session, "symbol": str(symbol).upper(),
            "scanner_variant": signal.get("scanner_variant") or "S6_ORB15_SHADOW",
            "range_minutes": signal.get("range_minutes"),
            "shadow": True, "order_capable": False,
            "position_id": None,
            "entry_time": entry_time.isoformat() if entry_time else None,
            "entry_price": entry_price,
            "realized_pnl": None, "realized_return_pct": None,
            "exit_reason": None, "holding_minutes": None,
            "bars_available": bool(bars),
            "breakout_age_minutes": (signal.get("entry_quality") or {}).get(
                "breakout_age_minutes"),
            **exc,
        })
    return rows


def write(rows: Iterable[Dict[str, Any]], *, trading_day, env=None) -> int:
    """Replace the day's file (outcomes are recomputed, not appended)."""
    path = log_path(trading_day, env=env)
    if path is None:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows)
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, default=str) + "\n")
    return len(rows)


def read(trading_day, *, env=None) -> List[Dict[str, Any]]:
    path = log_path(trading_day, env=env)
    if path is None or not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def summarise(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per scanner_variant: count and mean MFE/MAE per horizon (measured
    rows only). Shadow rows keep their own variant so they can never be
    mistaken for fills."""
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        key = "%s|%s" % (str(row.get("session") or "UNKNOWN"),
                         str(row.get("scanner_variant")))
        buckets.setdefault(key, []).append(row)
    out: Dict[str, Dict[str, Any]] = {}
    for variant, members in buckets.items():
        session_name, _, variant_name = variant.partition("|")
        stats: Dict[str, Any] = {"count": len(members),
                                 "session": session_name,
                                 "scanner_variant": variant_name,
                                 "shadow": all(bool(m.get("shadow")) for m in members)}
        ages = [m["breakout_age_minutes"] for m in members
                if m.get("breakout_age_minutes") is not None]
        stats["avg_breakout_age_minutes"] = (sum(ages) / len(ages)) if ages else None
        for h in HORIZONS:
            for kind in ("mfe", "mae"):
                values = [m[f"{kind}_{h}m"] for m in members
                          if m.get(f"{kind}_{h}m") is not None]
                stats[f"avg_{kind}_{h}m"] = (sum(values) / len(values)) if values else None
        realized = [m["realized_return_pct"] for m in members
                    if m.get("realized_return_pct") is not None]
        stats["avg_realized_return_pct"] = (sum(realized) / len(realized)) if realized else None
        out[variant] = stats
    return out
