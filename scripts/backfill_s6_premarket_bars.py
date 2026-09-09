#!/usr/bin/env python3
"""Backfill KIS one-minute PREMARKET bars for past days -- READ-ONLY.

Why
---
The collector persists bars only for the 41 symbols it can subscribe
to, and on 2026-09-04 / 09-08 that set held almost none of the symbols
S6 actually traded. A replay of ORB5 versus ORB15 for real trades needs
the traded symbols' premarket bars, and KIS's minute chart
(inquire-time-itemchartprice) pages into the past when NEXT=1 and KEYB
carries the newest bar wanted (measured on 2026-09-09: KEYB=20260904093000
returned 09-04 09:30 back to 07:31 ET).

What it does
------------
For each (trading day, symbol) it pages backwards from 09:30 ET until the
oldest bar returned is before 04:00 ET or the endpoint returns nothing,
and writes <out>/<day>/<SYMBOL>.jsonl with one bar per line:

    {"minute": "<UTC ISO, bar open>", "et": "<ET ISO>", "open", "high",
     "low", "close", "volume", "source": "KIS_REST_CHART_BACKFILL"}

Throttled (default 6 s between calls) because the shared KIS rate limiter
is the same one the live runtime uses; the total is capped by
--max-calls. It never calls anything but the chart read.
"""
import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.secret_redaction import install_logging_redaction  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_s6_premarket_bars")
EASTERN = ZoneInfo("America/New_York")


def positions_symbols(db_path, days):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT substr(submitted_at,1,10), symbol FROM s6_positions "
            "WHERE entry_session = 'PREMARKET'").fetchall()
    finally:
        conn.close()
    out = {}
    for day, symbol in rows:
        if day in days:
            out.setdefault(day, set()).add(str(symbol).upper())
    return out


def candidate_symbols(candidate_dir, days, top):
    out = {}
    for day in days:
        path = Path(candidate_dir) / f"{day}-PREMARKET.jsonl"
        if not path.exists():
            continue
        best = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            symbol = str(row.get("symbol") or "").upper()
            score = row.get("score") or 0.0
            if symbol and score >= best.get(symbol, -1):
                best[symbol] = score
        ranked = sorted(best.items(), key=lambda kv: -kv[1])[:top]
        out[day] = {s for s, _ in ranked}
    return out


def page(broker, *, symbol, exchange, keyb):
    from brokers.kis_broker import _excd_for
    from market_data import kis_minute_chart as mc

    body = broker._get(mc.CHART_PATH, mc.TR_ID_CHART, {
        "AUTH": "", "EXCD": _excd_for(exchange), "SYMB": symbol,
        "NMIN": "1", "PINC": "1", "NEXT": "1", "NREC": "120", "FILL": "", "KEYB": keyb,
    })
    if str(body.get("rt_cd")) != "0":
        return None
    return body.get("output2") or []


def bar_from_row(row):
    ymd, hms = row.get("xymd"), row.get("xhms")
    if not ymd or not hms:
        return None
    et = datetime(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]),
                  int(hms[:2]), int(hms[2:4]), int(hms[4:6]), tzinfo=EASTERN)
    try:
        return {"minute": et.astimezone(timezone.utc).isoformat(), "et": et.isoformat(),
                "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["last"]),
                "volume": float(row.get("evol") or 0.0),
                "source": "KIS_REST_CHART_BACKFILL"}, et
    except (KeyError, TypeError, ValueError):
        return None


def backfill_symbol(broker, *, symbol, exchange, day, out_dir, sleep, budget):
    target = Path(out_dir) / day / f"{symbol}.jsonl"
    if target.exists():
        return 0, "exists"
    keyb = f"{day.replace('-', '')}093000"
    open_et = datetime(int(day[:4]), int(day[5:7]), int(day[8:10]), 4, 0, tzinfo=EASTERN)
    bars = {}
    calls = 0
    # A page moves back only 120 TRADED minutes. A day beyond KIS's reach
    # would otherwise be paged for hours without ever arriving, so the
    # walk is capped and gives up when pages keep landing after the day.
    max_pages = 6
    while budget["calls"] < budget["max"] and calls < max_pages:
        rows = page(broker, symbol=symbol, exchange=exchange, keyb=keyb)
        budget["calls"] += 1
        calls += 1
        time.sleep(sleep)
        if not rows:
            break
        oldest = None
        oldest_any = None
        for row in rows:
            parsed = bar_from_row(row)
            if parsed is None:
                continue
            bar, et = parsed
            oldest_any = et if oldest_any is None or et < oldest_any else oldest_any
            if et.date().isoformat() != day:
                continue
            if et >= open_et:
                bars[bar["minute"]] = bar
            oldest = et if oldest is None or et < oldest else oldest
        if oldest_any is None:
            break
        if oldest is not None and oldest <= open_et:
            break
        if oldest is None and oldest_any.date().isoformat() > day and calls >= 2:
            break   # still after the target day two pages in: out of reach
        keyb = oldest_any.strftime("%Y%m%d%H%M%S")
    if not bars:
        return calls, "no bars"
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        for key in sorted(bars):
            handle.write(json.dumps(bars[key]) + "\n")
    return calls, f"{len(bars)} bars"


def main(argv=None) -> int:
    install_logging_redaction()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", required=True, help="comma-separated YYYY-MM-DD")
    parser.add_argument("--out", required=True)
    parser.add_argument("--db", default=os.environ.get("STATE_STORE_DB_FILE"))
    parser.add_argument("--candidate-dir", default=None)
    parser.add_argument("--top", type=int, default=0, help="top-N candidates per day by score")
    parser.add_argument("--symbols", default="", help="extra comma-separated symbols for every day")
    parser.add_argument("--sleep", type=float, default=6.0)
    parser.add_argument("--max-calls", type=int, default=400)
    args = parser.parse_args(argv)
    days = [d.strip() for d in args.days.split(",") if d.strip()]
    plan = {}
    if args.db:
        for day, symbols in positions_symbols(args.db, set(days)).items():
            plan.setdefault(day, set()).update(symbols)
    if args.candidate_dir and args.top:
        for day, symbols in candidate_symbols(args.candidate_dir, days, args.top).items():
            plan.setdefault(day, set()).update(symbols)
    extra = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
    for day in days:
        plan.setdefault(day, set()).update(extra)
    total = sum(len(v) for v in plan.values())
    print(f"plan: {total} symbol-days over {len(plan)} days; max calls {args.max_calls}")
    from brokers.kis_broker import KISBroker
    from live_pilot.bootstrap import build_kis_instrument

    broker = KISBroker()
    broker.config.validate_read_allowed()
    budget = {"calls": 0, "max": args.max_calls}
    for day in sorted(plan):
        for symbol in sorted(plan[day]):
            if budget["calls"] >= budget["max"]:
                print("call budget exhausted")
                return 0
            try:
                built = build_kis_instrument(symbol)
                instrument = built[0] if isinstance(built, tuple) else built
                exchange = getattr(instrument, "exchange", None) or "NASDAQ"
            except Exception as exc:  # noqa: BLE001
                print(f"{day} {symbol}: instrument unresolved ({type(exc).__name__})")
                continue
            calls, status = backfill_symbol(broker, symbol=symbol, exchange=exchange, day=day,
                                            out_dir=args.out, sleep=args.sleep, budget=budget)
            print(f"{day} {symbol}: {status} ({calls} calls, total {budget['calls']})", flush=True)
    print("orders submitted: 0 (read-only chart backfill)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
