"""Is a scan RUNNING right now, and how did the last one end?

The gap this closes
-------------------
S6 scans at :02/:17/:32/:47 and its runtime consumes at :07/:22/:37/:52 --
five minutes apart. A scan that takes longer than five minutes is not a
missing candidate file: it is a candidate file holding the PREVIOUS
cycle's answer, with the same trading day, the same session and the same
variant as the one being computed. Every refusal S6's candidate source
already makes is keyed on one of those three, so a superseded row passes
all of them and reaches the BUY cycle looking current.

That cannot be fixed with an age limit. Picking "candidates older than N
minutes are stale" would be inventing a threshold nobody measured, and
the codebase refuses those on principle -- see `s1_live/freshness.py` on
why `max_age_seconds` is still None. The honest question is not "how old
is this row" but "is the answer that supersedes it being computed right
now", and that has a factual answer.

The lock IS the marker
----------------------
A scan holds an exclusive `flock` on its cycle file for as long as it
runs. "Is a scan in progress" is then answered by trying to take the same
lock, and the answer is true exactly while a process is alive and
holding it. A crashed or killed scan releases the lock in the kernel,
so the state cannot get stuck -- which a plain "I am running" flag file
absolutely can, and it would block S6 entries until somebody noticed and
deleted it.

Queued, never stacked
---------------------
The lock is not waited on. A cron firing while the previous run of the
SAME scan is still going gets a refusal and exits, rather than queueing a
second pass over the universe behind the first. Two concurrent scans
would append two answers to one candidate file with no way to tell which
one a consumer read.

It grants nothing
-----------------
Holding the lock does not make a scan publishable, and releasing it does
not make a candidate tradeable. This module records two facts and
answers questions about them. Every refusal that already existed still
applies.
"""

import json
import logging
import os
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:  # pragma: no cover - present on every platform this runs on
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

CYCLE_SUFFIX = ".scan"

#: How a completed scan ended, as recorded on the run marker.
STATUS_OK = "OK"
STATUS_FAILED = "FAILED"

#: Statuses that leave the published rows usable. Anything else means the
#: scan did not produce a complete answer, and a partial answer written
#: into a hand-off file is indistinguishable from a complete one.
CONSUMABLE_STATUSES = frozenset({STATUS_OK})

REASON_SCAN_IN_PROGRESS = "SCAN_IN_PROGRESS"
REASON_SCAN_FAILED = "LAST_SCAN_FAILED"
REASON_UNDETECTABLE = "SCAN_STATE_UNDETECTABLE"


@dataclass(frozen=True)
class CycleState:
    """What a consumer needs to know before reading a candidate file."""

    running: bool
    detectable: bool = True
    started_at: Optional[str] = None
    run_id: Optional[str] = None
    pid: Optional[int] = None
    scanner: Optional[str] = None
    detail: str = ""

    @property
    def blocks_consumption(self) -> bool:
        """Fail closed. "Could not tell" is not "no scan is running".

        A consumer that treated an undetectable state as idle would
        resume exactly the behaviour this module exists to stop, and it
        would do it silently on the one platform where the check does not
        work.
        """
        return self.running or not self.detectable

    def refusal(self) -> Optional[str]:
        if not self.blocks_consumption:
            return None
        if not self.detectable:
            return (f"{REASON_UNDETECTABLE}: whether a scan is running "
                    f"could not be established ({self.detail})")
        return (f"{REASON_SCAN_IN_PROGRESS}: a scan started at "
                f"{self.started_at} is still running; the published rows "
                f"are the previous cycle's")

    def as_dict(self) -> Dict[str, Any]:
        return {"running": self.running, "detectable": self.detectable,
                "started_at": self.started_at, "run_id": self.run_id,
                "pid": self.pid, "scanner": self.scanner,
                "detail": self.detail}


@dataclass
class Hold:
    """The result of trying to start a scan cycle."""

    acquired: bool
    scanner: Optional[str] = None
    started_at: Optional[str] = None
    detail: str = ""
    #: Populated for a caller that wants to name what it collided with.
    blocked_by: Optional[CycleState] = None

    @property
    def skipped(self) -> bool:
        return not self.acquired


