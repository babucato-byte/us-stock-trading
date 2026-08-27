"""HIGH-2: one pacing gate in front of every KIS HTTP request.

Oracle read-only verification found reconciliation failing on every run:

    EGW00201  "초당 거래건수를 초과하였습니다"

It issues balance -> open-orders -> fills back to back with no spacing.
An independent probe that put 3 seconds between the SAME endpoints
succeeded on all of them, so the endpoints are fine and the cadence is
not. A follow-up control ruled out the obvious alternative explanation:
a token request followed IMMEDIATELY by one read succeeds, so this is
about consecutive reads, not about token timing.

Two properties matter and neither is achievable with a `sleep()` sprinkled
into each caller:

  - the budget is shared ACROSS PROCESSES. reconciliation, health, Shadow
    entry, Shadow exit and the diagnostics script are separate systemd
    units that can overlap; a per-process limiter would let four of them
    burst simultaneously and trip the same cap.
  - the clock is injectable, so tests assert the spacing without ever
    really sleeping.

Categories are separated (TOKEN / READ / ORDER / CANCEL) because their
policies genuinely differ -- most importantly, a rate-limited ORDER or
CANCEL is NEVER retried automatically: KIS may have received it, and a
blind re-send could double an order.

Everything in this limiter's own temp namespace is classified before any
request is paced through (see `_scan_namespace`). Two earlier findings
came from files that were merely SKIPPED: a `.temp` suffix fell outside
the `.tmp` filter, and a symlink was refused for deletion but then
ignored, so in both cases the request went out. A file that carries this
limiter's prefix but is not a temporary this limiter could have written
is now a hard block -- it is evidence that something else is writing into
the shared state directory, and that is exactly when a shared pacing
budget cannot be trusted.
"""

import errno
import fcntl
import json
import logging
import os
import re
import stat
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

CATEGORY_TOKEN = "TOKEN"
CATEGORY_READ = "READ"
CATEGORY_ORDER = "ORDER"
CATEGORY_CANCEL = "CANCEL"

CATEGORIES = (CATEGORY_TOKEN, CATEGORY_READ, CATEGORY_ORDER, CATEGORY_CANCEL)

# KIS's own rate-limit code, seen on Oracle.
RATE_LIMIT_MSG_CD = "EGW00201"
REASON_KIS_RATE_LIMIT = "KIS_RATE_LIMIT"
REASON_STATE_INVALID = "KIS_RATE_LIMIT_STATE_INVALID"
REASON_STATE_UNAVAILABLE = "KIS_RATE_LIMIT_STATE_UNAVAILABLE"
REASON_LOCK_FAILED = "KIS_RATE_LIMIT_LOCK_FAILED"
REASON_PERSISTENCE = "KIS_RATE_LIMIT_PERSISTENCE"
REASON_LOCK_RELEASE_FAILED = "KIS_RATE_LIMIT_LOCK_RELEASE_FAILED"
REASON_LOCK_CLOSE_FAILED = "KIS_RATE_LIMIT_LOCK_CLOSE_FAILED"
REASON_LIMITER_INVALIDATED = "KIS_RATE_LIMIT_LIMITER_INVALIDATED"
REASON_STALE_TEMP_CLEANUP_FAILED = "KIS_RATE_LIMIT_STALE_TEMP_CLEANUP_FAILED"
REASON_TEMP_ARTIFACT_INVALID = "KIS_RATE_LIMIT_TEMP_ARTIFACT_INVALID"
REASON_TEMP_ARTIFACT_LIVE = "KIS_RATE_LIMIT_TEMP_ARTIFACT_LIVE"
REASON_ARTIFACT_SCAN_FAILED = "KIS_RATE_LIMIT_ARTIFACT_SCAN_FAILED"

# How an entry of this limiter's own namespace was classified.
ARTIFACT_VALID_STALE_TEMP = "VALID_STALE_TEMP"
ARTIFACT_VALID_LIVE_TEMP = "VALID_LIVE_TEMP"
ARTIFACT_INVALID = "INVALID_TEMP_ARTIFACT"
ARTIFACT_UNRELATED = "UNRELATED"

# Why an entry was rejected. Logged; deliberately coarse.
DETAIL_MALFORMED_FILENAME = "malformed_filename"
DETAIL_SYMLINK = "symlink"
DETAIL_NON_REGULAR_FILE = "non_regular_file"
DETAIL_UNSUPPORTED_FILE_TYPE = "unsupported_file_type"
DETAIL_TYPE_CHANGED = "type_changed"

STATE_VERSION = 1

# ORACLE-HIGH-02: a state timestamp further ahead than this means the
# file is corrupt or the clock moved; either way the pacing budget is
# unknowable and requests must stop, not proceed.
DEFAULT_MAX_CLOCK_SKEW = 5.0

BASE_DIR = Path(__file__).resolve().parent.parent

# Oracle measured 3s between reads as sufficient; that is the documented
# starting point, not an attempt to sit just under an unpublished cap.
DEFAULT_READ_MIN_INTERVAL = 3.0
DEFAULT_TOKEN_MIN_INTERVAL = 60.0   # KIS issues at most one token a minute
DEFAULT_ORDER_MIN_INTERVAL = 1.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_BACKOFF = 3.0
DEFAULT_MAX_BACKOFF = 15.0

