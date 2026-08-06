"""T9: the pilot's evidence -- one JSONL row per tick, plus a daily
report derived entirely from those rows.

Write discipline is copied from `shadow_mode.persist()` verbatim in
spirit: take an flock on a sibling lock file, append one line, flush,
fsync, release. A crash mid-write can therefore lose at most the line
being written, never a previously-recorded tick, and two writers cannot
interleave halves of a line.

Two things are deliberately NOT shared with shadow_mode.py:

  - the FILE. Shadow's JSONL is the audit record for the Shadow judging
    window (SHADOW_MODE_EXIT_CRITERIA G1-G11 count rows in it). Pouring
    a pilot's per-tick telemetry into the same file would dilute that
    evidence with records that were never part of the window.
  - the DEFAULT. shadow_mode writes nowhere unless a path is configured,
    because on the Oracle host an unconfigured path meant records
    scattered into the release directory. The pilot is a foreground,
    operator-launched tool whose entire purpose is producing a record,
    so silence would be the wrong default; it writes to logs/live_pilot/
    unless LIVE_PILOT_LOG_DIR says otherwise.

The daily report is a PURE function of that day's JSONL. It can be
rebuilt at any time, it never reads the ticks back from memory, and a
line it could not parse is counted in `unreadable_lines` rather than
silently dropped -- a report that quietly summarises fewer ticks than
happened is worse than one that says so.
"""

import fcntl
import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path

from execution.secret_redaction import redact_text, redact_value

logger = logging.getLogger("live_pilot.recorder")

REPO_ROOT = Path(__file__).resolve().parent.parent

TICK_FILE_PREFIX = "live-pilot-"
REPORT_FILE_PREFIX = "live-pilot-report-"


class RecorderError(Exception):
    """A write failed (disk full, permissions). Never raised for a tick
    whose CONTENT is a block or an error -- recording those is the job."""


def log_dir(env=None):
    mapping = os.environ if env is None else env
    raw = (mapping.get("LIVE_PILOT_LOG_DIR") or "").strip()
    return Path(raw) if raw else REPO_ROOT / "logs" / "live_pilot"


def tick_path(*, for_date=None, directory=None):
    day = for_date or datetime.now(timezone.utc).date()
    base = Path(directory) if directory is not None else log_dir()
    return base / f"{TICK_FILE_PREFIX}{day.isoformat()}.jsonl"


def report_path(*, for_date=None, directory=None):
    day = for_date or datetime.now(timezone.utc).date()
    base = Path(directory) if directory is not None else log_dir()
    return base / f"{REPORT_FILE_PREFIX}{day.isoformat()}.json"


def _lock_path_for(target):
    return target.with_name(target.name + ".lock")


def record_tick(row, *, path=None, directory=None):
    """Appends one tick. Returns the path written.

    Every value goes through redact_value() (structural, key-name based)
    and any free-text `error`/`skip_reason` additionally through
    redact_text(), since both are built from exception messages that
    could otherwise carry an account number into a durable file.
    """
    payload = redact_value(dict(row))
    for key in ("error", "skip_reason"):
        if isinstance(payload.get(key), str):
            payload[key] = redact_text(payload[key])

    if path is not None:
        target = Path(path)
    else:
        for_date = None
        stamp = row.get("started_at")
        if isinstance(stamp, str):
            try:
                for_date = datetime.fromisoformat(stamp).date()
            except ValueError:
                for_date = None
        target = tick_path(for_date=for_date, directory=directory)

    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, default=str) + "\n"
    lock_path = _lock_path_for(target)
    try:
        with open(lock_path, "a+") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                with open(target, "a", encoding="utf-8") as fh:
                    fh.write(line)
                    # Without flush+fsync a host crash can lose a tick
                    # that was already reported as written.
                    fh.flush()
                    os.fsync(fh.fileno())
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
    except OSError as exc:
        raise RecorderError(f"failed to record a pilot tick: {exc}") from exc
    return target


