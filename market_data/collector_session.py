"""Is the running collector still writing the session we are actually in?

The gap this closes
-------------------
`run_realtime_bar_collector.py` resolves its session ONCE, at process
start, and carries it for its whole life -- the snapshot path and every
`add_trade` are keyed by that one value. A collector alive across a
session boundary therefore keeps appending to the PREVIOUS session's
file, and the new session's snapshot is not created until the next
collector STARTS. Its `coverage_started_at` is then minutes-to-an-hour
after that session's official origin, and `kis_bar_features` -- which
correctly judges coverage on a real bar at or before the open -- answers
OFFICIAL_ORIGIN_NOT_COVERED for every symbol in it.

Measured across every 2026-09-* snapshot: 13 of 16 were not covered,
lags +5.3 to +150.2 minutes, on all four sessions. The lag was never
zero under any collector lifetime, because no lifetime can make a frozen
session value follow the clock.

So the supervisor asks the one question the collector cannot ask itself,
and the answer is a restart -- the same bounded restart path a wedged
socket already takes.

Why this is not the health check
--------------------------------
A collector on the wrong session is perfectly healthy: connected,
subscribed, heartbeat current. `collector_health` would keep it, and
should -- it is answering "is this process working", which it is. This
answers "is it working on the right thing", which is a different
question with a different remedy, so it is a different verdict rather
than a new reason code inside that one.

Fail quiet, never churn
-----------------------
Anything unknown -- no status file, unreadable JSON, no recorded
session -- is NOT a mismatch. A missing status is already
`collector_health`'s NO_STATUS and it owns that decision; answering
"restart" here on the same evidence would restart twice for one fault.
A restart is also recorded and rate-limited on disk, so a collector that
somehow comes back still reporting the wrong session cannot be killed
every five minutes forever.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: One boundary crossing per cooldown. Real sessions are hours apart, so
#: this can never suppress a legitimate crossing; it only bounds a loop.
RESTART_COOLDOWN_SECONDS = 600.0
MARKER_NAME = "collector_session_restart.marker"

SESSION_MATCH = "SESSION_MATCH"
SESSION_CHANGED = "SESSION_CHANGED"
SESSION_UNKNOWN = "SESSION_UNKNOWN"


@dataclass(frozen=True)
class Verdict:
    restart: bool
    reason: str
    detail: str = ""


def _read_status(status_path) -> Optional[dict]:
    try:
        raw = Path(status_path).read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 - absent or unreadable is "unknown"
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _clean(value) -> str:
    return str(value or "").strip().upper()


def assess(status_path, *, current_session, process_running=True) -> Verdict:
    """Should the running collector be replaced because the session moved?

    `current_session` is the caller's clock reading -- passed in rather
    than computed here so the decision is testable without patching a
    clock, and so the shell and this module cannot disagree about which
    session function is authoritative.
    """
    if not process_running:
        # Nothing to replace. The wrapper starts one either way.
        return Verdict(False, SESSION_UNKNOWN, "no collector running")

    now_session = _clean(current_session)
    if not now_session:
        return Verdict(False, SESSION_UNKNOWN, "current session unknown")

    record = _read_status(status_path)
    if record is None:
        return Verdict(False, SESSION_UNKNOWN, "no readable collector status")

    running_session = _clean(record.get("market_session"))
    if not running_session:
        return Verdict(False, SESSION_UNKNOWN, "collector reports no session")

    if running_session == now_session:
        return Verdict(False, SESSION_MATCH, running_session)

    return Verdict(True, SESSION_CHANGED, f"{running_session}->{now_session}")


def _parse(stamp) -> Optional[datetime]:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def restart_allowed(marker_path, *, now=None,
                    cooldown=RESTART_COOLDOWN_SECONDS) -> bool:
    """At most one session restart per cooldown, remembered on disk."""
    current = now or datetime.now(timezone.utc)
    try:
        last = _parse(Path(marker_path).read_text(encoding="utf-8").strip())
    except Exception:  # noqa: BLE001
        last = None
    if last is not None and (current - last).total_seconds() < cooldown:
        return False
    return True


def note_restart(marker_path, *, now=None) -> None:
    current = now or datetime.now(timezone.utc)
    path = Path(marker_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(current.isoformat(), encoding="utf-8")


def main(argv=None) -> int:
    """CLI for the cron wrapper, same contract as `collector_health`:
    prints the verdict, exits 2 when a restart is recommended AND allowed,
    0 otherwise. Never exits non-zero for "I do not know"."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--status", required=True)
    parser.add_argument("--marker", required=True)
    parser.add_argument("--process-running", choices=("yes", "no"),
                        required=True)
    parser.add_argument("--current-session", default=None)
    args = parser.parse_args(argv)

    session = args.current_session
    if not session:
        from scanners.base import scan_session

        session = scan_session.session_at()

    verdict = assess(args.status, current_session=session,
                     process_running=args.process_running == "yes")
    print(f"{verdict.reason} {verdict.detail}")
    if verdict.restart and restart_allowed(args.marker):
        note_restart(args.marker)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
