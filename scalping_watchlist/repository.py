"""Persistence and lifecycle (expiry) for scalping_watchlist.csv (Phase 2
instructions, section 8). Selected entries (NEW/ACTIVE/COOLING/EXPIRED)
persist and age across runs; REJECTED entries are a fresh per-run
snapshot only (kept for this run's audit trail, not tracked over time —
there is no bound on how many symbols could be rejected in a given run,
so carrying rejection history forever would grow unbounded for no
operational benefit; Phase 3 only ever reads ACTIVE rows anyway).
"""

from pathlib import Path

import pandas as pd

from .atomic_io import atomic_write_csv, file_lock, read_csv_fail_closed
from .models import CSV_COLUMNS, STATUS_ACTIVE, STATUS_COOLING, STATUS_EXPIRED, STATUS_NEW, STATUS_REJECTED

BASE_DIR = Path(__file__).resolve().parent.parent
WATCHLIST_FILE = BASE_DIR / "scalping_watchlist.csv"
WATCHLIST_LOCK_FILE = BASE_DIR / "scalping_watchlist.lock"

_SELECTED_STATUSES = {STATUS_NEW, STATUS_ACTIVE, STATUS_COOLING, STATUS_EXPIRED}


class WatchlistUnavailable(Exception):
    """scalping_watchlist.csv exists but could not be safely read (fail-closed)."""


def load_watchlist():
    """Missing file -> empty (legitimate first-run state). Corrupted file
    (wrong columns, unparseable) -> raises, never silently reinitialized."""
    if not WATCHLIST_FILE.exists():
        return pd.DataFrame(columns=CSV_COLUMNS).astype({c: "object" for c in CSV_COLUMNS})
    try:
        return read_csv_fail_closed(WATCHLIST_FILE, CSV_COLUMNS)
    except Exception as exc:
        raise WatchlistUnavailable(str(exc))


def _minutes_between(earlier_iso, later_dt):
    from datetime import datetime

    try:
        earlier = datetime.fromisoformat(earlier_iso)
    except (TypeError, ValueError):
        return None
    if earlier.tzinfo is None:
        return None
    return (later_dt - earlier).total_seconds() / 60.0


def _apply_expiry(existing_rows, now, ttl_minutes, expire_minutes):
    for row in existing_rows:
        if row["status"] not in (STATUS_NEW, STATUS_ACTIVE, STATUS_COOLING):
            continue
        age_minutes = _minutes_between(row.get("detected_at"), now)
        if age_minutes is None:
            continue  # can't judge age from an unparseable timestamp; leave status as-is
        if age_minutes >= expire_minutes:
            row["status"] = STATUS_EXPIRED
        elif age_minutes >= ttl_minutes:
            row["status"] = STATUS_COOLING
    return existing_rows


def save_watchlist_cycle(selected_entries, rejected_entries, now, ttl_minutes, expire_minutes,
                          max_watchlist_size, lock_timeout=5.0):
    """One scan cycle's write: merge `selected_entries` (list of dict, one
    per WatchlistEntry) into the persisted NEW/ACTIVE/COOLING/EXPIRED rows
    (refreshing detected_at/scores for re-detected symbols, aging the
    rest), then replace the REJECTED rows wholesale with this cycle's
    rejections. All under one lock so a concurrent reader never observes a
    half-written merge.
    """
    with file_lock(WATCHLIST_LOCK_FILE, timeout=lock_timeout):
        try:
            existing = load_watchlist()
        except WatchlistUnavailable as exc:
            print(f"Watchlist update refused: {exc}")
            return False

        existing_selected = [
            dict(row) for _, row in existing.iterrows() if row.get("status") in _SELECTED_STATUSES
        ]
        by_symbol = {row["symbol"]: row for row in existing_selected}

        selected_symbols_this_cycle = {e["symbol"] for e in selected_entries}
        untouched = [row for row in existing_selected if row["symbol"] not in selected_symbols_this_cycle]
        untouched = _apply_expiry(untouched, now, ttl_minutes, expire_minutes)

        for entry in selected_entries:
            by_symbol[entry["symbol"]] = entry

        merged_selected = list({row["symbol"]: row for row in (untouched + selected_entries)}.values())
        # Highest scalping_score first; MAX_WATCHLIST_SIZE caps only the
        # non-expired portion so expired history isn't silently deleted here
        # (a separate housekeeping pass can prune EXPIRED rows if desired).
        active_ish = [r for r in merged_selected if r["status"] != STATUS_EXPIRED]
        expired = [r for r in merged_selected if r["status"] == STATUS_EXPIRED]
        active_ish.sort(key=lambda r: _safe_score(r.get("scalping_score")), reverse=True)
        capped = active_ish[:max_watchlist_size] + expired

        all_rows = capped + list(rejected_entries)
        new_df = pd.DataFrame(all_rows, columns=CSV_COLUMNS)
        return atomic_write_csv(WATCHLIST_FILE, new_df)


def _safe_score(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return -1.0
