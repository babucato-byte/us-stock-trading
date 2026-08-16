"""Publishing and loading the S1 live candidate set, fail-closed.

Files, deliberately named so they can never be confused with the
trading candidate store's:

    s1_live_candidates.csv
    s1_live_candidates.manifest.json

Both live in the same release-independent `shared/state` directory the
trading candidate store uses, because that is the one location every
release can see -- but they are different files and this module never
opens the other ones.

Why the manifest carries a payload hash
---------------------------------------
Freshness by mtime is not freshness. An mtime survives a copy, a
restore, and a release rollout that touches a file without regenerating
it. The manifest therefore records `payload_sha256` of the exact CSV
bytes it describes, and `load()` recomputes it. A CSV edited by hand, a
CSV left over from a previous day, and a CSV half-written by a crashed
publisher all fail the same check.

Publish order
-------------
CSV first, manifest second -- the same ordering and the same reason as
`market_data/candidate_store.py`. A reader that catches the intermediate
state sees a NEW csv with an OLD manifest, whose hash and trading day
will not match, so it refuses. The other order would leave a fresh
manifest describing a stale CSV, which is the state a reader believes.

Every refusal is empty, never partial
-------------------------------------
`load()` returns `None` for every failure. There is no "some rows were
readable so use those" path: a malformed row means the file is not what
the publisher wrote, and the honest response to a candidate file of
unknown provenance is to have no candidates.
"""

import csv
import hashlib
import io
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CANDIDATE_FILE = "s1_live_candidates.csv"
MANIFEST_FILE = "s1_live_candidates.manifest.json"

#: Deliberately NOT `KIS_CANDIDATE_DIR`. Pointing one at the other must
#: not be possible by setting a single variable.
S1_CANDIDATE_DIR_ENV = "S1_LIVE_CANDIDATE_DIR"

SCHEMA_VERSION = "s1_live_candidates_v2"

#: The CSV's columns, in order. A file whose header differs is rejected
#: rather than coerced -- a column that moved is a different file.
#:
#: v2 added `signal_timestamp`. The downstream re-entry and freshness
#: guards both need the moment the signal was generated -- one to refuse
#: a signal older than the position's last exit, the other to measure
#: age at order time -- and v1 simply did not carry it, so every
#: candidate was rejected for having no usable timestamp. Bumping
#: SCHEMA_VERSION rather than tolerating both shapes means a v1 file left
#: on disk is refused outright instead of silently losing the field.
COLUMNS = ("rank", "symbol", "scanner_score", "signal_price", "signal_id",
           "signal_timestamp", "scanner_run_id", "trading_day")

#: Manifest keys that must all be present and non-empty.
REQUIRED_MANIFEST_KEYS = (
    "schema_version", "generated_at", "trading_day", "source_scanner",
    "scanner_version", "scanner_run_id", "config_fingerprint",
    "market_data_provider", "candidate_count", "payload_sha256",
)


class S1StoreError(Exception):
    """A publish failed. Loads never raise -- they return None."""


class S1StoreUnresolved(S1StoreError):
    """No shared store could be located. Refusing to guess a path."""


def candidate_dir() -> Path:
    """The shared, release-independent directory, or a refusal.

    Same resolution order and the same refusal as the trading candidate
    store: an explicit override, else `TRADING_PROJECT_ROOT`'s sibling
    `shared/state`, else raise. There is no third option, because a
    process that cannot locate the shared store has no business writing
    candidates into its own release where no other release can see them.
    """
    override = os.environ.get(S1_CANDIDATE_DIR_ENV)
    if override and str(override).strip():
        return Path(override)
    root = os.environ.get("TRADING_PROJECT_ROOT")
    if root and str(root).strip():
        shared = Path(root).parent / "shared" / "state"
        if shared.is_dir():
            return shared
        raise S1StoreUnresolved(
            f"TRADING_PROJECT_ROOT={root!r} but no shared store at {shared}")
    raise S1StoreUnresolved(
        f"neither {S1_CANDIDATE_DIR_ENV} nor TRADING_PROJECT_ROOT is set; "
        "refusing to fall back to a release-local S1 candidate path")


