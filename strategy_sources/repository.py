"""Versioned, append-only JSON storage for StrategySource records.

Append-only versioning (mirroring order_intent_ledger.py's own "never
rewrite history" convention): saving a source never overwrites an
existing version file. A change to already-saved source material must be
submitted as a new, higher version number; save_source() rejects any
attempt to reuse or skip a version, and rejects overwriting a file that
already exists on disk even if the version number were somehow valid.
This means the *history* of how a source's claims changed over time
(e.g. "collector originally marked this ASSUMPTION, later found the
source excerpt and reclassified it as SOURCE") is preserved rather than
lost.

Locking follows the same fcntl.flock pattern as positions/store.py and
kill_switch_state.py: a lock file guards the read-current-max-version /
write-new-version sequence so two concurrent savers can't both compute
the same "next version" and silently clobber each other (the second
writer's version-number computation would be stale without the lock).
"""

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from strategy_sources.models import StrategySource

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SOURCES_DIR = BASE_DIR / "docs" / "strategy" / "sources"
LOCK_TIMEOUT_SECONDS = 5.0


class RepositoryError(Exception):
    pass


def _resolve_sources_dir():
    override = os.environ.get("STRATEGY_SOURCES_DIR")
    return Path(override) if override else DEFAULT_SOURCES_DIR


@contextmanager
def _repo_lock(sources_dir, timeout=LOCK_TIMEOUT_SECONDS):
    sources_dir.mkdir(parents=True, exist_ok=True)
    lock_path = sources_dir / ".repository.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        _flock_with_timeout(fd, timeout)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _flock_with_timeout(fd, timeout):
    import time
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise RepositoryError(f"Could not acquire repository lock within {timeout}s")
            time.sleep(0.02)


def _version_filename(source_id, version):
    return f"{source_id}__v{version}.json"


def _existing_versions(sources_dir, source_id):
    if not sources_dir.exists():
        return []
    versions = []
    prefix = f"{source_id}__v"
    for path in sources_dir.glob(f"{prefix}*.json"):
        suffix = path.stem[len(prefix):]
        if suffix.isdigit():
            versions.append(int(suffix))
    return sorted(versions)


def _atomic_write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as tmp_file:
            json.dump(payload, tmp_file, indent=2, ensure_ascii=False)
            tmp_file.write("\n")
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_name, path)
    except Exception:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise


def save_source(source: StrategySource, *, sources_dir=None, lock_timeout=LOCK_TIMEOUT_SECONDS):
    """Save `source` as a new version file. Raises RepositoryError if
    `source.version` is not exactly (max existing version + 1) -- callers
    must read the current max version (e.g. via next_version()) before
    constructing the StrategySource they intend to save, so a stale-read
    race is caught here rather than silently overwriting history."""
    sources_dir = sources_dir or _resolve_sources_dir()
    with _repo_lock(sources_dir, timeout=lock_timeout):
        existing = _existing_versions(sources_dir, source.source_id)
        expected_next = (max(existing) + 1) if existing else 1
        if source.version != expected_next:
            raise RepositoryError(
                f"Cannot save {source.source_id!r} version {source.version}: "
                f"expected next version {expected_next} (existing versions: {existing})"
            )
        dest = sources_dir / _version_filename(source.source_id, source.version)
        if dest.exists():
            raise RepositoryError(f"Refusing to overwrite existing version file: {dest}")
        _atomic_write(dest, source.to_dict())
    return dest


def next_version(source_id, *, sources_dir=None):
    sources_dir = sources_dir or _resolve_sources_dir()
    existing = _existing_versions(sources_dir, source_id)
    return (max(existing) + 1) if existing else 1


def load_source(source_id, version=None, *, sources_dir=None):
    """Return the StrategySource for `source_id` at `version` (or the
    latest version if None), or None if source_id doesn't exist at all."""
    sources_dir = sources_dir or _resolve_sources_dir()
    existing = _existing_versions(sources_dir, source_id)
    if not existing:
        return None
    target_version = version if version is not None else max(existing)
    if target_version not in existing:
        raise RepositoryError(f"{source_id!r} has no version {target_version} (existing: {existing})")
    path = sources_dir / _version_filename(source_id, target_version)
    with open(path) as f:
        payload = json.load(f)
    return StrategySource.from_dict(payload)


def load_all_versions(source_id, *, sources_dir=None):
    sources_dir = sources_dir or _resolve_sources_dir()
    return [load_source(source_id, v, sources_dir=sources_dir) for v in _existing_versions(sources_dir, source_id)]


def list_source_ids(*, sources_dir=None):
    sources_dir = sources_dir or _resolve_sources_dir()
    if not sources_dir.exists():
        return []
    ids = set()
    for path in sources_dir.glob("*__v*.json"):
        source_id, _, _ = path.stem.rpartition("__v")
        if source_id:
            ids.add(source_id)
    return sorted(ids)


def load_all_latest(*, sources_dir=None):
    """Return {source_id: latest StrategySource} for every source in the repository."""
    sources_dir = sources_dir or _resolve_sources_dir()
    return {sid: load_source(sid, sources_dir=sources_dir) for sid in list_source_ids(sources_dir=sources_dir)}
