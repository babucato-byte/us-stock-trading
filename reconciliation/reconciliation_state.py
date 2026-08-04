"""CODEX-044: durable record of "when did a real internal-vs-KIS
reconciliation last run, and was it clean" -- the actual fact
`kis_live_trading.py`/`brokers/kis_broker_adapter.py` must query for
`reconciliation_ok`, replacing the previous `reconciliation_ok=True`
constant Codex flagged as a bypass rather than a safety check.

Fail-closed on every axis: no recorded result, a corrupted state file,
a result older than `max_age_seconds`, or a recorded mismatch all
resolve to `is_current_and_clean() == False` -- there is no scenario
where a missing/stale/dirty reconciliation reads as "OK".
"""

import fcntl
import json
import logging
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from reconciliation import freshness

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_STATE_FILE = BASE_DIR / "RECONCILIATION_STATE.json"
# How stale a recorded reconciliation is allowed to be before the buy/sell
# gates must treat it as "no current result" (fail-closed). Reconciliation
# runs every tick of kis_position_manager.sync_kis_fills_and_manage_exits(),
# so this only needs to tolerate one missed tick, not a long outage.
DEFAULT_MAX_AGE_SECONDS = 300


def _resolve_state_path():
    override = os.environ.get("RECONCILIATION_STATE_FILE")
    return Path(override) if override else DEFAULT_STATE_FILE


REASON_TIMEZONE_MISSING = "RECONCILIATION_TIMESTAMP_TIMEZONE_MISSING"
REASON_COMMIT_UNCERTAIN = "RECONCILIATION_SNAPSHOT_COMMIT_UNCERTAIN"
REASON_STALE_TEMP_CLEANUP_FAILED = "RECONCILIATION_STALE_TEMP_CLEANUP_FAILED"
REASON_TEMP_ARTIFACT_INVALID = "RECONCILIATION_TEMP_ARTIFACT_INVALID"
REASON_LOCK_FAILED = "RECONCILIATION_WRITER_LOCK_FAILED"

# ".<snapshot name>.<pid>.<32 hex>.tmp" -- same directory, so os.replace
# is atomic, and carrying the pid so a crashed writer's leftovers can be
# told apart from a live one's.
_TEMP_PATTERN = re.compile(r"\.(?P<state>.+)\.(?P<pid>\d+)\.(?P<uuid>[0-9a-f]{32})\.tmp")

MARKER_ABSENT = "ABSENT"
MARKER_VALID = "VALID_MARKER"
MARKER_INVALID = "INVALID_MARKER_ARTIFACT"
REASON_MARKER_ARTIFACT_INVALID = "RECONCILIATION_MARKER_ARTIFACT_INVALID"
REASON_LOCK_ARTIFACT_INVALID = "RECONCILIATION_LOCK_ARTIFACT_INVALID"


def _marker_name(target):
    return f".{Path(target).name}.commit-uncertain"


def _marker_path(target):
    return Path(target).with_name(_marker_name(target))


def _lock_name(target):
    return f".{Path(target).name}.writer.lock"


def _open_dir(directory):
    return os.open(str(directory), os.O_RDONLY)


def _classify_file_artifact(directory_fd, name):
    """(state, detail) for a file this module owns, judged by lstat().

    Never follows: a symlink here could point anywhere, and following one
    would let an outside file be truncated, locked, or unlinked.
    """
    try:
        info = os.lstat(name, dir_fd=directory_fd)
    except FileNotFoundError:
        return MARKER_ABSENT, None
    except OSError as exc:
        return MARKER_INVALID, type(exc).__name__
    if stat.S_ISLNK(info.st_mode):
        return MARKER_INVALID, "symlink"
    if stat.S_ISDIR(info.st_mode):
        return MARKER_INVALID, "directory"
    if not stat.S_ISREG(info.st_mode):
        return MARKER_INVALID, "non_regular_file"
    if info.st_mode & stat.S_IWOTH:
        return MARKER_INVALID, "world_writable"
    if info.st_uid != os.getuid() and os.getuid() != 0:
        return MARKER_INVALID, "unexpected_owner"
    if info.st_nlink != 1:
        # A hardlink to an inode outside this directory would make the
        # file we act on not the file we validated.
        return MARKER_INVALID, "unexpected_link_count"
    return MARKER_VALID, None


