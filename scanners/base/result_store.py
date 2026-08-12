"""The Scanner Analytics Store (spec section 10).

This is NOT the candidate store
-------------------------------
`market_data/candidate_store.py` publishes the rows that an order can be
placed from. It is fail-closed, freshness-checked, and read by the live
bootstrap. Nothing in this module writes to it, reads from it, or
imports it.

What lives here is an experiment log: every symbol every scanner flagged,
kept so that a month from now the question "which scanner found better
stocks" has an answer. Section 10 requires the two to be separate, and
they are separate at the filesystem level -- different directory,
different environment variable, different file format -- so a mistake in
this code cannot put a row in front of the order path.

Why JSONL and not the trading database
--------------------------------------
`TRADING_STATE.db` is the live system's state, with a migration runner
and a schema the order path depends on. Adding scanner-experiment tables
to it would couple a month-long research exercise to the database that
decides whether an order may be placed, and would mean every schema
change to the research tables ran a migration against the trading DB.

Append-only JSONL, one file per trading day, avoids all of that:

* Appends of a single short line under `O_APPEND` are atomic on POSIX,
  so a scanner appending while a report reads never yields a torn row.
* A day's file is immutable once the day is over, which is what section
  11's "freeze the experiment" actually requires of the storage.
* Section 22 wants the data exported for AI analysis as CSV or JSON --
  it is already JSON, and `export.py`'s CSV path is a DataFrame dump.
* A corrupt line loses one signal, not a database.

Performance is stored separately from signals
---------------------------------------------
Signals never change: `signal_price` is the anchor every return in
section 12 is measured from, and a tracker that could rewrite it could
rewrite its own scorecard. Forward returns and MFE/MAE therefore go in a
parallel `performance/` file keyed by `signal_id`, appended each time
the tracker runs, with the newest record for a given id winning on read.
The signal file is written once and never touched again.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from config.paths import get_project_root
from scanners.base.models import ScannerSignal

logger = logging.getLogger(__name__)

#: Where the analytics store lives. Deliberately a DIFFERENT variable
#: from `KIS_CANDIDATE_DIR` -- pointing one at the other must not be
#: possible by accident.
ANALYTICS_DIR_ENV = "SCANNER_ANALYTICS_DIR"

SIGNALS_SUBDIR = "signals"
PERFORMANCE_SUBDIR = "performance"
RUNS_SUBDIR = "runs"
EXPORTS_SUBDIR = "exports"
REPORTS_SUBDIR = "reports"


class ScannerStoreError(Exception):
    """A store read or write failed."""


def analytics_dir() -> Path:
    override = os.environ.get(ANALYTICS_DIR_ENV)
    if override and str(override).strip():
        return Path(override)
    return Path(get_project_root()) / "logs" / "scanners"


def _subdir(name: str) -> Path:
    path = analytics_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def signals_path(trading_day: str) -> Path:
    return _subdir(SIGNALS_SUBDIR) / f"{trading_day}.jsonl"


def performance_path(trading_day: str) -> Path:
    return _subdir(PERFORMANCE_SUBDIR) / f"{trading_day}.jsonl"


def runs_path(trading_day: str) -> Path:
    return _subdir(RUNS_SUBDIR) / f"{trading_day}.jsonl"


def exports_dir() -> Path:
    return _subdir(EXPORTS_SUBDIR)


def reports_dir() -> Path:
    return _subdir(REPORTS_SUBDIR)


def _append_lines(path: Path, payloads: Iterable[Dict[str, Any]]) -> int:
    """Append JSON objects, one per line, durably.

    Opened in `"a"` mode, which maps to `O_APPEND`: the kernel places
    each write at the current end of file under a single lock, so a
    scanner appending while a weekly report reads the same file cannot
    produce a half-written row. `fsync` before returning, because the
    call sites here are batch jobs run from cron -- if the box goes down
    right after a scan, the signals should already be on disk rather
    than in a page cache that never flushed.
    """
    rows = [payload for payload in payloads]
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = "".join(json.dumps(row, sort_keys=True, default=str) + "\n" for row in rows)
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise ScannerStoreError(f"cannot append to {path}: {exc}") from exc
    return len(rows)


def _read_lines(path: Path) -> Iterator[Dict[str, Any]]:
    """Every readable row. A malformed line is warned about and skipped.

    Skipping rather than raising is the right trade for an append-only
    research log: one truncated line (a full disk mid-write, say) must
    not make the other 4,000 signals of that month unreadable.
    """
    if not path.exists():
        return
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    logger.warning("skipping malformed row at %s:%s", path, number)
    except OSError as exc:
        raise ScannerStoreError(f"cannot read {path}: {exc}") from exc


def write_signals(signals: Iterable[ScannerSignal], *, trading_day: str) -> int:
    """Append signals for one trading day. Returns how many were written."""
    rows = []
    for signal in signals:
        payload = signal.to_dict()
        payload["stored_at"] = datetime.now(timezone.utc).isoformat()
        rows.append(payload)
    written = _append_lines(signals_path(trading_day), rows)
    if written:
        logger.info("stored %s scanner signals for %s", written, trading_day)
    return written


def read_signals(trading_day: str) -> List[ScannerSignal]:
    """Every signal recorded for a trading day.

    Duplicates are collapsed on `signal_id`, so re-running a scan (a
    retried cron job, an operator repeating a run) cannot double-count a
    symbol in the month-end statistics. Section 6's requirement that the
    SAME symbol appear once per scanner is untouched by this: those rows
    have different `scanner_name`s and therefore different ids.
    """
    seen = {}
    for row in _read_lines(signals_path(trading_day)):
        try:
            signal = ScannerSignal.from_dict(row)
        except (TypeError, ValueError):
            logger.warning("skipping unreadable signal row in %s", trading_day)
            continue
        seen[signal.signal_id] = signal
    return list(seen.values())


def read_signal_rows(trading_day: str) -> List[Dict[str, Any]]:
    """Raw dicts, deduplicated on `signal_id` -- for report code that
    wants the stored payload rather than the typed object."""
    seen = {}
    for row in _read_lines(signals_path(trading_day)):
        key = row.get("signal_id")
        if not key:
            continue
        seen[key] = row
    return list(seen.values())


def write_performance(records: Iterable[Dict[str, Any]], *, trading_day: str) -> int:
    """Append forward-return records for signals of `trading_day`.

    Filed under the signal's OWN trading day, not the day the tracker
    ran. A 5-day return computed next week still belongs to the signal
    that produced it, and filing it under the compute date would scatter
    one signal's results across six files.
    """
    rows = []
    for record in records:
        payload = dict(record)
        payload.setdefault("computed_at", datetime.now(timezone.utc).isoformat())
        rows.append(payload)
    return _append_lines(performance_path(trading_day), rows)


#: Fields a later performance run may overwrite with anything,
#: including a null. Everything else follows the merge rule below.
_PERFORMANCE_OVERWRITABLE = frozenset({
    "computed_at", "horizon_status", "status", "includes_signal_day_intraday",
    "sessions_available", "error", "stored_at",
})


def read_performance(trading_day: str) -> Dict[str, Dict[str, Any]]:
    """Performance per `signal_id`, MERGED across runs -- not replaced.

    Why merging rather than last-write-wins
    ---------------------------------------
    The tracker runs every day and re-walks recent days, because a
    signal's 3- and 5-day returns do not exist until several sessions
    later. Under a plain last-write-wins read, that daily re-run was
    actively destructive: minute bars are only served for about a week,
    so the run on day 8 computes `return_30m = None` and that null
    supersedes the correct value computed on day 0. The intraday
    columns would fill in, then silently empty out again a week later --
    and a month-end report would show them missing for every day except
    the most recent week, with nothing to indicate they had ever been
    measured.

    The rule: a later run may FILL a field that was null, and may
    CORRECT a field whose new value is not null, but may never blank a
    value that was already measured. Forward returns only ever become
    more known, so there is no legitimate case for a computed number
    turning back into an unknown.

    Bookkeeping fields (`computed_at`, `horizon_status`, ...) are
    exempt: they describe the latest run, not the measurement, and must
    reflect the newest attempt.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    for row in _read_lines(performance_path(trading_day)):
        key = row.get("signal_id")
        if not key:
            continue
        existing = merged.get(key)
        if existing is None:
            merged[key] = dict(row)
            continue
        for field, value in row.items():
            if field in _PERFORMANCE_OVERWRITABLE or value is not None:
                existing[field] = value
    return merged


