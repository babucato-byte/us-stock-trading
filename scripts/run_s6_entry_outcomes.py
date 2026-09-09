#!/usr/bin/env python3
"""Compute post-entry MFE/MAE for the day's S6 ORB5 fills and ORB15 shadow
signals and write entry_outcomes/<day>.jsonl. Read-only against the
state store; no broker call.

    scripts/run_s6_entry_outcomes.py                    # today's trading day
    scripts/run_s6_entry_outcomes.py --trading-day 2026-09-08 --bars-dir <backfill>

Bars come from each session's persisted collector store and, when
`--bars-dir` is given, from backfilled KIS minute-chart files
(<dir>/<day>/<SYMBOL>.jsonl) for symbols the collector never held.
"""
import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.secret_redaction import install_logging_redaction  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("s6_entry_outcomes")


def _backfilled(bars_dir, day, symbol):
    if not bars_dir:
        return []
    path = Path(bars_dir) / day / f"{symbol.upper()}.jsonl"
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def main(argv=None) -> int:
    install_logging_redaction()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trading-day", default=None)
    parser.add_argument("--session", default="ALL",
                        help="one S6 session, or ALL for every session it scans")
    parser.add_argument("--bars-dir", default=None)
    args = parser.parse_args(argv)
    from s6_live import entry_outcomes, kis_bar_features, range_shadow
    from scanners.base.trading_calendar import us_trading_day
    from state_store import db as state_db

    from config import s6_sessions

    day = args.trading_day or us_trading_day()
    if str(args.session).upper() == "ALL":
        sessions = sorted(s6_sessions.SCAN_SESSIONS)
    else:
        sessions = [str(args.session).upper()]

    # Each session is summarised on its OWN bars, its own fills and its own
    # shadow rows. One day's shadow log holds all four, so it is filtered
    # per session rather than relabelled.
    shadow_rows = range_shadow.read(day)
    rows = []
    per_session = {}
    for session in sessions:
        store = kis_bar_features.load_store(session, day)

        def bars_for(symbol, _store=store, _session=session):
            if _store is not None:
                bars = _store.bars(symbol, _session)
                if bars:
                    return bars
            return _backfilled(args.bars_dir, day, symbol)

        conn = state_db.open_db()
        try:
            fills = entry_outcomes.fill_rows(conn, day, session=session)
        finally:
            conn.close()
        shadow = range_shadow.first_ready(shadow_rows, session=session)
        session_rows = entry_outcomes.build(day, fills=fills, shadow_ready=shadow,
                                            bars_for=bars_for, session=session)
        rows.extend(session_rows)
        per_session[session] = {"fills": len(fills), "shadow_signals": len(shadow),
                                "rows": len(session_rows)}
    written = entry_outcomes.write(rows, trading_day=day)
    print(json.dumps({"trading_day": day, "sessions": per_session,
                      "rows_written": written,
                      "summary": entry_outcomes.summarise(rows)}, default=str, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