def marker_state(path=None):
    """(state, detail) so a caller can tell "a crash happened" from
    "someone put something odd in the state directory"."""
    target = Path(path) if path else _resolve_state_path()
    try:
        directory_fd = _open_dir(target.parent)
    except OSError as exc:
        return MARKER_INVALID, type(exc).__name__
    try:
        return _classify_file_artifact(directory_fd, _marker_name(target))
    finally:
        os.close(directory_fd)


def commit_is_uncertain(path=None):
    """True while a snapshot's durability is unknown.

    The marker is created and fsynced BEFORE the snapshot is replaced and
    removed only after the replace has been made durable, so its presence
    covers the whole window -- including a SIGKILL between os.replace()
    and the directory fsync, which previously left no trace at all and
    let the new snapshot be approved as fresh.

    An unusable marker (symlink, directory, wrong owner, ...) counts as
    uncertain too: it is never followed and never deleted.
    """
    state, _detail = marker_state(path)
    return state != MARKER_ABSENT


class ReconciliationStateError(Exception):
    """Raised only for a write failure. A read failure (missing/
    corrupted file) is NOT raised -- it fails closed via
    is_current_and_clean() returning False, exactly like a definitively
    dirty reconciliation would."""

    def __init__(self, message, *, reason_code=None, detail=None):
        super().__init__(message)
        self.reason_code = reason_code
        self.detail = detail


class ReconciliationCommitUncertain(ReconciliationStateError):
    """os.replace() landed but the directory fsync failed.

    This is NOT "the write failed": the new snapshot may already be the
    one a reader sees. Saying "failed" would claim the previous snapshot
    is still in place, which is exactly what is unknown. Nothing is
    rolled back either -- removing a snapshot that may be the committed
    one would turn an uncertainty into a loss.
    """


def _pid_is_alive(pid):
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


@dataclass(frozen=True)
class ReconciliationRecord:
    clean: bool
    mismatch_count: int
    checked_at: datetime
    unknown_count: int = 0
    halt: bool = False


def _cleanup_stale_temps(target):
    """Inside the writer's lock, before this write creates its own temp.

    Codex found a temp from a SIGKILLed writer surviving the next
    healthy write, because nothing ever looked for one. Classification
    matches the limiter's: a dead owner's well-formed temp is removed, a
    live owner's is left alone and blocks, and anything in this
    snapshot's namespace that this module could not have written is
    fail-closed rather than deleted.
    """
    directory = Path(target).parent
    prefix = f".{Path(target).name}."
    try:
        names = [entry.name for entry in directory.iterdir()]
    except OSError as exc:
        raise ReconciliationStateError(
            "the snapshot directory could not be scanned",
            reason_code=REASON_STALE_TEMP_CLEANUP_FAILED, detail=type(exc).__name__)

    stale = []
    for name in sorted(names):
        if not name.startswith(prefix):
            continue
        if name.endswith((".commit-uncertain", ".writer.lock")):
            continue                                  # this module's own furniture
        match = _TEMP_PATTERN.fullmatch(name)
        if match is not None and match.group("state") != Path(target).name:
            continue                                  # another snapshot's temp
        try:
            info = os.lstat(str(directory / name))
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ReconciliationStateError(
                "a snapshot directory entry could not be inspected",
                reason_code=REASON_STALE_TEMP_CLEANUP_FAILED, detail=type(exc).__name__)

        if match is None:
            raise ReconciliationStateError(
                "a malformed snapshot temp artifact is present",
                reason_code=REASON_TEMP_ARTIFACT_INVALID, detail="malformed_filename")
        if stat.S_ISLNK(info.st_mode):
            raise ReconciliationStateError(
                "a snapshot temp artifact is a symlink",
                reason_code=REASON_TEMP_ARTIFACT_INVALID, detail="symlink")
        if not stat.S_ISREG(info.st_mode):
            raise ReconciliationStateError(
                "a snapshot temp artifact is not a regular file",
                reason_code=REASON_TEMP_ARTIFACT_INVALID, detail="non_regular_file")
        if _pid_is_alive(int(match.group("pid"))):
            raise ReconciliationStateError(
                "a snapshot temp file owned by a live process is present",
                reason_code=REASON_TEMP_ARTIFACT_INVALID, detail="live_writer_temp")
        stale.append(name)

    if not stale:
        return 0
    dir_fd = None
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
        for name in stale:
            try:
                current = os.lstat(name, dir_fd=dir_fd)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(current.st_mode):
                raise ReconciliationStateError(
                    "a snapshot temp file changed type before cleanup",
                    reason_code=REASON_TEMP_ARTIFACT_INVALID, detail="type_changed")
            os.unlink(name, dir_fd=dir_fd)
        os.fsync(dir_fd)
    except ReconciliationStateError:
        raise
    except OSError as exc:
        raise ReconciliationStateError(
            "a stale snapshot temp file could not be removed",
            reason_code=REASON_STALE_TEMP_CLEANUP_FAILED, detail=type(exc).__name__)
    finally:
        if dir_fd is not None:
            try:
                os.close(dir_fd)
            except OSError:
                pass
    logger.info("removed %d stale reconciliation snapshot temp file(s)", len(stale))
    return len(stale)


