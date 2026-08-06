"""T9: the pilot loop itself.

One tick is:

    1. what session is it?          -> outside the allowed sessions the
                                       tick is recorded as IDLE and makes
                                       no KIS call at all
    2. is a scan due?               -> daily_candidate_scanner.scan()
                                       refreshes order_candidates.csv
    3. entry evaluation             -> OBSERVE or ARMED (posture is
                                       re-read from the environment on
                                       EVERY tick, never cached)
    4. exit evaluation              -> OBSERVE or ARMED
    5. record the tick              -> one JSONL line, fsynced

and the loop repeats until --max-ticks, --until, an operator's SIGINT/
SIGTERM, or the end of the last allowed session. The daily report is
written when the loop ends, however it ends -- including on a signal,
which is when an operator most wants the summary.

Exclusivity: the pilot holds its OWN flock for the session. It must not
hold `execution.idempotency.single_run_lock()`, which reconciliation,
the Shadow timer and the health report all contend for; preflight takes
and releases that one to prove no other trading process is mid-run.
"""

import fcntl
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

from live_pilot import observe, recorder
from live_pilot.posture import POSTURE_ARMED, resolve_posture

logger = logging.getLogger("live_pilot.runner")

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_SCAN_INTERVAL_SECONDS = 900
DEFAULT_SESSIONS = ("regular",)

TICK_IDLE = "IDLE"
TICK_RAN = "RAN"


class PilotLockError(Exception):
    """Another pilot already holds the session lock."""


def lock_path(env=None):
    """Deliberately derived from the ENVIRONMENT's log directory, not
    from a `--log-dir` argument: two pilots pointed at different output
    directories are still two pilots reading the same account and writing
    the same order state, and they must contend for the same lock.
    LIVE_PILOT_LOCK_FILE exists for a host that genuinely runs separate
    deployments."""
    mapping = os.environ if env is None else env
    raw = (mapping.get("LIVE_PILOT_LOCK_FILE") or "").strip()
    if raw:
        return Path(raw)
    return recorder.log_dir(mapping) / "live_pilot.lock"


class PilotLock:
    """A whole-session exclusive lock, released when the process exits
    for any reason (the kernel drops the flock with the descriptor), so a
    killed pilot never leaves a stale lock behind."""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else lock_path()
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._fh.close()
            self._fh = None
            raise PilotLockError(
                f"another live pilot holds {self.path} -- refusing to start a second one"
            ) from exc
        return self

    def __exit__(self, *_exc):
        if self._fh is not None:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None
        return False


