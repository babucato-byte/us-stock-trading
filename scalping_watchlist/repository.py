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
from .models import (
    CSV_COLUMNS,
    STATUS_ACTIVE,
    STATUS_COOLING,
    STATUS_EXPIRED,
    STATUS_NEW,
    STATUS_REJECTED,
    validate_lifecycle_timestamps,
)

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
    """Ages untouched NEW/ACTIVE/COOLING rows based on last_detected_at.

    CODEX-014: a row with any corrupted/missing/naive lifecycle timestamp,
    or timestamps that are internally inconsistent (last_detected_at or
    expires_at before first_detected_at), is REJECTED outright rather than
    left as-is — the previous "continue" here is exactly what let a
    corrupted timestamp bypass TTL and keep a row ACTIVE forever. See
    DECISION_LOG.md for why per-row rejection was chosen over failing the
    entire store closed.
    """
    now_iso = now.isoformat()
    for row in existing_rows:
        if row["status"] not in (STATUS_NEW, STATUS_ACTIVE, STATUS_COOLING):
            continue

        problems = validate_lifecycle_timestamps(row)
        if problems:
            row["status"] = STATUS_REJECTED
            row["rejection_reasons"] = "INVALID_LIFECYCLE_TIMESTAMP: " + "; ".join(problems)
            row["updated_at"] = now_iso
            continue

        age_minutes = _minutes_between(row.get("last_detected_at"), now)
        if age_minutes >= expire_minutes:
            row["status"] = STATUS_EXPIRED
            row["updated_at"] = now_iso
        elif age_minutes >= ttl_minutes:
            row["status"] = STATUS_COOLING
            row["updated_at"] = now_iso
    return existing_rows


def save_watchlist_cycle(selected_entries, rejected_entries, now, ttl_minutes, expire_minutes,
                          max_watchlist_size, lock_timeout=5.0):
    """One scan cycle's write: merge `selected_entries` (list of dict, one
    per WatchlistEntry) into the persisted NEW/ACTIVE/COOLING/EXPIRED rows
    (refreshing detected_at/scores for re-detected symbols, aging the
    rest), then replace the REJECTED rows wholesale with this cycle's
    rejections. All under one lock so a concurrent reader never observes a
    half-written merge.

    CODEX-013: returns a result dict instead of a bare bool — computation
    success and persistence success are different things, and the caller
    (pipeline.py) must be able to tell "the write itself failed" apart
    from "the write succeeded but the file doesn't read back correctly".
    {
        "success": bool,
        "persisted_count": int,       # rows actually confirmed on disk (0 on any failure)
        "error_code": str,            # "" on success
        "error_message": str,         # "" on success
    }
    Never raises for a persistence problem — always returns a result dict,
    even when the underlying write or the post-write verification fails.
    """
    with file_lock(WATCHLIST_LOCK_FILE, timeout=lock_timeout):
        try:
            existing = load_watchlist()
        except WatchlistUnavailable as exc:
            print(f"Watchlist update refused: {exc}")
            return _failure("FAILED_VALIDATION", f"Existing watchlist unreadable: {exc}")

        existing_selected = [
            dict(row) for _, row in existing.iterrows() if row.get("status") in _SELECTED_STATUSES
        ]

        selected_symbols_this_cycle = {e["symbol"] for e in selected_entries}
        untouched = [row for row in existing_selected if row["symbol"] not in selected_symbols_this_cycle]
        untouched = _apply_expiry(untouched, now, ttl_minutes, expire_minutes)

        # _apply_expiry() may have turned some untouched rows REJECTED
        # (corrupted lifecycle timestamps) — those leave the tracked
        # selected pool entirely and join this cycle's rejection snapshot,
        # consistent with how every other REJECTED row is handled (not
        # merged/aged across cycles).
        newly_rejected = [row for row in untouched if row["status"] == STATUS_REJECTED]
        untouched = [row for row in untouched if row["status"] != STATUS_REJECTED]

        merged_selected = list({row["symbol"]: row for row in (untouched + selected_entries)}.values())
        # Highest scalping_score first; MAX_WATCHLIST_SIZE caps only the
        # non-expired portion so expired history isn't silently deleted here
        # (a separate housekeeping pass can prune EXPIRED rows if desired).
        active_ish = [r for r in merged_selected if r["status"] != STATUS_EXPIRED]
        expired = [r for r in merged_selected if r["status"] == STATUS_EXPIRED]
        active_ish.sort(key=lambda r: _safe_score(r.get("scalping_score")), reverse=True)
        capped = active_ish[:max_watchlist_size] + expired

        all_rows = capped + list(rejected_entries) + newly_rejected
        expected_symbols = [r["symbol"] for r in capped]  # REJECTED rows may legitimately repeat across cycles
        new_df = pd.DataFrame(all_rows, columns=CSV_COLUMNS)

        if not atomic_write_csv(WATCHLIST_FILE, new_df):
            return _failure("FAILED_PERSISTENCE", f"atomic write to {WATCHLIST_FILE} failed")

        return _verify_after_write(len(all_rows), expected_symbols)


def _verify_after_write(expected_row_count, expected_selected_symbols):
    """CODEX-013 post-write check: re-read what was just written and
    confirm it is actually usable, not just "the OS said the write
    succeeded". A write can succeed at the filesystem level and still not
    be what the caller intended (e.g. a concurrent process interleaving
    incorrectly, or a serialization bug)."""
    try:
        reread = read_csv_fail_closed(WATCHLIST_FILE, CSV_COLUMNS)
    except Exception as exc:
        return _failure("FAILED_PERSISTENCE", f"post-write reread failed: {exc}")

    if len(reread) != expected_row_count:
        return _failure(
            "FAILED_PERSISTENCE",
            f"row count mismatch after write: expected {expected_row_count}, found {len(reread)}",
        )

    selected_rows = reread[reread["status"].isin(_SELECTED_STATUSES)]
    duplicate_symbols = selected_rows["symbol"][selected_rows["symbol"].duplicated()].unique().tolist()
    if duplicate_symbols:
        return _failure("FAILED_PERSISTENCE", f"duplicate symbols in selected rows after write: {duplicate_symbols}")

    if set(selected_rows["symbol"]) != set(expected_selected_symbols):
        return _failure("FAILED_PERSISTENCE", "selected symbol set after write does not match what was intended")

    return {"success": True, "persisted_count": len(reread), "error_code": "", "error_message": ""}


def _failure(error_code, error_message):
    print(f"Watchlist persistence failed ({error_code}): {error_message}")
    return {"success": False, "persisted_count": 0, "error_code": error_code, "error_message": error_message}


def _safe_score(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return -1.0