def _ensure_marker(directory_fd, target):
    """Creates the intent marker and makes it durable BEFORE the replace.

    Writing it after the replace could not close the crash window: a
    SIGKILL in between left the new snapshot visible with nothing saying
    its commit had never been synced.

    An existing VALID marker is residue from an earlier attempt. It is
    kept -- this call is the new reconciliation that clears it once the
    whole lifecycle succeeds -- and never merely deleted, which would
    approve a snapshot nobody re-derived. An INVALID one is fail-closed.
    """
    name = _marker_name(target)
    state, detail = _classify_file_artifact(directory_fd, name)
    if state == MARKER_INVALID:
        raise ReconciliationStateError(
            "the commit-uncertain marker is not a file this writer may use",
            reason_code=REASON_MARKER_ARTIFACT_INVALID, detail=detail)
    if state == MARKER_VALID:
        logger.warning(
            "a commit-uncertain marker from an earlier attempt is present; "
            "this reconciliation must complete before anything is armed")
        return

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
    except OSError as exc:
        raise ReconciliationStateError(
            "the commit-uncertain marker could not be created",
            reason_code=REASON_MARKER_ARTIFACT_INVALID, detail=type(exc).__name__)
    try:
        os.write(fd, b"reconciliation write in progress\n")
        os.fsync(fd)
    except OSError as exc:
        raise ReconciliationStateError(
            "the commit-uncertain marker could not be synced",
            reason_code=REASON_MARKER_ARTIFACT_INVALID, detail=type(exc).__name__)
    finally:
        os.close(fd)
    # Durable before the replace, or the crash window reopens.
    os.fsync(directory_fd)


