#!/usr/bin/env python3
"""S6 PREMARKET historical review: ORB5 vs ORB15, entry quality, and
counterfactual filters -- offline, from backfilled/collected bars.

    scripts/replay_s6_premarket.py --days 2026-09-04,2026-09-08 \
        --bars-dir <backfill dir> --db <TRADING_STATE.db copy> \
        --candidate-dir <shared/state/candidates> --out review.json

For every (day, symbol) with bars it replays minute by minute WITHOUT
future data: at each bar close it computes the entry-quality snapshot
from bars up to that minute (s6_live.entry_quality.compute) and applies
the watch's structural conditions from those same bars -- close above
the range high, price above session VWAP, EMA9 above EMA21, extension
within the scanner's limit -- for the 5-minute and the 15-minute range
independently. The first minute that qualifies is that range's signal;
the hypothetical entry is the NEXT bar's open. Outcomes are MFE/MAE at
5/15/30/60 minutes after the entry and the return at 30/60 minutes.

For real fills (s6_positions) it also measures the snapshot at the
actual submission minute and the realized result under the exit rules
that actually ran -- exits are not re-simulated.

Then it scores candidate filters (breakout age, session-high age,
recent RVOL, 5m/15m ratio, 5m return) on the ORB5 signals: for each
threshold it counts losing / winning / flat opportunities blocked and
the return, MFE and MAE of what remains. "Win" for a hypothetical entry
means the 30-minute return is positive; for a real fill the realized
return.

Nothing here touches the broker, the live config, or the state store
(the DB is opened read-only).
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from s6_live import entry_quality as eq  # noqa: E402

HORIZONS = (5, 15, 30, 60)
RANGES = (5, 15)
EXTENSION_LIMIT_PCT = 6.0
WIN_HORIZON = 30
VOLUME_EXPANSION_MIN = 1.2
EASTERN = ZoneInfo("America/New_York")


def official_origin(day):
    return datetime.fromisoformat(f"{day}T04:00:00").replace(
        tzinfo=EASTERN).astimezone(timezone.utc)


def _utc(text):
    moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def load_bars(bars_dir, day, symbol) -> List[eq.SimpleBar]:
    path = Path(bars_dir) / day / f"{symbol.upper()}.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        out.append(eq.SimpleBar(minute=_utc(row["minute"]), open=float(row["open"]),
                                high=float(row["high"]), low=float(row["low"]),
                                close=float(row["close"]), volume=float(row["volume"])))
    return sorted(out, key=lambda b: b.minute)


def _ema(values, span):
    if not values:
        return None
    k = 2.0 / (span + 1.0)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return ema


def structural_ok(bars: List[eq.SimpleBar], opening, post, or_high, *,
                  extension_limit=EXTENSION_LIMIT_PCT):
    """Both scanner and precision-watch production volume definitions."""
    closes = [b.close for b in bars]
    price = closes[-1]
    pv = sum(((b.high + b.low + b.close) / 3.0) * b.volume for b in bars)
    vol = sum(b.volume for b in bars)
    vwap = pv / vol if vol > 0 else None
    ema9 = _ema(closes, 9) if len(closes) >= 2 else None
    ema21 = _ema(closes, 21) if len(closes) >= 2 else None
    opening_mean = (sum(b.volume for b in opening) / len(opening)) if opening else None
    post_mean = (sum(b.volume for b in post) / len(post)) if post else None
    scanner_expansion = (post_mean / opening_mean
                         if opening_mean not in (None, 0) and post_mean is not None
                         else None)
    previous = bars[-21:-1]
    previous_mean = (sum(b.volume for b in previous) / len(previous)) if previous else None
    watch_expansion = (bars[-1].volume / previous_mean
                       if previous_mean not in (None, 0) else None)
    if (or_high is None or vwap is None or ema9 is None or ema21 is None
            or scanner_expansion is None or watch_expansion is None):
        return False, {"vwap": vwap, "ema9": ema9, "ema21": ema21}
    extension = (price / or_high - 1.0) * 100.0
    ok = (price > or_high and price > vwap and ema9 > ema21
          and extension <= extension_limit
          and scanner_expansion >= VOLUME_EXPANSION_MIN
          and watch_expansion >= VOLUME_EXPANSION_MIN)
    return ok, {"vwap": vwap, "ema9": ema9, "ema21": ema21,
                "extension_pct": extension,
                "scanner_volume_expansion": scanner_expansion,
                "precision_watch_volume_expansion": watch_expansion}


def excursions(bars, entry_index, entry_price):
    entry_time = bars[entry_index].minute
    out = {}
    # Include the entry bar.  The theoretical entry is its OPEN, so its
    # subsequent high/low belongs to the trade rather than to the signal.
    later = bars[entry_index:]
    for h in HORIZONS:
        end = entry_time + timedelta(minutes=h)
        window = [b for b in later if b.minute < end]
        if not window:
            out[f"mfe_{h}m"] = out[f"mae_{h}m"] = out[f"return_{h}m"] = None
            continue
        out[f"mfe_{h}m"] = (max(b.high for b in window) / entry_price - 1.0) * 100.0
        out[f"mae_{h}m"] = (min(b.low for b in window) / entry_price - 1.0) * 100.0
        out[f"return_{h}m"] = (window[-1].close / entry_price - 1.0) * 100.0
    return out


def first_signal(bars, *, orb_minutes, symbol, day, min_post_bars=3):
    """The first minute the ORB<n> thesis holds, with the snapshot taken
    from bars up to that minute only, and the outcome from the next open."""
    if len(bars) < orb_minutes + min_post_bars + 1:
        return None
    origin = official_origin(day)
    session_end = origin + timedelta(hours=5, minutes=30)
    bars = [b for b in bars if origin <= b.minute < session_end]
    if not bars or bars[0].minute > origin:
        return None
    range_cutoff = origin + timedelta(minutes=orb_minutes)
    for index in range(1, len(bars) - 1):
        upto = bars[:index + 1]
        decision_at = upto[-1].minute + timedelta(minutes=1)
        if decision_at < range_cutoff:
            continue
        opening = [b for b in upto if origin <= b.minute < range_cutoff]
        post = [b for b in upto if b.minute >= range_cutoff]
        if len(post) < min_post_bars or not opening:
            continue
        or_high = max(b.high for b in opening)
        ok, struct = structural_ok(upto, opening, post, or_high)
        if not ok:
            continue
        quality = eq.compute(upto, symbol=symbol, session="PREMARKET",
                             orb_minutes=orb_minutes, now=decision_at,
                             provider="KIS_REST_CHART_BACKFILL",
                             vwap=struct.get("vwap"), ema9=struct.get("ema9"),
                             ema21=struct.get("ema21"),
                             range_origin_timestamp=origin,
                             require_official_origin=True,
                             closed_bar_only=True)
        entry_index = index + 1
        entry_price = bars[entry_index].open
        row = {"day": day, "symbol": symbol, "orb_minutes": orb_minutes,
               "signal_at": decision_at.isoformat(),
               "entry_at": bars[entry_index].minute.isoformat(),
               "entry_price": entry_price, "or_high": or_high,
               "structural": struct, "quality": quality.compact(),
               "quality_full": quality.as_record()}
        row.update(excursions(bars, entry_index, entry_price))
        return row
    return None


def positions(db_path, days):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT position_id, symbol, submitted_at, entry_time, entry_price, exit_price, "
            "exit_reason, closed_at, quantity, range_minutes, range_high, status "
            "FROM s6_positions WHERE entry_session = 'PREMARKET'").fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        record = dict(row)
        day = str(record.get("submitted_at") or "")[:10]
        if day in days:
            record["day"] = day
            out.append(record)
    return out


def replay_fill(bars, fill):
    """The snapshot at the actual submission minute, both ranges, and the
    realized outcome under the exit rules that ran."""
    submitted = _utc(fill["submitted_at"])
    origin = official_origin(fill["day"])
    bars = [b for b in bars if origin <= b.minute < origin + timedelta(
        hours=5, minutes=30)]
    # A submit at HH:MM:ss may only see bars strictly before HH:MM.  The
    # complete OHLCV of the submit minute is future information.
    submit_minute = submitted.replace(second=0, microsecond=0)
    upto = [b for b in bars if b.minute < submit_minute]
    if len(upto) < 6:
        return None
    out = {"day": fill["day"], "symbol": fill["symbol"], "position_id": fill["position_id"],
           "submitted_at": submitted.isoformat(), "entry_price": fill.get("entry_price"),
           "exit_price": fill.get("exit_price"), "exit_reason": fill.get("exit_reason"),
           "filled": fill.get("entry_price") is not None,
           "live_range_minutes": fill.get("range_minutes")}
    if fill.get("entry_price") and fill.get("exit_price"):
        out["realized_return_pct"] = (fill["exit_price"] / fill["entry_price"] - 1.0) * 100.0
        out["realized_pnl"] = (fill["exit_price"] - fill["entry_price"]) * int(fill.get("quantity") or 0)
    for orb in RANGES:
        q = eq.compute(upto, symbol=fill["symbol"], session="PREMARKET", orb_minutes=orb,
                       now=submitted, provider="KIS_REST_CHART_BACKFILL",
                       range_origin_timestamp=origin,
                       require_official_origin=True, closed_bar_only=True)
        out[f"orb{orb}_at_submit"] = q.compact()
    entry_at = _utc(fill.get("entry_time") or fill["submitted_at"])
    entry_minute = entry_at.replace(second=0, microsecond=0)
    entry_index = next((i for i, b in enumerate(bars)
                        if b.minute > entry_minute), len(bars))
    if fill.get("entry_price"):
        if entry_index < len(bars):
            out.update(excursions(bars, entry_index, float(fill["entry_price"])))
    return out


FILTERS = [
    ("max_breakout_age_minutes", "breakout_age_minutes", "le", (10, 15, 20, 30, 45)),
    ("max_minutes_since_session_high", "minutes_since_session_high", "le", (5, 10, 15, 20)),
    ("min_rvol_5m", "rvol_5m", "ge", (0.5, 0.75, 1.0, 1.5)),
    ("min_volume_ratio_5m_15m", "volume_ratio_5m_15m", "ge", (0.6, 0.8, 1.0)),
    ("min_return_5m_pct", "return_5m", "ge", (-1.0, -0.5, 0.0)),
]


def score_filters(signals: List[Dict[str, Any]]):
    """For each candidate threshold: what it would have blocked."""
    def label(row):
        r = row.get(f"return_{WIN_HORIZON}m")
        if r is None:
            return "unmeasured"
        if r > 0.1:
            return "win"
        if r < -0.1:
            return "loss"
        return "flat"

    base = [r for r in signals if r.get(f"return_{WIN_HORIZON}m") is not None]
    out = []
    for key, metric, mode, values in FILTERS:
        for limit in values:
            kept, blocked = [], []
            for row in base:
                value = (row.get("quality") or {}).get(metric)
                if value is None:
                    kept.append(row)  # a filter cannot block what it cannot measure
                    continue
                ok = value <= limit if mode == "le" else value >= limit
                (kept if ok else blocked).append(row)
            def _mean(rows, field):
                vals = [r[field] for r in rows if r.get(field) is not None]
                return sum(vals) / len(vals) if vals else None
            out.append({
                "filter": key, "threshold": limit,
                "opportunities": len(base),
                "blocked_losses": sum(1 for r in blocked if label(r) == "loss"),
                "blocked_wins": sum(1 for r in blocked if label(r) == "win"),
                "blocked_flat": sum(1 for r in blocked if label(r) == "flat"),
                "remaining": len(kept),
                "remaining_wins": sum(1 for r in kept if label(r) == "win"),
                "remaining_losses": sum(1 for r in kept if label(r) == "loss"),
                "avg_return_30m_before": _mean(base, "return_30m"),
                "avg_return_30m_after": _mean(kept, "return_30m"),
                "avg_mfe_30m_after": _mean(kept, "mfe_30m"),
                "avg_mae_30m_after": _mean(kept, "mae_30m"),
                "avg_mae_30m_before": _mean(base, "mae_30m"),
            })
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", required=True)
    parser.add_argument("--bars-dir", required=True)
    parser.add_argument("--db", default=None)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    days = [d.strip() for d in args.days.split(",") if d.strip()]
    signals: Dict[int, List[Dict[str, Any]]] = {5: [], 15: []}
    symbols_seen = 0
    for day in days:
        directory = Path(args.bars_dir) / day
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.jsonl")):
            symbol = path.stem
            bars = load_bars(args.bars_dir, day, symbol)
            if len(bars) < 10:
                continue
            symbols_seen += 1
            for orb in RANGES:
                row = first_signal(bars, orb_minutes=orb, symbol=symbol, day=day)
                if row:
                    signals[orb].append(row)
    fills = []
    if args.db:
        for fill in positions(args.db, set(days)):
            bars = load_bars(args.bars_dir, fill["day"], fill["symbol"])
            if not bars:
                fills.append({"day": fill["day"], "symbol": fill["symbol"],
                              "position_id": fill["position_id"], "bars": "none"})
                continue
            replayed = replay_fill(bars, fill)
            if replayed:
                fills.append(replayed)
    paired = []
    by_key15 = {(r["day"], r["symbol"]): r for r in signals[15]}
    for r5 in signals[5]:
        r15 = by_key15.get((r5["day"], r5["symbol"]))
        paired.append({"day": r5["day"], "symbol": r5["symbol"],
                       "orb5_signal_at": r5["signal_at"], "orb5_return_30m": r5.get("return_30m"),
                       "orb5_mae_30m": r5.get("mae_30m"), "orb5_mfe_30m": r5.get("mfe_30m"),
                       "orb15_signal_at": r15["signal_at"] if r15 else None,
                       "orb15_return_30m": r15.get("return_30m") if r15 else None,
                       "orb15_mae_30m": r15.get("mae_30m") if r15 else None,
                       "orb15_mfe_30m": r15.get("mfe_30m") if r15 else None,
                       "minutes_earlier": ((_utc(r15["signal_at"]) - _utc(r5["signal_at"])).total_seconds() / 60.0
                                           if r15 else None)})
    report = {
        "days": days, "symbols_with_bars": symbols_seen,
        "orb5_signals": signals[5], "orb15_signals": signals[15],
        "paired": paired, "fills": fills,
        "filters_orb5": score_filters(signals[5]),
        "filters_orb15": score_filters(signals[15]),
        "methodology": {
            "official_premarket_origin": "04:00 America/New_York",
            "decision_policy": "closed one-minute bars only",
            "signal_entry": "next bar open",
            "scanner_volume_expansion_min": VOLUME_EXPANSION_MIN,
            "precision_watch_volume_expansion_min": VOLUME_EXPANSION_MIN,
            "actual_submit_minute_ohlcv": "excluded",
            "entry_bar_excursion": "included for theoretical entries; first full post-fill bar for actual fills",
            "unmeasured_is_flat": False,
            "availability": "origin coverage required; truncated history without 04:00 fails closed",
            "costs_and_slippage": "excluded explicitly",
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    Path(args.out).write_text(json.dumps(report, default=str, indent=1), encoding="utf-8")
    summary = {
        "symbols_with_bars": symbols_seen,
        "orb5_signals": len(signals[5]), "orb15_signals": len(signals[15]),
        "fills_replayed": sum(1 for f in fills if f.get("bars") != "none"),
        "fills_without_bars": sum(1 for f in fills if f.get("bars") == "none"),
    }
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
