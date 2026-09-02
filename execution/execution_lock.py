"""The lock that serialises BROKER MUTATION, and nothing else.

The problem this exists for
---------------------------
`s6_exec.lock` was taken by the cron WRAPPER, so it was held for the
whole life of the process. For the exit monitor and the runtime tick
that is nearly free -- they are short. For the BUY entry it meant the
lock was held across the entire candidate funnel: precision watch,
pre-trade validation, per-symbol KIS quotes, sizing. On 2026-09-02 that
was three to five minutes per cycle, re-launched every minute, and the
one-minute held-position monitor acquired the lock 1 time in 29.

The consequence was measured, not theorised. A 180-second
`BUY_FILL_TTL` was enforced at 782s and 836s, because the only jobs that
can enforce it could not get in. Had the position filled, the exit rules
-- VWAP failure, EMA structure failure, range re-entry, volume decay --
would have been evaluated on the same ~15 minute clock the one-minute
monitor exists to replace.

What changed
------------
Analysis no longer holds this lock. Only the mutating step does:

    read candidates, validate, price, size      <- NO lock
    |
    `- acquire ---> REVALIDATE ---> submit ---> persist ---> release

The window is a submission, not a cycle, and a cycle that submits
nothing never takes the lock at all -- which is most of them.

Why the entry yields and never waits
------------------------------------
A new BUY is the lowest-priority use of execution access: below exits,
below position management, below reconciliation. It takes the lock
non-blocking with a short bounded retry and, failing that, DROPS the
prepared entry. It is never queued behind an exit.

Dropping is safe in the exact way deferring is: the next tick re-asks,
and by then the candidate is either still READY -- nothing lost -- or it
is not, in which case the order should not have been sent. Waiting would
reintroduce the starvation from the other side.

Fail closed
-----------
Every failure to hold this lock means NO submission. There is no path
where an order is sent because the lock could not be taken, and none
where it is sent twice because it was taken twice.
"""

import contextlib
import errno
import fcntl
import logging
import os
import time

logger = logging.getLogger(__name__)

#: Where the lock file lives. Same file the cron wrappers flock, so a
#: shell-held lock and a Python-held lock exclude each other -- that is
#: the entire point, and pointing this at a different path would give
#: broker mutation two independent locks and therefore none.
EXEC_LOCK_PATH_ENV = "S6_EXECUTION_LOCK_FILE"
DEFAULT_EXEC_LOCK_PATH = "/home/ubuntu/logs/cron/s6_exec.lock"

#: How long a BUY may spend trying to take the lock before giving up.
#: Deliberately short. This is NOT a fix for contention -- it is the
#: opposite: the entry gives way quickly so the holder is not delayed.
ACQUIRE_TIMEOUT_ENV = "S6_EXECUTION_LOCK_TIMEOUT_SECONDS"
DEFAULT_ACQUIRE_TIMEOUT_SECONDS = 2.0

_RETRY_INTERVAL_SECONDS = 0.05


class ExecutionLockUnavailable(RuntimeError):
    """The execution lock could not be held, so nothing was submitted."""

    def __init__(self, message, *, purpose=None, waited_seconds=None):
        super().__init__(message)
        self.purpose = purpose
        self.waited_seconds = waited_seconds


def lock_path() -> str:
    override = os.environ.get(EXEC_LOCK_PATH_ENV)
    if override and str(override).strip():
        return str(override).strip()
    return DEFAULT_EXEC_LOCK_PATH


def acquire_timeout() -> float:
    raw = os.environ.get(ACQUIRE_TIMEOUT_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_ACQUIRE_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_ACQUIRE_TIMEOUT_SECONDS
    # A non-positive timeout means "try once", not "wait forever".
    return max(0.0, value)


@contextlib.contextmanager
def hold(purpose, *, path=None, timeout_seconds=None, clock=None, sleeper=None):
    """Hold the execution lock for the duration of the block.

    Raises `ExecutionLockUnavailable` -- before entering the block -- if
    the lock cannot be taken within the timeout. The caller must treat
    that as "do not submit", never as "submit anyway".
    """
    target = path or lock_path()
    budget = acquire_timeout() if timeout_seconds is None else max(0.0, float(timeout_seconds))
    now = clock or time.monotonic
    rest = sleeper or time.sleep

    directory = os.path.dirname(target)
    if directory:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise ExecutionLockUnavailable(
                f"execution lock directory is unusable: {type(exc).__name__}",
                purpose=purpose) from exc

    started = now()
    try:
        handle = open(target, "a+")
    except OSError as exc:
        raise ExecutionLockUnavailable(
            f"execution lock file could not be opened: {type(exc).__name__}",
            purpose=purpose) from exc

    acquired = False
    try:
        deadline = started + budget
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise ExecutionLockUnavailable(
                        f"execution lock could not be taken: {type(exc).__name__}",
                        purpose=purpose) from exc
                if now() >= deadline:
                    break
                rest(_RETRY_INTERVAL_SECONDS)

        waited = now() - started
        if not acquired:
            logger.info(
                "EXEC_LOCK owner=%s outcome=SKIPPED wait_ms=%.1f "
                "(another execution cycle holds it; this attempt is dropped, "
                "not queued)", purpose, waited * 1000.0)
            raise ExecutionLockUnavailable(
                "another execution cycle holds the execution lock",
                purpose=purpose, waited_seconds=waited)

        held_from = now()
        logger.info("EXEC_LOCK owner=%s outcome=ACQUIRED wait_ms=%.1f",
                    purpose, waited * 1000.0)
        try:
            yield
        finally:
            held = now() - held_from
            logger.info("EXEC_LOCK owner=%s outcome=RELEASED hold_ms=%.1f",
                        purpose, held * 1000.0)
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                fcntl.flock(handle, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            handle.close()