def write_run_manifest(manifest: Dict[str, Any], *, trading_day: str) -> int:
    """Record what actually ran: versions, config fingerprints, counts.

    This is the audit trail for sections 11 and 19. Month one's claim
    that the parameters never moved is only checkable if every run wrote
    down the fingerprint it ran with.
    """
    payload = dict(manifest)
    payload.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
    return _append_lines(runs_path(trading_day), [payload])


def read_run_manifests(trading_day: str) -> List[Dict[str, Any]]:
    return list(_read_lines(runs_path(trading_day)))


def available_trading_days() -> List[str]:
    """Every trading day with recorded signals, oldest first."""
    directory = analytics_dir() / SIGNALS_SUBDIR
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.jsonl"))


def read_signal_rows_range(start_day: str, end_day: str) -> List[Dict[str, Any]]:
    """Signals across an inclusive date range, for weekly/monthly reports."""
    rows: List[Dict[str, Any]] = []
    for day in available_trading_days():
        if start_day <= day <= end_day:
            rows.extend(read_signal_rows(day))
    return rows


def read_performance_range(start_day: str, end_day: str) -> Dict[str, Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for day in available_trading_days():
        if start_day <= day <= end_day:
            merged.update(read_performance(day))
    return merged


def joined_rows(start_day: str, end_day: str) -> List[Dict[str, Any]]:
    """Signals with their forward-return record merged in.

    The single shape every report and every export in sections 15, 16,
    17 and 22 consumes: one row per (scanner, symbol, day) carrying both
    what the scanner saw and what happened next. Signals with no
    performance record yet are included with null return fields -- a
    report that silently dropped them would overstate hit rate, because
    the missing ones are disproportionately the most recent.
    """
    performance = read_performance_range(start_day, end_day)
    rows = []
    for row in read_signal_rows_range(start_day, end_day):
        merged = dict(row)
        metrics = merged.pop("metrics", {}) or {}
        for key, value in metrics.items():
            merged.setdefault(f"metric_{key}", value)
        reasons = merged.get("reasons")
        if isinstance(reasons, list):
            merged["reasons"] = "; ".join(str(item) for item in reasons)
        record = performance.get(row.get("signal_id"))
        if record:
            for key, value in record.items():
                if key not in ("signal_id", "trading_day", "symbol",
                               "scanner_name", "scanner_version"):
                    merged[key] = value
        rows.append(merged)
    return rows


def purge_day(trading_day: str) -> None:
    """Delete one day's stored signals and performance.

    Exists for tests and for removing a run made with a broken config
    before it contaminates a month. Never called by any scheduled path.
    """
    for path in (signals_path(trading_day), performance_path(trading_day),
                 runs_path(trading_day)):
        if path.exists():
            path.unlink()


def latest_signal_price(signal_id: str, trading_day: str) -> Optional[float]:
    for signal in read_signals(trading_day):
        if signal.signal_id == signal_id:
            return signal.signal_price
    return None