# Only reads may be retried automatically. See the module docstring.
RETRYABLE_CATEGORIES = frozenset({CATEGORY_READ})

_STATE_LOCK_TIMEOUT = 10.0

#: Who is asking, for the contention telemetry. Set by each cron wrapper.
#: Not authorisation -- it changes nothing about what a caller may do,
#: and a wrong or missing value costs only a less useful log line.
OWNER_ENV = "KIS_LOCK_OWNER"

#: How long THIS process may wait for the state lock, overriding the
#: default. The entry path sets it low: a new BUY is the lowest-priority
#: use of the broker, and it must never be the reason a position-managing
#: tick waits.
ACQUIRE_TIMEOUT_ENV = "KIS_LOCK_ACQUIRE_TIMEOUT_SECONDS"

#: A wait or hold above this is worth an operator's attention; below it
#: the line is debug-level so normal operation does not flood the log.
_TELEMETRY_NOTABLE_MS = 500.0


#: Entrypoint script -> owner, so a caller is labelled without every
#: wrapper having to remember to export one. Several of those wrappers
#: live outside the repository on the host, and a label that depends on
#: editing them would be missing from exactly the processes whose
#: contention most needs naming.
_OWNER_BY_ENTRYPOINT = {
    "run_s1_live_cycle.py": "S1_EXECUTOR",
    "run_s1_position_watchdog.py": "S1_WATCHDOG",
    "run_live_buy_entry.py": "S6_ENTRY",
    "run_s6_runtime.py": "S6_EXIT",
    "run_reconciliation.py": "RECONCILIATION",
}


def lock_owner():
    """Who is asking. The environment wins; the entrypoint is the
    fallback; UNKNOWN is honest rather than a guess."""
    declared = str(os.environ.get(OWNER_ENV, "") or "").strip()
    if declared:
        return declared
    try:
        import sys

        return _OWNER_BY_ENTRYPOINT.get(os.path.basename(sys.argv[0] or ""),
                                        "UNKNOWN")
    except Exception:  # noqa: BLE001 - a label is never worth an exception
        return "UNKNOWN"


def acquire_timeout():
    """Seconds to wait for the state lock before giving up."""
    return _float_env(ACQUIRE_TIMEOUT_ENV, _STATE_LOCK_TIMEOUT)




# A temporary this module wrote: ".<state name>.<pid>.<32 hex>.tmp".
#
# Always applied with fullmatch(). `$` alone would also accept a trailing
# newline, and a filename may legally contain one, so ".rate.json.1.<hex>
# .tmp\n" would have passed a `$`-anchored match and been treated as a
# temporary of ours.
_TEMP_PATTERN = re.compile(r"\.(?P<state>.+)\.(?P<pid>\d+)\.(?P<uuid>[0-9a-f]{32})\.tmp")


def _temp_name(state_name, pid, token):
    return f".{state_name}.{pid}.{token}.tmp"


def _namespace_prefix(state_name):
    """Everything this limiter may ever have written starts with this."""
    return f".{state_name}."


def _file_type_detail(mode):
    """None for a regular file, otherwise why the entry is not one.

    Judged from an lstat(), so a symlink is reported as a symlink and its
    target is never examined -- following it is what would let something
    outside the state directory be read or unlinked.
    """
    if stat.S_ISLNK(mode):
        return DETAIL_SYMLINK
    if stat.S_ISREG(mode):
        return None
    if stat.S_ISDIR(mode) or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
        return DETAIL_NON_REGULAR_FILE
    return DETAIL_UNSUPPORTED_FILE_TYPE


def _mask_name(name):
    """A filename carries no account data, but it is still an operator's
    file name; logs get the shape and the length, not the whole string."""
    if len(name) <= 20:
        return f"<{len(name)} chars>"
    return f"{name[:12]}...<{len(name)} chars>...{name[-6:]}"


class _Artifact:
    """One entry of this limiter's namespace, as seen during the scan."""

    __slots__ = ("name", "classification", "detail", "info")

    def __init__(self, name, classification, detail, info):
        self.name = name
        self.classification = classification
        self.detail = detail
        self.info = info


def _pid_is_alive(pid):
    """True when a process with this pid exists. Used only to REFUSE
    deletion -- a live pid means "not stale", never "delete it"."""
    if pid <= 0:
        return True                      # unusable: treat as live, i.e. keep
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                      # exists, owned by someone else
    except OSError:
        return True                      # unknown: keep
    return True


class KISRateLimitStateInvalid(Exception):
    """ORACLE-HIGH-02: the shared pacing state could not be trusted.

    An independent probe showed an empty file, a truncated JSON file and a
    future timestamp each granting a READ immediately with no wait -- the
    cross-process budget silently disabled exactly when it was already
    misbehaving. Anything but "absent" or "valid" now stops the request:
    a corrupt budget is not a zero budget.
    """

    def __init__(self, message, *, detail=None):
        super().__init__(message)
        self.reason_code = REASON_STATE_INVALID
        self.detail = detail


