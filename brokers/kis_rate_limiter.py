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
"""

import errno
import fcntl
import json
import logging
import os
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
        if interval <= 0:
            return 0.0

        path = self._resolve_path()
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
        try:
            if not self._acquire(lock_handle):
                self._alert(category, "lock could not be acquired")
                raise KISRateLimitStateUnavailable(
                    "the shared KIS rate-limit lock could not be acquired",
                    reason_code=REASON_LOCK_FAILED, detail="lock_timeout",
                )
            acquired = True
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
        return slept

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
        deadline = self._clock() + _STATE_LOCK_TIMEOUT
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
                if elapsed < -max_clock_skew():
                    # The recorded time is in the FUTURE. Waiting it out
                    # could block for hours and proceeding would bypass
                    # pacing entirely, so stop and let an operator fix the
                    # clock or the file.
                    raise KISRateLimitStateInvalid(
                        f"KIS rate-limit timestamp for {category} is "
                        f"{abs(elapsed):.1f}s in the future",
                        detail="future_timestamp")
                if elapsed < interval:
                    # Covers small negative skew too: within tolerance we
                    # wait the FULL interval rather than assume freshness.
                    slept = interval - max(elapsed, 0.0)
                    self._sleeper(slept)
                    now = self._wall()
            state[category] = now
            # The reservation must be DURABLE before the request goes out.
            self._store_state(path, state, category)
            return slept

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
        temp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")

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
