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

    # -- state file ------------------------------------------------------

    def _resolve_path(self):
        return self._path if self._path is not None else state_file()

    def _lock_path(self, path):
        return path.with_name(path.name + ".lock")

    def _read_state(self, handle, *, is_new_file):
        """Classifies the shared state as ABSENT or VALID or CORRUPT.

        Only a file that did not exist counts as a legitimate first run.
        An existing file that is empty, truncated, or the wrong JSON type
        is corrupt, and corrupt is NOT the same as "no budget used".
        """
        try:
            handle.seek(0)
            raw = handle.read()
        except OSError as exc:
            raise KISRateLimitStateInvalid(
                "KIS rate-limit state could not be read", detail=type(exc).__name__)
        if not raw.strip():
            if is_new_file:
                return {}
            raise KISRateLimitStateInvalid(
                "KIS rate-limit state file exists but is empty", detail="empty")
        try:
            data = json.loads(raw)
        except ValueError:
            raise KISRateLimitStateInvalid(
                "KIS rate-limit state is not valid JSON", detail="truncated_or_malformed")
        if not isinstance(data, dict):
            raise KISRateLimitStateInvalid(
                "KIS rate-limit state is not an object",
                detail=type(data).__name__)
        return data

    def wait(self, *, category):
        """Blocks until this process may issue a request of `category`."""
        if category not in CATEGORIES:
            category = CATEGORY_READ
        interval = min_interval_for(category)
        if interval <= 0:
            return 0.0

        path = self._resolve_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("cannot prepare the rate-limit state dir (%s) -- pacing locally", exc)
            return self._wait_without_shared_state(interval)

        try:
            lock_handle = open(self._lock_path(path), "a+")
        except OSError as exc:
            raise KISRateLimitStateInvalid(
                "cannot open the KIS rate-limit lock", detail=type(exc).__name__) from exc

        try:
            if not self._acquire(lock_handle):
                logger.warning("rate-limit lock timed out -- pacing locally")
                return self._wait_without_shared_state(interval)
            try:
                return self._wait_locked(path, category, interval)
            finally:
                try:
                    fcntl.flock(lock_handle, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            lock_handle.close()

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
        is_new_file = not path.exists()
        try:
            handle = open(path, "r+") if not is_new_file else open(path, "w+")
        except FileNotFoundError:
            is_new_file = True
            handle = open(path, "w+")
        except OSError as exc:
            raise KISRateLimitStateInvalid(
                "cannot open the KIS rate-limit state", detail=type(exc).__name__) from exc

        with handle:
            state = self._read_state(handle, is_new_file=is_new_file)
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
            try:
                handle.seek(0)
                handle.truncate()
                json.dump(state, handle)
                handle.flush()
                os.fsync(handle.fileno())
            except OSError as exc:
                logger.warning("could not record the rate-limit timestamp: %s", exc)
            return slept

    def _wait_without_shared_state(self, interval):
        """Degraded mode: still pace, just without cross-process sharing.
        Never "give up and fire immediately"."""
        self._sleeper(interval)
        return interval

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
