"""The one place scanner candidates are published and read.

The problem this replaces
-------------------------
The scanner wrote `order_candidates.csv` next to its own source file,
and the consumer read `order_candidates.csv` next to ITS source file.
Those are the same relative path and never the same absolute one: the
scanner runs from the legacy working copy, the trading code runs from a
detached release. A freshly deployed release therefore had no candidates
at all, and the gap was being closed by hand with `cp`. A pipeline that
needs a human to carry a file between two directories is not automated,
and the first LIMITED LIVE bootstrap needed exactly that copy.

So candidates move to a RELEASE-INDEPENDENT shared path, published once
and read by every release. No per-release copy, no manual step.

Atomicity
---------
`DataFrame.to_csv(path)` truncates and rewrites in place. A consumer
reading during that window sees a short file -- and a short candidate
file does not look like an error, it looks like fewer candidates. That
is the worst possible failure mode here: a silently smaller watchlist.

Publication is therefore temp -> fsync -> os.replace -> directory fsync,
the same discipline the env switch uses. `os.replace` is atomic on POSIX,
so a reader sees either the whole previous file or the whole new one.

Freshness is data, not a guess
------------------------------
A sidecar manifest records `generated_at` and `trading_day` at
publication time. Consumers do not infer freshness from mtime -- an
mtime survives a copy, a restore, or a release rollout that touches the
file without regenerating it. The trading day the scanner believed it
was scanning is written down, and the bootstrap compares it against the
US trading day it believes it is trading.

This module holds no strategy logic. It does not filter, score, rank or
threshold anything: whatever rows the scanner produced are the rows that
get published.
"""

import csv
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

CANDIDATE_FILE = "order_candidates.csv"
MANIFEST_FILE = "order_candidates.manifest.json"

# How old a candidate set may be and still be used for a real order.
# Six hours spans a premarket scan (~09:20 ET) through the regular
# session close, so a morning scan stays usable all day, while a
# yesterday-shaped file is not.
DEFAULT_MAX_AGE_SECONDS = 6 * 60 * 60

CANDIDATE_DIR_ENV = "KIS_CANDIDATE_DIR"


class CandidateStoreError(Exception):
    """Base class. Every failure here is fail-closed for the caller."""


class CandidatesUnavailable(CandidateStoreError):
    reason_code = "NO_CANDIDATE"


class CandidatesStale(CandidateStoreError):
    reason_code = "STALE_CANDIDATE"


def candidate_dir():
    """The shared, release-independent directory.

    `KIS_CANDIDATE_DIR` overrides it -- that is how tests point at a
    tmp_path and how an operator could relocate the store -- but the
    default deliberately sits beside the other shared state
    (RECONCILIATION.json, TRADING_STATE.db) rather than inventing a new
    top-level location.
    """
    override = os.environ.get(CANDIDATE_DIR_ENV)
    if override:
        return Path(override)
    root = os.environ.get("TRADING_PROJECT_ROOT")
    if root:
        # <releases>/<sha>/ -> <releases>/shared/state
        shared = Path(root).parent / "shared" / "state"
        if shared.exists():
            return shared
    return Path(__file__).resolve().parents[1]


def candidate_path():
    return candidate_dir() / CANDIDATE_FILE


def manifest_path():
    return candidate_dir() / MANIFEST_FILE


def _atomic_write_bytes(path, payload):
    """temp -> fsync -> replace -> dir fsync, in the destination
    directory so the replace is a rename within one filesystem."""
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def publish(csv_bytes, *, trading_day, generated_at=None, source=None):
    """Publish a candidate CSV and its manifest atomically.

    The CSV is published FIRST and the manifest second, so a reader that
    catches the intermediate state sees a valid candidate file with a
    stale manifest -- which `load()` treats as stale and refuses. The
    other order would leave a fresh manifest describing an old file,
    which would be believed.
    """
    if not isinstance(csv_bytes, (bytes, bytearray)):
        raise CandidateStoreError("candidate payload must be bytes")
    stamp = generated_at or datetime.now(timezone.utc)
    _atomic_write_bytes(candidate_path(), bytes(csv_bytes))
    manifest = {
        "generated_at": stamp.astimezone(timezone.utc).isoformat(),
        "trading_day": str(trading_day),
        "source": source or "daily_candidate_scanner",
        "candidate_file": CANDIDATE_FILE,
    }
    _atomic_write_bytes(manifest_path(),
                        json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"))
    return manifest


def publish_dataframe(frame, *, trading_day, generated_at=None, source=None):
    """Convenience for the scanner, which holds a DataFrame."""
    return publish(frame.to_csv(index=False).encode("utf-8"),
                   trading_day=trading_day, generated_at=generated_at, source=source)


def read_manifest():
    path = manifest_path()
    if not path.exists():
        raise CandidatesUnavailable(f"no candidate manifest at {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise CandidatesUnavailable(f"candidate manifest unreadable: {exc}") from exc


def read_rows():
    path = candidate_path()
    if not path.exists():
        raise CandidatesUnavailable(f"no candidate file at {path}")
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    except Exception as exc:  # noqa: BLE001
        raise CandidatesUnavailable(f"candidate file unreadable: {exc}") from exc


def symbols():
    """Every candidate symbol, in publication order. Never raises for an
    absent store -- an empty list is a valid 'no candidates today', and
    the ordinary watchlist path should not explode over it."""
    try:
        rows = read_rows()
    except CandidatesUnavailable:
        return []
    seen, out = set(), []
    for row in rows:
        sym = (row.get("symbol") or "").strip()
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def load_verified(*, trading_day, now=None, max_age_seconds=DEFAULT_MAX_AGE_SECONDS):
    """The strict read, for callers that are about to risk real money.

    Raises rather than returning a flag, because the only correct
    response to any of these is "place no order":

      CandidatesUnavailable -- nothing published, or unreadable
      CandidatesStale       -- wrong trading day, or too old

    Returns (rows, manifest).
    """
    manifest = read_manifest()
    rows = read_rows()
    if not rows:
        raise CandidatesUnavailable("candidate file is empty")

    published_day = str(manifest.get("trading_day") or "")
    if published_day != str(trading_day):
        raise CandidatesStale(
            f"candidates were scanned for trading day {published_day!r}, "
            f"but this is {trading_day!r}")

    raw = manifest.get("generated_at")
    try:
        generated = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError) as exc:
        raise CandidatesStale(f"unreadable generated_at {raw!r}") from exc
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)

    current = now or datetime.now(timezone.utc)
    age = (current - generated).total_seconds()
    if age > max_age_seconds:
        raise CandidatesStale(
            f"candidates are {age:.0f}s old, limit is {max_age_seconds}s")
    if age < -300:
        # Published in the future by more than clock jitter: something is
        # wrong with a clock, and guessing which is not this module's job.
        raise CandidatesStale(f"candidates are dated {-age:.0f}s in the future")
    return rows, manifest


def find(symbol, *, rows=None):
    """The scanner's own row for one symbol, or None."""
    for row in (rows if rows is not None else read_rows()):
        if (row.get("symbol") or "").strip() == symbol:
            return row
    return None