@dataclass
class Cycle:
    """Several scanners' holds, taken together for one run."""

    holds: List[Hold] = field(default_factory=list)
    started_at: Optional[str] = None

    @property
    def acquired(self) -> bool:
        """True when every requested lock was taken.

        All or nothing on purpose: a run that proceeded with half its
        locks would publish for one strategy while a second copy of
        itself published for another.
        """
        return all(hold.acquired for hold in self.holds)

    @property
    def skipped(self) -> bool:
        return not self.acquired

    def blocked(self) -> List[Hold]:
        return [hold for hold in self.holds if not hold.acquired]

    def detail(self) -> str:
        return "; ".join(
            f"{hold.scanner}: {hold.detail}" for hold in self.blocked()) or ""


def _directory() -> Path:
    from scanners.publish import candidates as publisher

    return publisher.candidate_dir()


def cycle_path(trading_day, session=None, scanner=None) -> Path:
    """One cycle file per (day, session, scanner).

    Per SCANNER, not per run: two different cron entries scanning the
    same session are not two copies of each other, and locking them
    against one another would make S1's premarket scan able to skip S6's.
    A cron entry colliding with ITSELF is the case worth refusing, and
    that collision is always same-scanner.
    """
    directory = _directory()
    directory.mkdir(parents=True, exist_ok=True)
    parts = [str(trading_day)]
    if session:
        parts.append(str(session))
    if scanner:
        parts.append(str(scanner))
    return directory / ("-".join(parts) + CYCLE_SUFFIX)