def _remove_marker(directory_fd, target):
    """The LAST step, after the replace has been made durable.

    Re-checks identity through the same directory descriptor: the entry
    unlinked must be the regular file that was validated, not something
    swapped in since.
    """
    name = _marker_name(target)
    state, detail = _classify_file_artifact(directory_fd, name)
    if state == MARKER_ABSENT:
        return
    if state == MARKER_INVALID:
        raise ReconciliationStateError(
            "the commit-uncertain marker changed into something unusable",
            reason_code=REASON_MARKER_ARTIFACT_INVALID, detail=detail)
    try:
        os.unlink(name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError as exc:
        # Returning success here would tell an operator the write
        # completed while the marker still blocks every gate.
        raise ReconciliationStateError(
            "the commit-uncertain marker could not be removed durably",
            reason_code=REASON_MARKER_ARTIFACT_INVALID, detail=type(exc).__name__)


def _open_writer_lock(directory_fd, target):
    """Opens the flock file without ever following a symlink.

    A symlinked lock path would have this writer locking some other
    inode -- two writers could then hold "the lock" on different files
    and write concurrently, which is the one thing the lock exists to
    prevent. O_NOFOLLOW also turns "it became a symlink between the
    lstat and the open" into an error rather than a follow.
    """
    name = _lock_name(target)
    state, detail = _classify_file_artifact(directory_fd, name)
    if state == MARKER_INVALID:
        raise ReconciliationStateError(
            "the writer lock is not a file this writer may use",
            reason_code=REASON_LOCK_ARTIFACT_INVALID, detail=detail)

    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    if state == MARKER_ABSENT:
        flags |= os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
    except OSError as exc:
        raise ReconciliationStateError(
            "the writer lock could not be opened safely",
            reason_code=REASON_LOCK_ARTIFACT_INVALID, detail=type(exc).__name__)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ReconciliationStateError(
                "the writer lock is not a regular file",
                reason_code=REASON_LOCK_ARTIFACT_INVALID, detail="non_regular_file")
        if info.st_nlink != 1:
            raise ReconciliationStateError(
                "the writer lock has more than one link",
                reason_code=REASON_LOCK_ARTIFACT_INVALID, detail="unexpected_link_count")
        if info.st_mode & stat.S_IWOTH:
            raise ReconciliationStateError(
                "the writer lock is world-writable",
                reason_code=REASON_LOCK_ARTIFACT_INVALID, detail="world_writable")
    except BaseException:
        os.close(fd)
        raise
    return fd


def record_result(*, clean: bool, mismatch_count: int, unknown_count: int,
                  halt: bool, now=None, path=None):
    """Writes the snapshot ATOMICALLY, in the strict schema, under a lock.

    `unknown_count` and `halt` are REQUIRED, not defaulted: a reader that
    assumes "no unknowns, not halted" when nobody said so is assuming the
    safe answer, which is the opposite of what a safety record is for.
    The two production callers already know both.

    `now` must be timezone-aware. A naive datetime would be serialised
    without an offset and the reader -- correctly -- refuses that, so the
    write would produce a snapshot guaranteed to be rejected. Refusing
    here leaves the previous, usable snapshot in place instead.

    Lifecycle, all inside one flock so a concurrent writer cannot race
    the cleanup or the replace:

        lock -> stale-temp scan/validate/cleanup -> temp write ->
        file fsync -> chmod -> os.replace -> directory fsync -> unlock

    A failure BEFORE the replace leaves the previous snapshot byte for
    byte and removes the temp. A failure AFTER it is reported as
    COMMIT_UNCERTAIN, never as "failed": the new snapshot may already be
    visible, and only its durability is in doubt.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.tzinfo.utcoffset(current) is None:
        raise ReconciliationStateError(
            "the reconciliation timestamp must be timezone-aware",
            reason_code=REASON_TIMEZONE_MISSING, detail="naive_datetime")
    current = current.astimezone(timezone.utc)

    if type(clean) is not bool or type(halt) is not bool:
        raise ReconciliationStateError("clean and halt must be booleans")
    if type(mismatch_count) is not int or type(unknown_count) is not int:
        raise ReconciliationStateError("counts must be integers")
    if mismatch_count < 0 or unknown_count < 0:
        raise ReconciliationStateError("counts must not be negative")

    target = path or _resolve_state_path()
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": freshness.SCHEMA_VERSION,
        "checked_at": current.isoformat(),
        "clean": clean,
        "mismatch_count": mismatch_count,
        "unknown_count": unknown_count,
        "halt": halt,
    }

    # One directory descriptor for the whole lifecycle: every lock,
    # marker and unlink below is resolved relative to it, so no path
    # component can be swapped underneath this writer.
    try:
        directory_fd = _open_dir(target.parent)
    except OSError as exc:
        raise ReconciliationStateError(
            "the snapshot directory could not be opened",
            reason_code=REASON_LOCK_FAILED, detail=type(exc).__name__) from exc

    try:
        lock_fd = _open_writer_lock(directory_fd, target)
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except OSError as exc:
                raise ReconciliationStateError(
                    "the snapshot writer lock could not be acquired",
                    reason_code=REASON_LOCK_FAILED, detail=type(exc).__name__) from exc

            try:
                # Cleanup failure blocks the write: a directory this
                # module cannot tidy is one it should not add to.
                _cleanup_stale_temps(target)

                # BEFORE the replace, and durable. This is what makes a
                # crash between os.replace() and the directory fsync
                # visible afterwards.
                _ensure_marker(directory_fd, target)

                temp_name = f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
                temp_path = target.with_name(temp_name)
                try:
                    with open(temp_path, "w", encoding="utf-8") as fh:
                        json.dump(payload, fh)
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.chmod(temp_path, 0o600)
                except OSError as exc:
                    try:
                        os.unlink(temp_name, dir_fd=directory_fd)
                    except OSError:
                        pass
                    raise ReconciliationStateError(
                        f"failed to persist reconciliation result: {exc}") from exc

                try:
                    os.replace(temp_path, target)
                except OSError as exc:
                    try:
                        os.unlink(temp_name, dir_fd=directory_fd)
                    except OSError:
                        pass
                    raise ReconciliationStateError(
                        f"failed to persist reconciliation result: {exc}") from exc

                # Past this point the new snapshot may already be what a
                # reader sees; only durability is still in question, and
                # the marker already records that.
                try:
                    os.fsync(directory_fd)
                except OSError as exc:
                    _alert_commit_uncertain()
                    raise ReconciliationCommitUncertain(
                        "the reconciliation snapshot was replaced but its directory "
                        "entry could not be synced; the commit is not known to be durable",
                        reason_code=REASON_COMMIT_UNCERTAIN,
                        detail=type(exc).__name__) from exc

                # Only now: the snapshot is durable, so the intent it
                # recorded is complete.
                _remove_marker(directory_fd, target)
            finally:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                os.close(lock_fd)
            except OSError:
                pass
    finally:
        try:
            os.close(directory_fd)
        except OSError:
            pass


def _alert_commit_uncertain():
    try:
        from operations import alerts

        alerts.send_alert(
            "*Reconciliation snapshot commit is uncertain*\n"
            "- the snapshot was replaced but the directory fsync failed\n"
            "- effect: the new snapshot may be visible but is not known to be durable\n"
            "- Shadow timer activation is blocked until a reconciliation completes cleanly"
        )
    except Exception as exc:  # noqa: BLE001 -- alerting must not mask it
        logger.debug("could not alert on an uncertain snapshot commit: %s", exc)


def _load(path=None) -> Optional[ReconciliationRecord]:
    target = path or _resolve_state_path()
    if not target.exists():
        return None
    try:
        with open(target, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    try:
        # The SAME strict schema the freshness gate applies. A snapshot
        # that gate would refuse must not read as usable here either --
        # `bool("false")` is True, and that is exactly the coercion this
        # avoids.
        data = freshness.validate_schema(data)
        checked_at = datetime.fromisoformat(
            data["checked_at"].replace("Z", "+00:00").replace("z", "+00:00"))
        if checked_at.tzinfo is None or checked_at.tzinfo.utcoffset(checked_at) is None:
            # A naive timestamp used to reach is_current_and_clean() and
            # raise TypeError there, out of a fail-closed check and into
            # the caller. Refusing it here keeps the contract: anything
            # not definitively usable reads as "no current result".
            return None
        return ReconciliationRecord(
            clean=data["clean"], mismatch_count=data["mismatch_count"],
            unknown_count=data["unknown_count"], halt=data["halt"],
            checked_at=checked_at,
        )
    except (freshness.SnapshotUnusable, ValueError, KeyError, TypeError, AttributeError):
        return None


def is_current_and_clean(*, max_age_seconds, now=None, path=None) -> bool:
    """Fail-closed: returns False for anything other than "a reconciliation
    ran within max_age_seconds and found zero mismatches"."""
    record = _load(path=path)
    if record is None:
        return False
    if record.clean is not True or record.mismatch_count > 0:
        return False
    if record.unknown_count > 0 or record.halt is not False:
        return False
    current = now or datetime.now(timezone.utc)
    age_seconds = (current - record.checked_at).total_seconds()
    if age_seconds < 0 or age_seconds > max_age_seconds:
        return False
    return True


def get_last_result(*, path=None) -> Optional[ReconciliationRecord]:
    return _load(path=path)