def candidate_path() -> Path:
    return candidate_dir() / CANDIDATE_FILE


def manifest_path() -> Path:
    return candidate_dir() / MANIFEST_FILE


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """temp -> fsync -> replace -> dir fsync, within the destination
    directory so the replace is a rename on one filesystem."""
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
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


def rows_to_csv_bytes(rows: List[Dict[str, Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(COLUMNS), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in COLUMNS})
    return buffer.getvalue().encode("utf-8")


def publish(rows: List[Dict[str, Any]], *, trading_day: str, source_scanner: str,
            scanner_version: str, scanner_run_id: str, config_fingerprint: str,
            market_data_provider: str, generated_at=None) -> Dict[str, Any]:
    """Write the CSV then the manifest, atomically. Returns the manifest."""
    payload = rows_to_csv_bytes(rows)
    stamp = generated_at or datetime.now(timezone.utc)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": stamp.astimezone(timezone.utc).isoformat(),
        "trading_day": str(trading_day),
        "source_scanner": str(source_scanner),
        "scanner_version": str(scanner_version),
        "scanner_run_id": str(scanner_run_id),
        "config_fingerprint": str(config_fingerprint),
        "market_data_provider": str(market_data_provider),
        "candidate_count": len(rows),
        "payload_sha256": _sha256(payload),
    }
    try:
        _atomic_write_bytes(candidate_path(), payload)
        _atomic_write_bytes(
            manifest_path(),
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    except OSError as exc:
        raise S1StoreError(f"could not publish the S1 candidate set: {exc}") from exc
    return manifest


class LoadResult:
    """What `load()` returns on success. On any failure, `load()` returns None."""

    __slots__ = ("rows", "manifest", "symbols")

    def __init__(self, rows, manifest):
        self.rows = rows
        self.manifest = manifest
        self.symbols = frozenset(str(row["symbol"]).upper() for row in rows)


def _refuse(reason: str) -> None:
    """One place every refusal is logged, so an operator can see WHICH
    check rejected the file rather than only that it was rejected."""
    logger.warning("S1 candidate set refused: %s", reason)
    return None


def load(*, expected_trading_day: str, expected_scanner: str,
         expected_run_id: Optional[str] = None,
         expected_provider: Optional[str] = None) -> Optional[LoadResult]:
    """The validated candidate set, or None.

    Never raises. Every failure -- missing file, missing manifest, hash
    mismatch, wrong trading day, wrong run id, wrong provider, wrong
    source scanner, malformed row, unreadable directory -- returns None,
    which every caller turns into an empty allow-list and therefore into
    "reject every symbol".
    """
    try:
        directory = candidate_dir()
    except S1StoreError as exc:
        return _refuse(f"store unresolved: {exc}")

    csv_path, mf_path = directory / CANDIDATE_FILE, directory / MANIFEST_FILE
    if not csv_path.exists():
        return _refuse(f"no candidate file at {csv_path}")
    if not mf_path.exists():
        return _refuse(f"no manifest at {mf_path}")

    try:
        payload = csv_path.read_bytes()
        manifest = json.loads(mf_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _refuse(f"unreadable candidate set: {exc}")

    if not isinstance(manifest, dict):
        return _refuse("manifest is not an object")
    missing = [key for key in REQUIRED_MANIFEST_KEYS
               if manifest.get(key) in (None, "")]
    if missing:
        return _refuse(f"manifest is missing required keys: {missing}")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        return _refuse(f"manifest schema {manifest.get('schema_version')!r} "
                       f"!= {SCHEMA_VERSION!r}")

    # Hash BEFORE parsing. A payload we cannot vouch for should not have
    # its contents interpreted at all.
    actual = _sha256(payload)
    if actual != manifest.get("payload_sha256"):
        return _refuse("payload sha256 mismatch -- the CSV is not the file the "
                       "manifest describes")

    if str(manifest.get("trading_day")) != str(expected_trading_day):
        return _refuse(f"manifest trading_day {manifest.get('trading_day')!r} "
                       f"!= expected {expected_trading_day!r}")
    if str(manifest.get("source_scanner")) != str(expected_scanner):
        return _refuse(f"manifest source_scanner {manifest.get('source_scanner')!r} "
                       f"!= expected {expected_scanner!r}")
    if expected_run_id is not None and str(manifest.get("scanner_run_id")) != str(expected_run_id):
        return _refuse(f"manifest scanner_run_id {manifest.get('scanner_run_id')!r} "
                       f"!= expected {expected_run_id!r}")
    if expected_provider is not None and str(manifest.get("market_data_provider")) != str(expected_provider):
        return _refuse(f"manifest market_data_provider "
                       f"{manifest.get('market_data_provider')!r} != expected "
                       f"{expected_provider!r}")

    rows = _parse_rows(payload, manifest, expected_trading_day, expected_scanner)
    if rows is None:
        return None
    if len(rows) != manifest.get("candidate_count"):
        return _refuse(f"row count {len(rows)} != manifest candidate_count "
                       f"{manifest.get('candidate_count')}")
    return LoadResult(rows, manifest)


def _parse_rows(payload: bytes, manifest: Dict[str, Any], expected_trading_day: str,
                expected_scanner: str) -> Optional[List[Dict[str, Any]]]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        return _refuse(f"candidate CSV is not UTF-8: {exc}")

    reader = csv.DictReader(io.StringIO(text))
    if list(reader.fieldnames or []) != list(COLUMNS):
        return _refuse(f"unexpected CSV header {reader.fieldnames!r}; "
                       f"expected {list(COLUMNS)}")

    rows, seen = [], set()
    for number, raw in enumerate(reader, start=2):
        symbol = str(raw.get("symbol") or "").strip().upper()
        if not symbol:
            return _refuse(f"row {number}: empty symbol")
        if symbol in seen:
            return _refuse(f"row {number}: duplicate symbol {symbol}")
        seen.add(symbol)

        if str(raw.get("trading_day")) != str(expected_trading_day):
            return _refuse(f"row {number}: trading_day {raw.get('trading_day')!r} "
                           f"!= {expected_trading_day!r}")
        if str(raw.get("scanner_run_id")) != str(manifest.get("scanner_run_id")):
            return _refuse(f"row {number}: scanner_run_id disagrees with the manifest")

        score = _finite(raw.get("scanner_score"))
        price = _finite(raw.get("signal_price"))
        if score is None:
            return _refuse(f"row {number}: scanner_score {raw.get('scanner_score')!r} "
                           "is not a finite number")
        if price is None or price <= 0:
            return _refuse(f"row {number}: signal_price {raw.get('signal_price')!r} "
                           "is not a positive finite number")
        if not str(raw.get("signal_id") or "").strip():
            return _refuse(f"row {number}: empty signal_id")

        stamp = str(raw.get("signal_timestamp") or "").strip()
        if not stamp or _as_utc(stamp) is None:
            return _refuse(f"row {number}: signal_timestamp "
                           f"{raw.get('signal_timestamp')!r} is not a usable timestamp")

        rows.append({
            "rank": number - 1,
            "symbol": symbol,
            "scanner_score": score,
            "signal_price": price,
            "signal_id": str(raw["signal_id"]).strip(),
            "signal_timestamp": stamp,
            "scanner_run_id": str(raw["scanner_run_id"]),
            "trading_day": str(raw["trading_day"]),
            "source_scanner": expected_scanner,
        })
    return rows


def _as_utc(value):
    """Shared with `s1_live/freshness.py` -- one definition of a timestamp."""
    from s1_live.freshness import as_utc

    return as_utc(value)


def _finite(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number
