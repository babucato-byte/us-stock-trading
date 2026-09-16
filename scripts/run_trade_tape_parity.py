#!/usr/bin/env python3
"""Compare trade-tape bars against a RETAINED KIS snapshot. Offline.

No KIS API call happens here, by construction: the authoritative side is
read from a snapshot file the collector already wrote, so the production
rate limiter is never touched and no live read competes with a trading
job. That is the whole point -- Phase 1's harness paged an operator by
losing a race for that limiter, and this one cannot.

What is being tested
--------------------
Whether bars aggregated from the raw SIP trade tape close the odd-lot
gap that made the bars endpoint unusable. The reference is the retained
KIS stream for the same session, and the question for each symbol is
whether the minutes and the volume line up well enough for S6 to reach
the same verdict.

Daily bars come from the SAME provider on both sides, so they cancel out
of the comparison entirely; the only thing that differs between the two
S6 evaluations is the intraday frame.
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger("trade_tape_parity")

#: The fixed comparison set carried over from Phase 1.
#:
#: The first five are where the bars endpoint failed or looked suspect --
#: TMO is the recorded false negative. The last five are liquid controls:
#: if the aggregation were wrong in some general way it would show up on
#: these, where both feeds have plenty of prints and no excuse to differ.
PROBLEM_SET = ("TMO", "HAL", "CL", "AA", "TRMD")
CONTROL_SET = ("AAPL", "MSFT", "NVDA", "INTC", "MU")


def _snapshot_bars(path, symbols, session):
    """Retained KIS bars per symbol. A file read, not an API call."""
    payload = json.loads(Path(path).read_text())
    wanted = set(symbols)
    out = {}
    for entry in payload.get("accumulators") or []:
        if entry.get("session") != session or entry.get("symbol") not in wanted:
            continue
        rows = []
        for bar in entry.get("bars") or []:
            minute = _parse(bar.get("minute"))
            if minute is None:
                continue
            rows.append({"minute": minute, "open": float(bar["open"]),
                         "high": float(bar["high"]), "low": float(bar["low"]),
                         "close": float(bar["close"]),
                         "volume": float(bar.get("volume") or 0.0)})
        rows.sort(key=lambda r: r["minute"])
        out[entry["symbol"]] = rows
    return out, payload.get("coverage_started_at")


def _parse(raw):
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _frame(rows):
    import pandas as pd

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([{
        "Open": r["open"], "High": r["high"], "Low": r["low"],
        "Close": r["close"], "Volume": r["volume"],
    } for r in rows], index=pd.DatetimeIndex(
        [r["minute"] for r in rows], name="Datetime"))


def _qualify(frame, symbol, *, session, trading_day, daily):
    """Run the REAL S6 path on one intraday frame.

    Returns (qualified, detail). The scanner and the feature builder are
    the production ones; only the intraday frame differs between the two
    calls this harness makes.
    """
    from scanners.base.features import build_features
    from scanners.base.market_data_provider import SymbolData
    from scanners.registry import build_scanner

    if frame is None or len(frame) == 0:
        return (False, "no intraday bars")
    bundle = SymbolData(symbol=symbol, daily=daily, intraday=frame,
                        intraday_interval="1m", include_prepost=True,
                        provider_name="parity", as_of=datetime.now(timezone.utc))
    try:
        features = build_features(bundle)
    except Exception as exc:  # noqa: BLE001 - a per-symbol data shortfall
        return (False, f"features: {type(exc).__name__}: {exc}")
    try:
        signal = build_scanner("orb").evaluate(
            bundle, trading_day=trading_day, shared_features=features,
            session=session)
    except Exception as exc:  # noqa: BLE001
        return (False, f"evaluate: {type(exc).__name__}: {exc}")
    return (signal is not None, "qualified" if signal is not None else "rejected")


def _compare(kis_rows, tape_rows, *, window):
    start, end = window
    k = {r["minute"]: r for r in kis_rows if start <= r["minute"] <= end}
    t = {r["minute"]: r for r in tape_rows if start <= r["minute"] <= end}
    shared = sorted(set(k) & set(t))
    close_bad = ohlc_bad = 0
    vol_bad = 0
    vol_ratios = []
    for minute in shared:
        a, b = k[minute], t[minute]
        if a["close"] and abs(a["close"] - b["close"]) / a["close"] > 0.001:
            close_bad += 1
        for field in ("open", "high", "low", "close"):
            if a[field] and abs(a[field] - b[field]) / a[field] > 0.001:
                ohlc_bad += 1
                break
        if a["volume"]:
            ratio = b["volume"] / a["volume"]
            vol_ratios.append(ratio)
            if abs(ratio - 1.0) > 0.05:
                vol_bad += 1
    kv = sum(r["volume"] for r in k.values())
    tv = sum(r["volume"] for r in t.values())
    vol_ratios.sort()
    return {
        "kis_bars": len(k), "tape_bars": len(t), "shared_minutes": len(shared),
        "only_in_kis": len(set(k) - set(t)), "only_in_tape": len(set(t) - set(k)),
        "close_mismatches_over_10bp": close_bad,
        "ohlc_mismatches_over_10bp": ohlc_bad,
        "volume_mismatches_over_5pct": vol_bad,
        "kis_volume": kv, "tape_volume": tv,
        "tape_over_kis_volume": round(tv / kv, 4) if kv else None,
        "per_minute_volume_ratio_median": (
            round(vol_ratios[len(vol_ratios) // 2], 4) if vol_ratios else None),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True,
                        help="retained collector snapshot JSON")
    parser.add_argument("--session", default="PREMARKET")
    parser.add_argument("--session-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--symbols", default=None,
                        help="comma-separated; defaults to the fixed set")
    parser.add_argument("--no-qualify", action="store_true",
                        help="skip S6 evaluation (skips the daily fetch)")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else list(PROBLEM_SET + CONTROL_SET))

    from scanners.base import session_range as srange

    day = datetime.strptime(args.session_date, "%Y-%m-%d").date()
    origin = srange.official_origin(args.session, day)
    if origin is None:
        raise SystemExit(f"no official origin for {args.session} on {day}")
    window_end = origin + timedelta(minutes=330)   # 04:00->09:30 ET premarket
    if args.session != "PREMARKET":
        window_end = origin + timedelta(minutes=240)

    kis_bars, coverage_started = _snapshot_bars(args.snapshot, symbols,
                                                args.session)
    logger.info("retained snapshot: %d of %d symbols, coverage from %s",
                len(kis_bars), len(symbols), coverage_started)

    from market_data.alpaca_trade_tape import TradeTapeClient, to_minute_bars

    client = TradeTapeClient()
    raw = client.fetch_trades(symbols, start=origin, end=window_end)

    aggregate_began = time.time()
    tape = {}
    detail = {}
    for symbol in symbols:
        result = to_minute_bars(raw.get(symbol) or [], symbol=symbol,
                                session=args.session)
        tape[symbol] = result["bars"]
        detail[symbol] = result
    aggregate_seconds = time.time() - aggregate_began

    daily_cache = {}
    if not args.no_qualify:
        from scanners.base.market_data_provider import default_provider

        provider = default_provider()
        for symbol in symbols:
            try:
                daily_cache[symbol] = provider.get_daily_bars(symbol,
                                                              lookback_days=400)
            except Exception:  # noqa: BLE001
                daily_cache[symbol] = None

    rows = []
    kis_pos = set()
    tape_pos = set()
    for symbol in symbols:
        k = kis_bars.get(symbol)
        t = tape.get(symbol) or []
        comparison = (_compare(k, t, window=(origin, window_end))
                      if k is not None else
                      {"kis_bars": None, "tape_bars": len(t),
                       "note": "symbol not in the retained snapshot"})
        entry = {
            "symbol": symbol,
            "in_snapshot": k is not None,
            "raw_trades": len(raw.get(symbol) or []),
            "odd_lot_trades": detail[symbol]["odd_lot_trades"],
            "odd_lot_pct": (round(100.0 * detail[symbol]["odd_lot_trades"]
                                  / max(detail[symbol]["trades_used"], 1), 1)),
            "conditions": dict(detail[symbol]["conditions"].most_common(6)),
            "comparison": comparison,
        }
        if not args.no_qualify:
            daily = daily_cache.get(symbol)
            kq, kd = ((False, "not in snapshot") if k is None else
                      _qualify(_frame(k), symbol, session=args.session,
                               trading_day=args.session_date, daily=daily))
            tq, td = _qualify(_frame([{**b} for b in t]), symbol,
                              session=args.session,
                              trading_day=args.session_date, daily=daily)
            entry["kis_qualified"] = kq
            entry["kis_detail"] = kd
            entry["tape_qualified"] = tq
            entry["tape_detail"] = td
            if kq:
                kis_pos.add(symbol)
            if tq:
                tape_pos.add(symbol)
        rows.append(entry)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session": args.session,
        "session_date": args.session_date,
        "window": {"start": origin.isoformat(), "end": window_end.isoformat()},
        "snapshot": str(args.snapshot),
        "snapshot_coverage_started_at": coverage_started,
        "symbols": len(symbols),
        "fetch": {
            "raw_trades": client.raw_trade_count,
            "pages": client.page_count,
            "api_calls": client.api_calls,
            "fetch_seconds": round(client.fetch_seconds, 3),
            "aggregation_seconds": round(aggregate_seconds, 3),
            "bars_produced": sum(len(v) for v in tape.values()),
        },
        "KIS_POSITIVES": len(kis_pos),
        "TAPE_POSITIVES": len(tape_pos),
        "TRUE_POSITIVES": len(kis_pos & tape_pos),
        "FALSE_POSITIVES": len(tape_pos - kis_pos),
        "FALSE_NEGATIVES": len(kis_pos - tape_pos),
        "RECALL": (round(len(kis_pos & tape_pos) / len(kis_pos), 4)
                   if kis_pos else None),
        "false_negative_symbols": sorted(kis_pos - tape_pos),
        "rows": rows,
    }

    text = json.dumps(report, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text)
        logger.info("wrote %s", args.out)
    else:
        print(text)
    logger.info("raw_trades=%d pages=%d api_calls=%d fetch=%.2fs aggregate=%.2fs",
                client.raw_trade_count, client.page_count, client.api_calls,
                client.fetch_seconds, aggregate_seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
