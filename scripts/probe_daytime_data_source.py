#!/usr/bin/env python3
"""READ-ONLY probe of the three possible daytime (미국주간거래) data sources.

Run on the host inside the KST daytime window, with the shared env
sourced. Places no order, writes nothing, and calls only read
endpoints: the price-detail quote (twice, a pause apart), the KIS
per-symbol minute chart, and the collector's stored trades.

For each symbol it prints:

    QUOTE_FRESH            second read differs from the first
    MINUTE_BAR_COUNT       bars the KIS chart returned
    FIRST_BAR_TIME / LAST_BAR_TIME (ET) and how many fall in the
                           daytime window
    REALTIME_TRADE_COUNT   trades the collector accumulated this session
                           and the newest trade time

Exit code 0 always; the table is the result.
"""

import argparse
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.secret_redaction import install_logging_redaction  # noqa: E402

DEFAULT_SYMBOLS = ("AAPL", "NVDA", "F", "TSLA", "MSFT", "AMD")
SESSION = "OVERNIGHT_DAYTIME"


def _et(stamp):
    from market_hours import EASTERN

    try:
        return stamp.astimezone(EASTERN).strftime("%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return str(stamp)


def probe(symbols, *, gap_seconds=15.0):
    from brokers.kis_broker import KISBroker
    from live_pilot.bootstrap import build_kis_instrument
    from market_data import kis_minute_chart
    from market_data.kis_bar_provider import _collected_store
    from config.kis_market_schedule import describe

    broker = KISBroker()
    now = datetime.now(timezone.utc)
    print("DAYTIME DATA SOURCE PROBE (read-only)", now.isoformat())
    print("schedule:", describe(now))
    store = _collected_store(SESSION)
    print("collector store:", "present" if store is not None else "ABSENT",
          "feed:", store.feed_status(now=now) if store is not None else "n/a")

    firsts = {}
    for symbol in symbols:
        built = build_kis_instrument(symbol)
        instrument = built[0] if isinstance(built, tuple) else built
        try:
            firsts[symbol] = (instrument, broker.get_price_detail(instrument))
        except Exception as exc:  # noqa: BLE001
            firsts[symbol] = (instrument, {"error": f"{type(exc).__name__}: {exc}"[:80]})
        time.sleep(3.2)
    time.sleep(gap_seconds)

    header = ("SYMBOL", "QUOTE_FRESH", "LAST", "TVOL", "MINUTE_BAR_COUNT",
              "DAYTIME_BARS", "FIRST_BAR_ET", "LAST_BAR_ET", "REALTIME_TRADES",
              "REALTIME_LAST_ET")
    print(" | ".join(header))
    for symbol in symbols:
        instrument, first = firsts[symbol]
        try:
            second = broker.get_price_detail(instrument)
        except Exception as exc:  # noqa: BLE001
            second = {"error": f"{type(exc).__name__}"[:40]}
        fresh = any(first.get(k) != second.get(k) and second.get(k) is not None
                    for k in ("last", "today_volume", "high", "low"))
        time.sleep(3.2)
        records = []
        try:
            records = kis_minute_chart.fetch(
                broker, symbol=symbol,
                exchange=getattr(instrument, "exchange", None)) or []
        except Exception as exc:  # noqa: BLE001
            records = []
            print(f"  {symbol}: chart error {type(exc).__name__}")
        time.sleep(3.2)
        stamps = [r.get("at") for r in records if r.get("at") is not None]
        daytime = 0
        for stamp in stamps:
            from config.kis_market_schedule import window_at
            if window_at(stamp) == "DAYTIME":
                daytime += 1
        trades, last_trade = 0, None
        if store is not None:
            acc = store.accumulator(symbol, SESSION)
            if acc is not None:
                trades = getattr(acc, "trade_count", 0) or 0
                last_trade = getattr(acc, "last_trade_at", None)
        row = (symbol, "yes" if fresh else "NO", str(second.get("last")),
               str(second.get("today_volume")), str(len(stamps)), str(daytime),
               _et(min(stamps)) if stamps else "-", _et(max(stamps)) if stamps else "-",
               str(trades), _et(last_trade) if last_trade else "-")
        print(" | ".join(row))
    print("orders submitted: 0 (this probe has no order path)")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="read-only daytime data source probe")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--gap-seconds", type=float, default=15.0)
    args = parser.parse_args(argv)
    install_logging_redaction()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    return probe(symbols, gap_seconds=args.gap_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
