"""Whether held positions are actually being evaluated, and how recently.

The problem this exists for
---------------------------
The one-minute exit monitor is the thing that makes S6's exit rules
real: VWAP failure, EMA structure failure, range re-entry and volume
decay are all conditions that can become true and be gone again inside a
quarter hour. The monitor is scheduled every minute so they are not
missed.

On 2026-09-02 it ran once in twenty-nine scheduled minutes -- the entry
cycle held the execution lock -- and NOTHING SAID SO. A blocked tick
exited silently, so "the monitor is starved" and "the monitor had
nothing to do" produced the same empty log. The starvation was only
found by counting cron firings in syslog against reports written, after
a position had already been lost.

So the fact is now recorded rather than inferred: every evaluation of a
held position stamps a heartbeat, and a held position whose heartbeat
has gone stale is a reportable condition.

What this is NOT
----------------
It does not trade. It does not stop trading, cancel anything, or change
a threshold. It answers one question -- "has a position S6 holds been
looked at recently?" -- and a stale answer is an operator signal, not an
input to any strategy rule. A health check that could halt entries would
be a second kill switch nobody asked for, and one that could force an
exit would be an exit rule that never went through the exit policy.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

HEARTBEAT_PATH_ENV = "S6_MONITOR_HEARTBEAT_FILE"
DEFAULT_HEARTBEAT_PATH = (
    "/home/ubuntu/releases/us-stock-trading/shared/state/s6_monitor_heartbeat.json")

#: How long a held position may go un-evaluated before it is reportable.
#:
#: Five minutes against a one-minute schedule: four consecutive misses.
#: Tight enough that the 1-in-29 starvation would have been flagged
#: within minutes, loose enough that a single slow tick is not an alert.
STALE_AFTER_SECONDS = 300.0

STATUS_OK = "OK"
STATUS_STALE = "STALE"
STATUS_FLAT = "FLAT"
STATUS_UNKNOWN = "UNKNOWN"


def heartbeat_path() -> str:
    override = os.environ.get(HEARTBEAT_PATH_ENV)
    if override and str(override).strip():
        return str(override).strip()
    return DEFAULT_HEARTBEAT_PATH


def record_evaluation(*, held_count, now=None, path=None) -> bool:
    """Stamp that held positions were evaluated. Never raises."""
    target = path or heartbeat_path()
    moment = now or datetime.now(timezone.utc)
    payload = {"evaluated_at": moment.isoformat(), "held_count": int(held_count or 0)}
    try:
        directory = os.path.dirname(target)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temporary = f"{target}.tmp"
        with open(temporary, "w") as handle:
            json.dump(payload, handle)
        os.replace(temporary, target)
        return True
    except Exception:  # noqa: BLE001 -- a heartbeat that cannot be written
        # must not fail the tick that was doing the real work.
        logger.warning("S6 monitor heartbeat could not be written", exc_info=True)
        return False


def _read(path) -> Optional[Dict[str, Any]]:
    try:
        with open(path) as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        logger.warning("S6 monitor heartbeat is unreadable", exc_info=True)
        return None


def check(conn, *, now=None, path=None,
          stale_after_seconds=STALE_AFTER_SECONDS) -> Dict[str, Any]:
    """Report whether held positions are being evaluated.

    FLAT when nothing is held -- silence is correct then, and the monitor
    deliberately does not even take the lock. STALE only when something
    IS held and has not been looked at inside the window.
    """
    from s6_live import position_store

    moment = now or datetime.now(timezone.utc)
    try:
        held = [row for _pid, row in (position_store.load_live(conn) or ())]
    except Exception:  # noqa: BLE001
        return {"status": STATUS_UNKNOWN, "detail": "position store unreadable"}

    if not held:
        return {"status": STATUS_FLAT, "held_count": 0}

    record = _read(path or heartbeat_path())
    if not record or not record.get("evaluated_at"):
        return {"status": STATUS_UNKNOWN, "held_count": len(held),
                "detail": "no evaluation has been recorded yet"}

    try:
        stamped = datetime.fromisoformat(str(record["evaluated_at"]))
        if stamped.tzinfo is None:
            stamped = stamped.replace(tzinfo=timezone.utc)
        age = (moment - stamped).total_seconds()
    except (TypeError, ValueError):
        return {"status": STATUS_UNKNOWN, "held_count": len(held),
                "detail": "the recorded evaluation time is unparseable"}

    if age >= float(stale_after_seconds):
        symbols = sorted({str(r.get("symbol")) for r in held})
        logger.warning(
            "S6_MONITOR_STALE held=%s symbols=%s last_evaluated_age_s=%.0f "
            "threshold_s=%.0f -- a held position has not been evaluated "
            "recently; exit conditions may be going unseen",
            len(held), ",".join(symbols), age, float(stale_after_seconds))
        return {"status": STATUS_STALE, "held_count": len(held),
                "symbols": symbols, "age_seconds": age,
                "threshold_seconds": float(stale_after_seconds)}

    return {"status": STATUS_OK, "held_count": len(held), "age_seconds": age}