class StopSignal:
    """SIGINT/SIGTERM set a flag; the loop finishes the tick it is in and
    then exits cleanly. Killing the process mid-tick would lose that
    tick's record, which is the one artefact the pilot exists to produce.
    A SECOND signal is left to the default handler, so an operator who
    really wants it gone is not trapped."""

    def __init__(self):
        self.requested = False
        self.signal_name = None
        self._previous = {}

    def install(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous[sig] = signal.signal(sig, self._handle)
            except ValueError:  # not on the main thread -- tests, embedding
                logger.debug("could not install a handler for %s", sig)
        return self

    def restore(self):
        for sig, handler in self._previous.items():
            try:
                signal.signal(sig, handler)
            except ValueError:
                pass
        self._previous.clear()

    def _handle(self, signum, _frame):
        self.requested = True
        self.signal_name = signal.Signals(signum).name
        logger.warning("%s received -- finishing this tick, then stopping",
                       self.signal_name)
        signal.signal(signum, signal.SIG_DFL)


def current_session(now=None):
    from market_hours import get_us_market_session

    return get_us_market_session(now)


def run_scan(*, scan_limit=None, preset=None):
    """One scanner pass. Returns the tick's `scan` section.

    Calls `daily_candidate_scanner.scan()` -- the same function the daily
    pipeline runs -- so the pilot's candidates are produced by the real
    scanner, not by a pilot-only shortcut. Slack is suppressed: a pilot
    ticking every few minutes must not page the channel every few
    minutes.
    """
    import daily_candidate_scanner

    started = time.monotonic()
    try:
        buckets = daily_candidate_scanner.scan(
            preset_name=preset, send_slack=False, scan_limit=scan_limit,
        )
    except Exception as exc:  # noqa: BLE001 -- a failed scan must not end the session
        logger.exception("scanner pass failed")
        return {"ran": False, "error": f"{type(exc).__name__}: {exc}",
                "duration_seconds": round(time.monotonic() - started, 3)}
    return {
        "ran": True,
        "error": None,
        "candidates": int(len(buckets.candidates)),
        "strong_candidates": int(len(buckets.strong_candidates)),
        "order_candidates": int(len(buckets.order_candidates)),
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def load_watchlist():
    import paper_strategy_order as pso

    return pso.load_watchlist()


def run_tick(*, tick_seq, broker, now=None, sessions=DEFAULT_SESSIONS, scan_due=False,
             scan_limit=None, preset=None, env=None, watchlist=None):
    """Evaluates one tick and returns the row to record. Never raises for
    a per-stage failure: an entry pass that blew up must still leave a
    recorded tick saying so, or the log silently under-reports the day.

    FatalRepositoryConnectionError is the one exception that IS allowed
    out, unwrapped -- it means this process may still hold a SQLite write
    lock that blocks every other writer, and only exiting releases it.
    """
    from execution.order_repository import FatalRepositoryConnectionError

    started_at = now or datetime.now(timezone.utc)
    decision = resolve_posture(env)
    session = current_session(started_at)
    row = {
        "tick_seq": tick_seq,
        "started_at": started_at.isoformat(),
        "session": session,
        "kis_env": (os.environ if env is None else env).get("KIS_ENV", ""),
        "skipped": False,
        "skip_reason": None,
        "scan": {"ran": False},
        "entry": None,
        "exit": None,
        "error": None,
    }
    row.update(decision.as_dict())

    if session not in sessions:
        row["skipped"] = True
        row["skip_reason"] = f"session={session} not in {sorted(sessions)}"
        row["status"] = TICK_IDLE
        row["finished_at"] = datetime.now(timezone.utc).isoformat()
        return row

    row["status"] = TICK_RAN
    if scan_due:
        row["scan"] = run_scan(scan_limit=scan_limit, preset=preset)

    symbols = watchlist if watchlist is not None else load_watchlist()

    if decision.armed:
        from live_pilot import armed

        entry_stage, exit_stage = armed.entry_cycle, armed.exit_cycle
        entry_kwargs = {"broker": broker, "now": started_at}
    else:
        entry_stage, exit_stage = observe.evaluate_entries, observe.evaluate_exits
        entry_kwargs = {"broker": broker, "watchlist": symbols, "now": started_at}

    try:
        row["entry"] = entry_stage(**entry_kwargs)
    except FatalRepositoryConnectionError:
        raise
    except Exception as exc:  # noqa: BLE001 -- recorded, not fatal to the session
        logger.exception("entry stage failed on tick %s", tick_seq)
        row["entry"] = {"mode": decision.posture, "evaluations": 0, "outcomes": [],
                        "submitted": [], "error": f"{type(exc).__name__}: {exc}"}

    try:
        row["exit"] = exit_stage(broker=broker, now=started_at)
    except FatalRepositoryConnectionError:
        raise
    except Exception as exc:  # noqa: BLE001 -- recorded, not fatal to the session
        logger.exception("exit stage failed on tick %s", tick_seq)
        row["exit"] = {"mode": decision.posture, "evaluations": 0, "outcomes": [],
                       "error": f"{type(exc).__name__}: {exc}"}

    errors = [stage["error"] for stage in (row["entry"], row["exit"])
              if stage and stage.get("error")]
    if errors:
        row["error"] = "; ".join(errors)
    row["finished_at"] = datetime.now(timezone.utc).isoformat()
    return row


def run_loop(*, broker, interval=DEFAULT_INTERVAL_SECONDS, max_ticks=None, until=None,
             sessions=DEFAULT_SESSIONS, scan_interval=DEFAULT_SCAN_INTERVAL_SECONDS,
             scan_limit=None, preset=None, env=None, directory=None, stop=None,
             sleep=time.sleep, now_fn=None):
    """Runs ticks until a stop condition. Returns a summary dict.

    `until` is an aware datetime; `max_ticks` a count; `stop` a
    StopSignal. All three are checked BETWEEN ticks, never mid-tick, so
    every tick that started is also recorded.
    """
    clock = now_fn or (lambda: datetime.now(timezone.utc))
    stop = stop if stop is not None else StopSignal()
    tick_seq = 0
    recorded = 0
    last_scan_at = None
    stopped_because = "max_ticks" if max_ticks else "signal"

    while True:
        if stop.requested:
            stopped_because = f"signal:{stop.signal_name}"
            break
        if max_ticks is not None and tick_seq >= max_ticks:
            stopped_because = "max_ticks"
            break
        now = clock()
        if until is not None and now >= until:
            stopped_because = "until"
            break

        tick_seq += 1
        scan_due = scan_interval is not None and scan_interval > 0 and (
            last_scan_at is None or (now - last_scan_at).total_seconds() >= scan_interval
        )
        row = run_tick(
            tick_seq=tick_seq, broker=broker, now=now, sessions=sessions,
            scan_due=scan_due, scan_limit=scan_limit, preset=preset, env=env,
        )
        if row.get("scan", {}).get("ran"):
            last_scan_at = now
        try:
            recorder.record_tick(row, directory=directory)
            recorded += 1
        except recorder.RecorderError:
            # The tick happened; only its record failed. Log loudly and
            # keep going -- stopping the session would lose the ticks
            # that would still have been recordable.
            logger.exception("could not record tick %s", tick_seq)
        logger.info(
            "tick %s: status=%s session=%s posture=%s entry=%s exit=%s",
            tick_seq, row.get("status"), row.get("session"), row.get("posture"),
            (row.get("entry") or {}).get("evaluations"),
            (row.get("exit") or {}).get("evaluations"),
        )

        if max_ticks is not None and tick_seq >= max_ticks:
            stopped_because = "max_ticks"
            break
        if stop.requested:
            stopped_because = f"signal:{stop.signal_name}"
            break
        if interval > 0:
            sleep(interval)

    report_target, report = recorder.write_report(
        for_date=clock().date(), directory=directory)
    return {
        "ticks": tick_seq,
        "recorded": recorded,
        "stopped_because": stopped_because,
        "report_path": str(report_target),
        "report": report,
    }


def build_broker():
    """Constructed here (not at import) so importing the runner never
    needs credentials -- the whole test suite depends on that."""
    from brokers.kis_broker import KISBroker

    return KISBroker()