class KISRateLimitStateUnavailable(Exception):
    """The SHARED pacing state could not be reached at all -- a permission
    error, an unwritable directory, a failed lock, a read-only filesystem.

    There used to be a local-sleep fallback here. That was the bug: every
    service hitting the same permission error would take the same local
    nap and then all fire together, which is precisely the burst the
    shared budget exists to prevent. Cross-process pacing that cannot be
    shared is not pacing, so the request does not go out.

    The message deliberately carries a classification, not the raw OS
    error or the absolute path.
    """

    def __init__(self, message, *, reason_code=REASON_STATE_UNAVAILABLE, detail=None):
        super().__init__(message)
        self.reason_code = reason_code
        self.detail = detail


class KISRateLimitTempCleanupError(KISRateLimitStateUnavailable):
    """A crashed writer's temporary file could not be cleaned up.

    Left alone these accumulate, and the reason they cannot be removed --
    a permission problem, a read-only mount -- is the same reason the next
    real write will fail. Failing here surfaces it at once instead of
    letting the directory fill silently.
    """


class KISRateLimitTempArtifactError(KISRateLimitTempCleanupError):
    """The state directory holds something in this limiter's own temp
    namespace that this limiter could not have written, or that another
    live process is in the middle of writing.

    Skipping such an entry was the reported defect: a `.temp` suffix and a
    symlink both fell through to "not mine, carry on", and the HTTP
    request went out. The entry is left exactly as it is -- deleting a
    file whose origin is unknown is not this module's call -- but nothing
    is paced through while it is there.
    """


class KISRateLimitArtifactScanError(KISRateLimitTempCleanupError):
    """The namespace could not be listed or stat()ed.

    An unreadable directory is not an empty one: treating a failed scan as
    "no artifacts" would restore precisely the bypass this scan exists to
    close.
    """


class KISRateLimitLockReleaseError(KISRateLimitStateUnavailable):
    """The shared lock could not be released or closed.

    Previously this was swallowed -- the state had been written, so the
    code released what it could, logged nothing useful, and made the HTTP
    request anyway. A lock that is still held blocks every other process
    from pacing itself, so continuing turns one filesystem fault into a
    system-wide stall while THIS process keeps talking to KIS. The
    reservation is only durable when the whole lifecycle closed cleanly.
    """


class KISRateLimitError(Exception):
    """Raised when a rate-limited request could not be completed within
    the retry budget. Callers must treat it as a hard block -- for
    reconciliation that means the snapshot is NOT fresh."""

    def __init__(self, message, *, category, attempts):
        super().__init__(message)
        self.reason_code = REASON_KIS_RATE_LIMIT
        self.category = category
        self.attempts = attempts


