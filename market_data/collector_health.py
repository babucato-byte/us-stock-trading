"""Is the realtime collector alive, connected and current? Read-only.

The collector writes `collector_status.json` on every heartbeat. This
module turns that file plus the process table into one verdict the cron
wrapper can act on, so a collector that is running but wedged --
process alive, socket dead, heartbeat stale -- is restarted within a
bounded time instead of holding its slot until the hourly exit.

What a restart may and may not do
---------------------------------
It may replace a collector whose heartbeat is older than
HEARTBEAT_MAX_AGE_SECONDS, or that has been DISCONNECTED / FAILED longer
than CONNECT_GRACE_SECONDS. It may do so at most once per
RESTART_COOLDOWN_SECONDS, recorded in a marker file beside the status,
so a feed that is genuinely down is not restarted every five minutes
forever. A collector with no trades is NOT unhealthy: CONNECTED_NO_TRADES
is the true state of a closed venue, and restarting on it would be
restarting on the market.
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

HEARTBEAT_MAX_AGE_SECONDS = 180.0
CONNECT_GRACE_SECONDS = 300.0
RESTART_COOLDOWN_SECONDS = 900.0
MARKER_NAME = "collector_restart.marker"

HEALTHY = "HEALTHY"
NO_PROCESS = "NO_PROCESS"
NO_STATUS = "NO_STATUS"
HEARTBEAT_STALE = "HEARTBEAT_STALE"
NOT_CONNECTED = "NOT_CONNECTED"
SUBSCRIPTIONS_INCOMPLETE = "SUBSCRIPTIONS_INCOMPLETE"


@dataclass(frozen=True)
class Verdict:
    healthy: bool
    reason: str
    detail: str = ""
    restart_recommended: bool = False


def _parse(stamp) -> Optional[datetime]:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def assess(status_path, *, process_running: bool, now=None,
           heartbeat_max_age=HEARTBEAT_MAX_AGE_SECONDS,
           connect_grace=CONNECT_GRACE_SECONDS) -> Verdict:
    """One verdict from the status file and the process table."""
    current = now or datetime.now(timezone.utc)
    if not process_running:
        return Verdict(False, NO_PROCESS, "no collector process", restart_recommended=False)
    path = Path(status_path)
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return Verdict(False, NO_STATUS, f"status unreadable: {type(exc).__name__}",
                       restart_recommended=True)
    heartbeat = _parse(status.get("last_heartbeat_at"))
    if heartbeat is None or (current - heartbeat).total_seconds() > heartbeat_max_age:
        age = None if heartbeat is None else round((current - heartbeat).total_seconds())
        return Verdict(False, HEARTBEAT_STALE, f"last heartbeat age {age}s",
                       restart_recommended=True)
    state = str(status.get("connection_state") or "")
    if state != "CONNECTED":
        started = _parse(status.get("collector_started_at"))
        age = (current - started).total_seconds() if started else None
        if age is None or age > connect_grace:
            return Verdict(False, NOT_CONNECTED, f"connection_state={state}",
                           restart_recommended=True)
        return Verdict(True, HEALTHY, f"connecting ({state}, {age:.0f}s since start)")
    requested, got = status.get("subscription_requested"), status.get("subscription_count")
    if requested and got != requested:
        return Verdict(False, SUBSCRIPTIONS_INCOMPLETE, f"{got}/{requested}",
                       restart_recommended=True)
    return Verdict(True, HEALTHY, f"{status.get('state')} {got}/{requested}")


def restart_allowed(marker_path, *, now=None, cooldown=RESTART_COOLDOWN_SECONDS) -> bool:
    """At most one forced restart per cooldown, remembered on disk."""
    current = now or datetime.now(timezone.utc)
    path = Path(marker_path)
    try:
        last = _parse(path.read_text(encoding="utf-8").strip())
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
    """CLI for the cron wrapper: prints the verdict, exits 0 when healthy,
    2 when a bounded restart is recommended and allowed, 1 otherwise."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--status", required=True)
    parser.add_argument("--marker", required=True)
    parser.add_argument("--process-running", choices=("yes", "no"), required=True)
    args = parser.parse_args(argv)
    verdict = assess(args.status, process_running=args.process_running == "yes")
    print(f"{verdict.reason} {verdict.detail}")
    if verdict.healthy:
        return 0
    if verdict.restart_recommended and restart_allowed(args.marker):
        note_restart(args.marker)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