def read_ticks(*, path=None, for_date=None, directory=None):
    """Returns `(ticks, unreadable_lines)`. A torn trailing line from a
    crash is skipped but COUNTED -- see the module docstring."""
    target = Path(path) if path is not None else tick_path(
        for_date=for_date, directory=directory)
    if not target.exists():
        return [], []
    ticks = []
    unreadable = []
    with open(target, encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                ticks.append(json.loads(line))
            except ValueError:
                unreadable.append(line_number)
    return ticks, unreadable


def _tally(counter, key):
    if key is None:
        key = "NONE"
    counter[key] = counter.get(key, 0) + 1


def build_report(ticks, *, unreadable_lines=None, for_date=None):
    """Aggregates a day's ticks. Pure: same input, same output, no I/O.

    Counts are deliberately kept as raw tallies rather than rates. A
    "68% approval rate" hides whether the denominator was 3 ticks or
    300, and this report is read to decide whether to arm real money.
    """
    day = for_date or datetime.now(timezone.utc).date()
    entry_results = {}
    entry_reasons = {}
    hypothetical = {}
    exit_decisions = {}
    exit_reasons = {}
    sessions = {}
    postures = {}
    skips = {}
    symbols_evaluated = set()
    submitted = []
    errors = []
    scans = 0
    scanned_candidates = 0
    entry_evaluations = 0
    exit_evaluations = 0

    for tick in ticks:
        _tally(sessions, tick.get("session"))
        _tally(postures, tick.get("posture"))
        if tick.get("skipped"):
            _tally(skips, tick.get("skip_reason"))
        if tick.get("error"):
            errors.append({"tick": tick.get("tick_seq"), "error": tick["error"]})

        scan = tick.get("scan") or {}
        if scan.get("ran"):
            scans += 1
            scanned_candidates += int(scan.get("order_candidates") or 0)

        entry = tick.get("entry") or {}
        for outcome in entry.get("outcomes") or []:
            entry_evaluations += 1
            if outcome.get("symbol"):
                symbols_evaluated.add(outcome["symbol"])
            _tally(entry_results, outcome.get("result"))
            _tally(entry_reasons, outcome.get("reason_code"))
            if outcome.get("hypothetical") is not None:
                _tally(hypothetical, outcome.get("hypothetical"))
        for order in entry.get("submitted") or []:
            submitted.append({"tick": tick.get("tick_seq"), "order": order})

        exits = tick.get("exit") or {}
        for outcome in exits.get("outcomes") or []:
            exit_evaluations += 1
            _tally(exit_decisions, outcome.get("decision"))
            _tally(exit_reasons, outcome.get("reason_code"))

    stamps = [t.get("started_at") for t in ticks if t.get("started_at")]
    return {
        "date": day.isoformat(),
        "generated_at": None,  # stamped by write_report(); keeps this pure
        "tick_count": len(ticks),
        "first_tick_at": min(stamps) if stamps else None,
        "last_tick_at": max(stamps) if stamps else None,
        "unreadable_lines": list(unreadable_lines or []),
        "sessions": sessions,
        "postures": postures,
        "skips": skips,
        "scan": {"passes": scans, "order_candidates_last_seen": scanned_candidates},
        "entry": {
            "evaluations": entry_evaluations,
            "distinct_symbols": len(symbols_evaluated),
            "results": entry_results,
            "reason_codes": entry_reasons,
            "hypothetical": hypothetical,
            "submitted": submitted,
        },
        "exit": {
            "evaluations": exit_evaluations,
            "decisions": exit_decisions,
            "reason_codes": exit_reasons,
        },
        "errors": errors,
    }


def write_report(*, for_date=None, directory=None, now=None):
    """Rebuilds the day's report from the JSONL on disk and writes it
    atomically (temp + os.replace), so a reader never sees a half-written
    report. Returns `(path, report)`."""
    day = for_date or datetime.now(timezone.utc).date()
    if isinstance(day, datetime):
        day = day.date()
    ticks, unreadable = read_ticks(for_date=day, directory=directory)
    report = build_report(ticks, unreadable_lines=unreadable, for_date=day)
    report["generated_at"] = (now or datetime.now(timezone.utc)).isoformat()
    target = report_path(for_date=day, directory=directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    try:
        with open(temp, "w", encoding="utf-8") as fh:
            json.dump(redact_value(report), fh, indent=2, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, target)
    except OSError as exc:
        raise RecorderError(f"failed to write the pilot report: {exc}") from exc
    return target, report


def parse_date(raw):
    """`--date 2026-08-06` for rebuilding an earlier day's report."""
    return date.fromisoformat(raw)