def _float_env(name, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _int_env(name, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def min_interval_for(category):
    if category == CATEGORY_READ:
        return _float_env("KIS_READ_MIN_INTERVAL_SECONDS", DEFAULT_READ_MIN_INTERVAL)
    if category == CATEGORY_TOKEN:
        return _float_env("KIS_TOKEN_MIN_INTERVAL_SECONDS", DEFAULT_TOKEN_MIN_INTERVAL)
    if category in (CATEGORY_ORDER, CATEGORY_CANCEL):
        return _float_env("KIS_ORDER_MIN_INTERVAL_SECONDS", DEFAULT_ORDER_MIN_INTERVAL)
    return _float_env("KIS_READ_MIN_INTERVAL_SECONDS", DEFAULT_READ_MIN_INTERVAL)


def max_retries():
    return _int_env("KIS_RATE_LIMIT_MAX_RETRIES", DEFAULT_MAX_RETRIES)


def base_backoff():
    return _float_env("KIS_RATE_LIMIT_BASE_BACKOFF_SECONDS", DEFAULT_BASE_BACKOFF)


def max_backoff():
    return _float_env("KIS_RATE_LIMIT_MAX_BACKOFF_SECONDS", DEFAULT_MAX_BACKOFF)


def stale_temp_min_age():
    """Extra caution before removing a dead writer's temporary. Defaults
    to 0: the owner PID is already gone and we hold the shared lock, so
    the next healthy run should leave the directory clean (the directive's
    "artifact 0 after the next run"). Raise it to also require the file to
    have aged."""
    return _float_env("KIS_RATE_LIMIT_STALE_TEMP_MIN_AGE_SECONDS", 0.0)


def max_clock_skew():
    return _float_env("KIS_RATE_LIMIT_MAX_CLOCK_SKEW_SECONDS", DEFAULT_MAX_CLOCK_SKEW)


def state_file():
    override = os.environ.get("KIS_RATE_LIMIT_STATE_FILE", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return BASE_DIR / "KIS_API_RATE_LIMIT_STATE.json"


def is_rate_limited(body):
    """True when a decoded KIS response body is the per-second cap."""
    if not isinstance(body, dict):
        return False
    for key in ("msg_cd", "message", "error_code"):
        if str(body.get(key, "")).strip() == RATE_LIMIT_MSG_CD:
            return True
    return False


def backoff_delays():
    """3s, 6s, 12s ... capped. Deterministic: tests inject the clock, so
    no jitter is added that a test could not reproduce."""
    base, cap = base_backoff(), max_backoff()
    delays = []
    for attempt in range(max_retries()):
        delays.append(min(base * (2 ** attempt), cap))
    return delays


class KisRateLimiter:
    """Cross-process pacing. The last-request timestamp per category lives
    in a small JSON file guarded by an flock, so every service on the box
    draws from one budget.

    `clock` and `sleeper` are injectable; tests drive a virtual clock and
    never really sleep.
    """

    def __init__(self, *, path=None, clock=None, sleeper=None):
        self._path = Path(path) if path else None
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._wall = time.time
        # Set when a lock could not be released or closed. The handle's
        # state is then unknown, so this limiter refuses further work.
        self._invalidated = False

    # -- state file ------------------------------------------------------

    def _resolve_path(self):
        return self._path if self._path is not None else state_file()

    def _lock_path(self, path):
        return path.with_name(path.name + ".lock")

    def wait(self, *, category):
        """Reserves this process's next slot, durably, and returns only
        when the WHOLE lifecycle succeeded: state persisted atomically,
        lock released, handle closed. The caller may issue its HTTP
        request only after this returns."""
        if self._invalidated:
            raise KISRateLimitStateUnavailable(
                "this KIS rate limiter was invalidated by an earlier lock failure",
                reason_code=REASON_LIMITER_INVALIDATED, detail="invalidated",
            )
        if category not in CATEGORIES:
            category = CATEGORY_READ
        interval = min_interval_for(category)
        path = self._resolve_path()
        if interval <= 0:
            # Pacing is switched off for this category by configuration,
            # so there is no budget to read, write or protect -- but an
            # alien artifact in the namespace still means someone else is
            # writing here, and that must not be silently tolerated just
            # because the interval happens to be zero. Validation only:
            # no lock, no cleanup, no reservation.
            self._reject_invalid_artifacts(path, category)
            return 0.0

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._alert(category, "state directory unavailable")
            raise KISRateLimitStateUnavailable(
                "the shared KIS rate-limit state directory is unavailable",
                detail=type(exc).__name__,
            ) from exc

        try:
            lock_handle = open(self._lock_path(path), "a+")
        except OSError as exc:
            self._alert(category, "lock file unavailable")
            raise KISRateLimitStateUnavailable(
                "the shared KIS rate-limit lock could not be opened",
                reason_code=REASON_LOCK_FAILED, detail=type(exc).__name__,
            ) from exc

        acquired = False
        primary = None
        slept = 0.0
        # Contention telemetry. The 2026-08-27 starvation was diagnosed
        # from a missing tick and one error line, which said that a lock
        # could not be acquired but not who was holding it or for how
        # long. These three stamps make the next one arithmetic.
        request_at = self._clock()
        acquired_at = None
        try:
            if not self._acquire(lock_handle):
                self._alert(category, "lock could not be acquired")
                raise KISRateLimitStateUnavailable(
                    "the shared KIS rate-limit lock could not be acquired",
                    reason_code=REASON_LOCK_FAILED, detail="lock_timeout",
                )
            acquired = True
            acquired_at = self._clock()
            try:
                slept = self._wait_locked(path, category, interval)
            except BaseException as exc:
                primary = exc
                raise
        finally:
            # The release is part of the lifecycle, not cleanup. Its
            # failure is reported, and it must not silently mask the
            # error that got us here.
            release_error = self._release(lock_handle, acquired, category)
            self._report_contention(category, request_at, acquired_at)
            if release_error is not None and primary is None:
                raise release_error
            if release_error is not None:
                # A persistence failure outranks a release failure -- the
                # caller needs the original cause -- but the release
                # problem is still surfaced to an operator.
                logger.error(
                    "the shared KIS rate-limit lock also failed to release (%s)",
                    release_error.reason_code,
                )
        # OUTSIDE the lock. The reservation is already durable, so every
        # other caller can take its own slot while this one waits for the
        # slot it holds.
        if slept > 0:
            self._sleeper(slept)
        return slept

    def _report_contention(self, category, request_at, acquired_at):
        """One line per acquisition: who waited, how long, how long held.

        Deliberately a log line and not a database write. This runs on
        the path whose contention it measures, and a shared table would
        add exactly the kind of cross-process serialisation the numbers
        exist to find.
        """
        try:
            released_at = self._clock()
            if acquired_at is None:
                wait_ms = (released_at - request_at) * 1000.0
                logger.warning(
                    "KIS_LOCK owner=%s category=%s outcome=NOT_ACQUIRED "
                    "lock_wait_ms=%.1f", lock_owner(), category, wait_ms)
                return
            wait_ms = (acquired_at - request_at) * 1000.0
            hold_ms = (released_at - acquired_at) * 1000.0
            line = ("KIS_LOCK owner=%s category=%s outcome=ACQUIRED "
                    "lock_wait_ms=%.1f lock_hold_ms=%.1f")
            args = (lock_owner(), category, wait_ms, hold_ms)
            if max(wait_ms, hold_ms) >= _TELEMETRY_NOTABLE_MS:
                logger.info(line, *args)
            else:
                logger.debug(line, *args)
        except Exception:  # noqa: BLE001 -- telemetry must never be able
            # to fail a request that has already been paced and reserved.
            logger.debug("KIS lock telemetry failed", exc_info=True)

    def _release(self, lock_handle, acquired, category):
        """Unlocks and closes. Returns the error to raise, or None.

        A failure here invalidates this limiter: the handle's state is
        unknown, so no further request may be paced through it.
        """
        error = None
        if acquired:
            try:
                fcntl.flock(lock_handle, fcntl.LOCK_UN)
            except OSError as exc:
                self._invalidated = True
                self._alert(category, "lock could not be released")
                error = KISRateLimitLockReleaseError(
                    "the shared KIS rate-limit lock could not be released",
                    reason_code=REASON_LOCK_RELEASE_FAILED,
                    detail=type(exc).__name__,
                )
        try:
            lock_handle.close()
        except OSError as exc:
            self._invalidated = True
            self._alert(category, "lock handle could not be closed")
            if error is None:
                error = KISRateLimitLockReleaseError(
                    "the shared KIS rate-limit lock handle could not be closed",
                    reason_code=REASON_LOCK_CLOSE_FAILED,
                    detail=type(exc).__name__,
                )
        return error

    def _alert(self, category, classification):
        """Operator-visible. Carries the category and a CLASSIFICATION --
        never the path, the OS error text, or anything from the account."""
        try:
            from operations import alerts

            alerts.send_alert(
                "*KIS shared rate limiter unavailable*\n"
                f"- category: {category}\n"
                f"- problem: {classification}\n"
                "- effect: the request was NOT sent; this cycle fails\n"
                "- action: restore access to the shared rate-limit state"
            )
        except Exception as exc:  # noqa: BLE001 -- alerting must not mask it
            logger.debug("could not alert on limiter unavailability: %s", exc)

    def _acquire(self, handle):
        deadline = self._clock() + acquire_timeout()
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    return False
                if self._clock() >= deadline:
                    return False
                self._sleeper(0.05)

    def _wait_locked(self, path, category, interval):
        # Inside the lock, before anything else and before this lifecycle
        # writes its own temporary: classify the whole namespace, refuse
        # to continue if any entry is not ours, and only then clean up.
        artifacts = self._scan_namespace(path, category)
        self._enforce_artifacts(artifacts, category)
        self._cleanup_stale_temps(path, artifacts, category)
        state = self._load_state(path, category)
        if True:
            has_entry = category in state
            last = state.get(category)
            now = self._wall()
            slept = 0.0
            if has_entry:
                # An entry that is explicitly null is corruption, not the
                # same as never having recorded this category.
                if not isinstance(last, (int, float)) or isinstance(last, bool) \
                        or last != last or last in (float("inf"), float("-inf")) \
                        or last < 0:
                    # NaN, infinity, negative and non-numeric are all
                    # corruption, not "a long time ago".
                    raise KISRateLimitStateInvalid(
                        f"KIS rate-limit timestamp for {category} is not a usable time",
                        detail=repr(last))
                elapsed = now - last
                # A recorded time may legitimately be up to one interval
                # in the FUTURE: that is a slot another caller reserved
                # and has not reached yet. Anything beyond that is a
                # clock or a file an operator has to fix -- waiting it
                # out could block for hours, and ignoring it would
                # bypass pacing entirely.
                if elapsed < -(max_clock_skew() + interval):
                    raise KISRateLimitStateInvalid(
                        f"KIS rate-limit timestamp for {category} is "
                        f"{abs(elapsed):.1f}s in the future",
                        detail="future_timestamp")
                if elapsed < interval:
                    # RESERVE the slot; do not occupy it here.
                    #
                    # This used to sleep out the whole interval while
                    # holding the exclusive lock, which made the lock a
                    # proxy for the pacing budget rather than a guard on
                    # the state file. With a 3s READ interval, any process
                    # issuing back-to-back reads held it ~3s at a time and
                    # re-took it immediately, so a process asking
                    # occasionally could lose the race for the full 10s
                    # acquisition timeout and give up. That is what
                    # happened on 2026-08-27: S6's entry cycle polled
                    # continuously, S1's executor could not get in, missed
                    # two 15-minute ticks while holding a real position,
                    # and its watchdog disabled entries account-wide.
                    #
                    # Reserving instead makes the queue fair and the lock
                    # short. `last` is now the slot the PREVIOUS caller
                    # claimed, so `last + interval` is the next free one;
                    # concurrent callers take successive slots in the
                    # order they get the lock, and each then waits for its
                    # own slot outside it. Pacing is unchanged -- the
                    # spacing between reservations is still `interval`.
                    reserved = last + interval
                    slept = reserved - now
                    now = reserved
            state[category] = now
            # The reservation must be DURABLE before the request goes out,
            # and before the lock is released -- a slot handed out twice
            # would let two callers issue at the same instant.
            self._store_state(path, state, category)
            return slept

    # -- artifacts -------------------------------------------------------

    def _scan_namespace(self, path, category):
        """Classifies every entry that belongs to this limiter's own temp
        namespace, i.e. every name starting with ".<state file name>.".

        Membership is decided by that prefix, NOT by the ".tmp" suffix.
        Filtering on the suffix first was the reported bypass: a file
        named ".<state>.<pid>.<uuid>.temp" is unmistakably about our state
        file, yet it was skipped before anything looked at it.

        The one exception is a name that fully matches the temporary
        pattern for a DIFFERENT state file. That can only happen when one
        state file's name is a dotted prefix of another's, and it belongs
        to that other limiter, not this one.

        Nothing here follows a symlink and nothing here deletes: this is a
        pure classification pass whose result the caller acts on.
        """
        directory = path.parent
        try:
            names = [entry.name for entry in directory.iterdir()]
        except FileNotFoundError:
            return []
        except OSError as exc:
            self._alert(category, "state directory could not be scanned")
            raise KISRateLimitArtifactScanError(
                "the KIS rate-limit state directory could not be scanned",
                reason_code=REASON_ARTIFACT_SCAN_FAILED,
                detail=type(exc).__name__,
            ) from exc

        prefix = _namespace_prefix(path.name)
        artifacts = []
        for name in sorted(names):
            if not name.startswith(prefix):
                continue                                  # ARTIFACT_UNRELATED
            match = _TEMP_PATTERN.fullmatch(name)
            if match is not None and match.group("state") != path.name:
                continue                                  # another state file's
            try:
                info = os.lstat(str(directory / name))
            except FileNotFoundError:
                # It went away between listing and stat. There is nothing
                # left to block on and nothing left to delete.
                continue
            except OSError as exc:
                self._alert(category, "a state directory entry could not be inspected")
                raise KISRateLimitArtifactScanError(
                    "a KIS rate-limit state directory entry could not be inspected",
                    reason_code=REASON_ARTIFACT_SCAN_FAILED,
                    detail=type(exc).__name__,
                ) from exc

            if match is None:
                # Our prefix, not our shape: a truncated name, a wrong
                # suffix, a non-numeric pid, an extra suffix. Never
                # deleted -- its origin is unknown -- but never ignored.
                artifacts.append(_Artifact(
                    name, ARTIFACT_INVALID, DETAIL_MALFORMED_FILENAME, info))
                continue

            type_detail = _file_type_detail(info.st_mode)
            if type_detail is not None:
                # Checked before the pid is even parsed: a symlink or a
                # directory wearing a valid temp name is not a temporary
                # this module wrote, whatever pid the name claims.
                artifacts.append(_Artifact(
                    name, ARTIFACT_INVALID, type_detail, info))
                continue

            pid = int(match.group("pid"))
            if _pid_is_alive(pid):
                # Also covers a reused pid, and a pid we may not signal.
                artifacts.append(_Artifact(
                    name, ARTIFACT_VALID_LIVE_TEMP, "live_writer", info))
            else:
                artifacts.append(_Artifact(
                    name, ARTIFACT_VALID_STALE_TEMP, "dead_writer", info))
        return artifacts

    def _enforce_artifacts(self, artifacts, category):
        """Blocks the whole lifecycle if anything in the namespace is not
        a temporary this limiter may clean up.

        Runs to completion over the SCAN result before a single file is
        removed. Cleaning the well-formed orphans first and only then
        discovering an alien entry would leave the directory in a state
        neither the operator nor the next run can reason about.
        """
        invalid = [a for a in artifacts if a.classification == ARTIFACT_INVALID]
        if invalid:
            # An unexplained writer in the shared state directory is not a
            # transient condition: this limiter stops for good and an
            # operator decides what the file is.
            self._invalidated = True
            self._alert_artifact(category, invalid[0], ARTIFACT_INVALID)
            raise KISRateLimitTempArtifactError(
                "an invalid KIS rate-limit temporary artifact is present",
                reason_code=REASON_TEMP_ARTIFACT_INVALID,
                detail=invalid[0].detail,
            )

        live = [a for a in artifacts if a.classification == ARTIFACT_VALID_LIVE_TEMP]
        if live:
            # A well-formed temporary owned by a LIVE pid. Writers hold
            # the shared lock for their whole lifecycle, so no healthy
            # writer's temporary can be visible from in here; seeing one
            # means either a crashed writer whose pid has been reused or
            # something writing without the lock. Neither is a state to
            # pace a request through.
            #
            # Not invalidating: unlike an alien file this can resolve on
            # its own, so a later run is allowed to try again.
            self._alert_artifact(category, live[0], ARTIFACT_VALID_LIVE_TEMP)
            raise KISRateLimitTempArtifactError(
                "a KIS rate-limit temporary file owned by a live process is present",
                reason_code=REASON_TEMP_ARTIFACT_LIVE, detail=live[0].detail,
            )

    def _cleanup_stale_temps(self, path, artifacts, category):
        """Removes the temporaries left by writers that died before their
        os.replace() landed.

        Runs INSIDE the shared lock, AFTER the whole namespace has been
        classified and cleared, and BEFORE this lifecycle creates its own
        temporary -- so it can never race a live writer and never deletes
        the file this call is about to make.

        Policy B: a temporary whose owner pid no longer exists is removed
        at once, so the very next healthy run leaves the directory clean.
        `KIS_RATE_LIMIT_STALE_TEMP_MIN_AGE_SECONDS` restores Policy A by
        also requiring the file to have aged; a temporary kept back by
        that knob is a known-safe orphan awaiting its window, so it does
        not block the request the way an alien entry does.
        """
        stale = [a for a in artifacts
                 if a.classification == ARTIFACT_VALID_STALE_TEMP]
        if not stale:
            return 0

        min_age = stale_temp_min_age()
        removed = 0
        dir_fd = None
        try:
            try:
                # Every unlink goes through this descriptor, so no part of
                # the path can be swapped underneath us between the scan
                # and the removal.
                dir_fd = os.open(str(path.parent), os.O_RDONLY)
            except OSError as exc:
                self._alert(category, "state directory could not be opened for cleanup")
                raise KISRateLimitTempCleanupError(
                    "the KIS rate-limit state directory could not be opened",
                    reason_code=REASON_STALE_TEMP_CLEANUP_FAILED,
                    detail=type(exc).__name__,
                ) from exc

            for artifact in stale:
                # st_mtime is WALL time; _clock() may be monotonic.
                if self._wall() - artifact.info.st_mtime < min_age:
                    continue

                current = self._recheck_before_unlink(artifact, dir_fd, category)
                if current is None:
                    continue

                try:
                    os.unlink(artifact.name, dir_fd=dir_fd)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    self._alert(category, "stale temporary file could not be removed")
                    raise KISRateLimitTempCleanupError(
                        "a stale KIS rate-limit temporary file could not be removed",
                        reason_code=REASON_STALE_TEMP_CLEANUP_FAILED,
                        detail=type(exc).__name__,
                    ) from exc
                removed += 1

            if removed:
                # The removal must be durable, or a crash could resurrect
                # the very files we just reported as cleaned.
                try:
                    os.fsync(dir_fd)
                except OSError as exc:
                    self._alert(category, "stale cleanup could not be synced")
                    raise KISRateLimitTempCleanupError(
                        "the KIS rate-limit state directory could not be synced",
                        reason_code=REASON_STALE_TEMP_CLEANUP_FAILED,
                        detail=type(exc).__name__,
                    ) from exc
                logger.info("removed %d stale KIS rate-limit temporary file(s)", removed)
        finally:
            if dir_fd is not None:
                try:
                    os.close(dir_fd)
                except OSError:
                    pass
        return removed

    def _recheck_before_unlink(self, artifact, dir_fd, category):
        """Re-stats the entry through the directory descriptor immediately
        before it is unlinked, and requires the very same regular inode
        the scan classified.

        Closes the window in which a valid orphan is swapped for a symlink
        after classification. unlink() never follows a symlink, so a
        target outside the directory was never reachable, but a swapped
        entry means someone else is writing here -- the same condition
        `_enforce_artifacts` refuses to run through.

        Returns the fresh stat, or None when the entry has already gone.
        """
        try:
            current = os.lstat(artifact.name, dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            self._alert(category, "a stale temporary file could not be re-inspected")
            raise KISRateLimitArtifactScanError(
                "a stale KIS rate-limit temporary file could not be re-inspected",
                reason_code=REASON_ARTIFACT_SCAN_FAILED,
                detail=type(exc).__name__,
            ) from exc

        if (not stat.S_ISREG(current.st_mode)
                or current.st_ino != artifact.info.st_ino
                or current.st_dev != artifact.info.st_dev):
            self._invalidated = True
            swapped = _Artifact(artifact.name, ARTIFACT_INVALID,
                                DETAIL_TYPE_CHANGED, current)
            self._alert_artifact(category, swapped, ARTIFACT_INVALID)
            raise KISRateLimitTempArtifactError(
                "a KIS rate-limit temporary file changed identity before cleanup",
                reason_code=REASON_TEMP_ARTIFACT_INVALID,
                detail=DETAIL_TYPE_CHANGED,
            )
        return current

    def _reject_invalid_artifacts(self, path, category):
        """The validation-only pass used when a category's interval is
        zero. No lock is taken and no cleanup runs, so only the entries
        that are permanently wrong -- never the transient ones -- are
        considered."""
        artifacts = self._scan_namespace(path, category)
        self._enforce_artifacts(
            [a for a in artifacts if a.classification == ARTIFACT_INVALID], category)

    def _alert_artifact(self, category, artifact, classification):
        """Operator-visible, and deliberately narrow: the category, how the
        entry was classified, and a masked file name. Never a symlink
        target, never a path, never anything from the account."""
        logger.error(
            "KIS rate-limit artifact refused: category=%s classification=%s "
            "detail=%s name=%s transport_suppressed=true",
            category, classification, artifact.detail, _mask_name(artifact.name),
        )
        try:
            from operations import alerts

            alerts.send_alert(
                "*KIS rate-limit state directory holds an unexpected file*\n"
                f"- category: {category}\n"
                f"- classification: {classification}\n"
                f"- detail: {artifact.detail}\n"
                f"- name: {_mask_name(artifact.name)}\n"
                "- effect: transport_suppressed=true; the request was NOT sent\n"
                "- action: inspect the shared rate-limit state directory and "
                "remove the file only after confirming what wrote it"
            )
        except Exception as exc:  # noqa: BLE001 -- alerting must not mask it
            logger.debug("could not alert on a limiter artifact: %s", exc)

    def _load_state(self, path, category):
        """Reads the shared state. Absent is a first run; anything else
        unreadable or malformed is fail-closed."""
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            self._alert(category, "state file could not be read")
            raise KISRateLimitStateUnavailable(
                "the shared KIS rate-limit state could not be read",
                detail=type(exc).__name__,
            ) from exc
        if not raw.strip():
            # The file EXISTS and is empty. With atomic replace this can
            # only mean corruption -- a partial write is impossible.
            raise KISRateLimitStateInvalid(
                "KIS rate-limit state file exists but is empty", detail="empty")
        try:
            data = json.loads(raw)
        except ValueError:
            raise KISRateLimitStateInvalid(
                "KIS rate-limit state is not valid JSON",
                detail="truncated_or_malformed")
        if not isinstance(data, dict):
            raise KISRateLimitStateInvalid(
                "KIS rate-limit state is not an object", detail=type(data).__name__)
        version = data.pop("version", STATE_VERSION)
        if version != STATE_VERSION:
            raise KISRateLimitStateInvalid(
                "KIS rate-limit state has an unsupported version",
                detail=f"version={version!r}")
        return data

    def _store_state(self, path, state, category):
        """Writes the state ATOMICALLY: a temporary file in the same
        directory, fsynced, then os.replace()d over the target, then the
        directory itself fsynced.

        Rewriting the file in place could leave a truncated or empty
        state behind a crash -- which the reader would then have to treat
        as corruption, stalling every service. With replace(), a reader
        sees either the whole previous state or the whole new one, never
        a partial one, and the previous state survives any failure before
        the replace.
        """
        payload = dict(state)
        payload["version"] = STATE_VERSION
        temp_path = path.with_name(
            _temp_name(path.name, os.getpid(), uuid.uuid4().hex))

        def _fail(stage, exc):
            # Best-effort cleanup; a leftover temp must not mask the
            # original fault, and must not become the reported error.
            try:
                os.unlink(temp_path)
            except OSError as cleanup_exc:
                logger.warning(
                    "could not remove the KIS rate-limit temporary state (%s)",
                    type(cleanup_exc).__name__,
                )
            self._alert(category, f"state could not be persisted ({stage})")
            raise KISRateLimitStateUnavailable(
                "the shared KIS rate-limit state could not be persisted",
                reason_code=REASON_PERSISTENCE, detail=type(exc).__name__,
            ) from exc

        try:
            handle = open(temp_path, "w", encoding="utf-8")
        except OSError as exc:
            self._alert(category, "state could not be persisted (create)")
            raise KISRateLimitStateUnavailable(
                "the shared KIS rate-limit state could not be persisted",
                reason_code=REASON_PERSISTENCE, detail=type(exc).__name__,
            ) from exc
        try:
            with handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            _fail("write", exc)
        except ValueError as exc:            # unserializable state
            _fail("serialize", exc)

        try:
            os.chmod(temp_path, 0o600)
        except OSError as exc:
            _fail("chmod", exc)

        try:
            os.replace(temp_path, path)
        except OSError as exc:
            _fail("replace", exc)

        # The rename itself must be durable, or a crash could resurrect
        # the old state while this process believes the new one is live.
        dir_fd = None
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            os.fsync(dir_fd)
        except OSError as exc:
            self._alert(category, "state could not be persisted (directory fsync)")
            raise KISRateLimitStateUnavailable(
                "the shared KIS rate-limit state directory could not be synced",
                reason_code=REASON_PERSISTENCE, detail=type(exc).__name__,
            ) from exc
        finally:
            if dir_fd is not None:
                try:
                    os.close(dir_fd)
                except OSError:
                    pass


    # -- retry -----------------------------------------------------------

    def call_with_retry(self, fn, *, category, describe=""):
        """Runs `fn()` under pacing, retrying ONLY when the category is
        retryable and the failure is EGW00201.

        `fn` must raise KISRateLimitSignal (below) to indicate the cap; any
        other exception propagates untouched on the first occurrence.
        """
        delays = backoff_delays() if category in RETRYABLE_CATEGORIES else []
        attempts = 0
        while True:
            self.wait(category=category)
            attempts += 1
            try:
                return fn()
            except KISRateLimitSignal:
                if attempts - 1 >= len(delays):
                    raise KISRateLimitError(
                        f"KIS rate limit ({RATE_LIMIT_MSG_CD}) persisted after "
                        f"{attempts} attempt(s){(' for ' + describe) if describe else ''}",
                        category=category, attempts=attempts,
                    ) from None
                delay = delays[attempts - 1]
                logger.warning(
                    "KIS rate limit on a %s request%s -- backing off %.1fs (attempt %d)",
                    category, (" for " + describe) if describe else "", delay, attempts,
                )
                self._sleeper(delay)


class KISRateLimitSignal(Exception):
    """Internal marker: the last response was EGW00201. Never escapes
    call_with_retry() -- it becomes KISRateLimitError or a success."""


_LIMITER = None


def get_limiter():
    global _LIMITER
    if _LIMITER is None:
        _LIMITER = KisRateLimiter()
    return _LIMITER


def reset_limiter():
    """Test hook -- drops the cached limiter so a changed state path or
    injected clock takes effect."""
    global _LIMITER
    _LIMITER = None