def _payload(path: Path) -> Dict[str, Any]:
    """The holder's own record, or {} if it cannot be read.

    A torn read is not an error: the file is rewritten while the lock is
    held, so a reader can catch it mid-write. The LOCK is the fact; this
    is only the description of who holds it.
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
        return json.loads(text) if text else {}
    except Exception:  # noqa: BLE001
        return {}


@contextmanager
def hold(trading_day, session=None, *, scanner=None, run_id=None,
         strategy_id=None, now=None):
    """Take the cycle lock for one scanner, or report that it is taken.

    Never blocks and never queues. The caller gets `acquired=False` and
    decides -- which for a scheduled scan means exiting, because the
    answer it would have produced is already being produced.
    """
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    if fcntl is None:  # pragma: no cover - not reachable on this platform
        yield Hold(False, scanner, stamp,
                   "flock is unavailable; refusing to run a scan whose "
                   "overlap could not be detected")
        return

    try:
        path = cycle_path(trading_day, session, scanner)
    except Exception as exc:  # noqa: BLE001 - a misconfigured hand-off
        # directory is already a loud failure elsewhere; here it must not
        # be reported as "the lock was free".
        yield Hold(False, scanner, stamp, f"cycle file unavailable: {exc}")
        return

    try:
        handle = open(path, "a+", encoding="utf-8")  # noqa: SIM115
    except OSError as exc:
        # A cycle file that cannot be opened is a scan whose overlap
        # could not be detected. Refusing to run is the same direction
        # every other check here fails in.
        yield Hold(False, scanner, stamp, f"cycle file unopenable: {exc}")
        return

    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            state = _state_from(path, running=True)
            logger.warning("scan cycle for %s/%s/%s is already held by pid %s "
                           "since %s -- not queueing a second scan",
                           trading_day, session, scanner, state.pid,
                           state.started_at)
            yield Hold(False, scanner, stamp,
                       f"a {scanner} scan started at {state.started_at} "
                       f"(pid {state.pid}) is still running",
                       blocked_by=state)
            return

        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({
            "trading_day": str(trading_day), "session": session,
            "scanner": scanner, "strategy_id": strategy_id,
            "run_id": run_id, "pid": os.getpid(), "started_at": stamp,
        }, sort_keys=True, default=str))
        handle.flush()
        try:
            yield Hold(True, scanner, stamp)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


@contextmanager
def hold_all(trading_day, session=None, *, scanners=(), run_id=None, now=None):
    """Every publishing scanner's lock for one run, taken together.

    An `ExitStack` holds them all for the caller's whole body and releases
    them in reverse order, so a collision part way through still frees
    what was already taken. Acquisition stops at the first refusal: the
    run is not going to proceed, and taking the rest would only delay
    saying so.
    """
    names = [str(name) for name in (scanners or [])]
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    if not names:
        yield Cycle(holds=[], started_at=stamp)
        return

    holds: List[Hold] = []
    with ExitStack() as exits:
        for name in names:
            taken = exits.enter_context(
                hold(trading_day, session, scanner=name, run_id=run_id,
                     now=now))
            holds.append(taken)
            if not taken.acquired:
                break
        yield Cycle(holds=list(holds), started_at=stamp)


def _state_from(path: Path, *, running: bool) -> CycleState:
    payload = _payload(path)
    return CycleState(
        running=running,
        started_at=payload.get("started_at"),
        run_id=payload.get("run_id"),
        pid=payload.get("pid"),
        scanner=payload.get("scanner"),
    )


def state(trading_day, session=None, *, scanner=None) -> CycleState:
    """Whether a scan for this (day, session, scanner) is running NOW.

    Answered by trying the lock rather than by reading a flag, so a scan
    that died without cleaning up reports as finished -- the kernel
    released its lock -- while one that is merely slow reports as
    running.
    """
    if fcntl is None:  # pragma: no cover
        return CycleState(running=False, detectable=False,
                          detail="fcntl is unavailable on this platform")
    try:
        path = cycle_path(trading_day, session, scanner)
    except Exception as exc:  # noqa: BLE001
        return CycleState(running=False, detectable=False,
                          detail=f"cycle file unavailable: {exc}")
    if not path.exists():
        # No scan has ever started for this (day, session, scanner).
        # Not running, and nothing to describe.
        return CycleState(running=False)

    try:
        handle = open(path, "r", encoding="utf-8")  # noqa: SIM115
    except OSError as exc:
        return CycleState(running=False, detectable=False,
                          detail=f"cycle file unreadable: {exc}")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return _state_from(path, running=True)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return _state_from(path, running=False)
    finally:
        handle.close()


def in_progress(trading_day, session=None, *, scanner=None) -> bool:
    return state(trading_day, session, scanner=scanner).running


def latest_run(trading_day, session=None, *, strategy_id=None
               ) -> Optional[Dict[str, Any]]:
    """The last run marker for this (day, session), optionally per strategy.

    The marker file is append-only, so "the last line" is the most recent
    run. Returns None when no run was ever marked -- which the candidate
    source already distinguishes from a run that found nothing.
    """
    from scanners.publish import candidates as publisher

    try:
        path = publisher.run_marker_path(trading_day, session)
        if not path.exists():
            return None
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except Exception:  # noqa: BLE001
        logger.warning("could not read the %s/%s run marker", trading_day,
                       session, exc_info=True)
        return None

    if strategy_id is not None:
        rows = [r for r in rows if str(r.get("strategy_id")) == str(strategy_id)]
    return rows[-1] if rows else None


def last_run_consumable(trading_day, session=None, *, strategy_id=None):
    """(ok, detail) for the most recent marked run.

    A marker written before this module existed carries no status. That
    absence means "the scan completed" -- which is what marking meant at
    the time -- and is deliberately not read as a failure, because
    treating every historical marker as broken would refuse candidates
    that were fine.
    """
    row = latest_run(trading_day, session, strategy_id=strategy_id)
    if row is None:
        return True, ""  # "no run at all" is the candidate source's own case
    status = row.get("status")
    if status is None:
        return True, ""
    if str(status) in CONSUMABLE_STATUSES:
        return True, ""
    return False, (f"{REASON_SCAN_FAILED}: the last {strategy_id or 'scan'} "
                   f"run for {trading_day}/{session} ended {status!r}")
