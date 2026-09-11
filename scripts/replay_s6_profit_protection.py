#!/usr/bin/env python3
"""Read-only replay of EXIT V2 integrated profit protection.

Uses only Phase-1/2 durable snapshots.  It never opens a broker adapter,
writes SQLite, or invokes the live runtime.  Missing snapshot history is
reported as unavailable rather than reconstructed from bars after the fact.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s6_live import exit_policy


MILESTONES = (
    ("armed_at", "armed"),
    ("giveback_warning_at", "giveback_warning"),
    ("vwap_confirmed_failure_at", "vwap_failure_confirmed"),
    ("ema_failure_at", "ema_structure_failure"),
    ("lower_high_lower_low_at", "lower_high_lower_low"),
    ("profit_protection_exit_at", "trigger"),
)


def _state(row):
    return exit_policy.S6PositionState(
        symbol=row["symbol"], entry_price=row["entry_price"],
        range_high=row["range_high"], range_low=row["range_low"],
        peak_price=row["peak_price"], exit_submitted=bool(row["exit_submitted"]),
    )


def _features(row):
    return SimpleNamespace(price=row["current_price"], vwap=row["vwap"],
                           ema9=row["ema9"], ema21=row["ema21"])


def replay_rows(rows):
    """Return first causal milestone timestamps and final replay verdict."""
    history, milestones, assessments = [], {}, []
    for row in rows:
        assessment = exit_policy.profit_protection_assessment(
            _state(row), features=_features(row), current_price=row["current_price"],
            vwap_state=row["shadow_vwap_state"], price_history=history)
        assessment["lower_high_lower_low"] = bool(
            assessment["lower_high"] and assessment["lower_low"])
        assessment["evaluated_at"] = row["evaluated_at"]
        assessments.append(assessment)
        for name, key in MILESTONES:
            if assessment.get(key) and name not in milestones:
                milestones[name] = row["evaluated_at"]
        history.append(row["current_price"])
    return milestones, assessments


def replay(conn, *, symbols=None):
    params, where = [], ""
    if symbols:
        where = "WHERE symbol IN (%s)" % ",".join("?" for _ in symbols)
        params = list(symbols)
    rows = conn.execute(
        "SELECT * FROM s6_exit_snapshots %s " % where +
        "ORDER BY position_id, evaluated_at, snapshot_id", params).fetchall()
    grouped = {}
    for raw in rows:
        row = dict(raw)
        grouped.setdefault(row["position_id"], []).append(row)
    output = []
    for position_id, ticks in grouped.items():
        milestones, assessments = replay_rows(ticks)
        final = assessments[-1] if assessments else {}
        output.append({
            "position_id": position_id, "symbol": ticks[0]["symbol"],
            "snapshot_count": len(ticks), "first_tick": ticks[0]["evaluated_at"],
            "last_tick": ticks[-1]["evaluated_at"],
            "actual_first_exit_reason": next(
                (r["current_exit_reason"] for r in ticks if r["current_exit_reason"]), None),
            **{name: milestones.get(name) for name, _key in MILESTONES},
            "final_assessment": final,
        })
    requested = set(symbols or ())
    seen = {row["symbol"] for row in output}
    unavailable = sorted(requested - seen)
    return {"positions": output, "snapshot_unavailable_symbols": unavailable}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--symbols", default="")
    args = parser.parse_args(argv)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    print(json.dumps(replay(conn, symbols=symbols or None), default=str, sort_keys=True))


if __name__ == "__main__":
    main()
