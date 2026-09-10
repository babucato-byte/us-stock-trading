"""Cross-tick scheduling state for fast-watch's priority scheduler.

Not a strategy verdict store: nothing here changes whether a symbol is
READY. It answers two purely scheduling questions that only a PRIOR
tick's own result can answer:

1. Was this symbol close to READY last time it was actually evaluated
   (few blocking conditions left)? -- promotes it into the HOT tier
   even though it carries no fresh scanner signal.
2. How many consecutive ticks in a row has this symbol been DEFERRED
   without ever being evaluated? -- an aging counter so a cold,
   low-priority symbol cannot be starved forever by a watchlist that
   is always busy with higher-priority work; see fast_watch.py's
   secondary sort key.

Same flock + atomic tmp-then-replace idiom as buy_intent.py and
active_watch.py's own provisional store.
"""

import fcntl
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from s6_live.active_watch import _normal_symbol, _root, _utc

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "s6_watch_priority_state_v1"

#: A symbol whose last real evaluation left this many (or fewer)
#: blocking conditions is treated as READY-near -- one or two
#: improving indicators away from qualifying, not a cold, unrelated
#: name. Chosen to match "near-ready" in the plain-English sense
#: without reading strategy-specific meaning into which conditions
#: those are; that stays entirely precision_watch's decision.
READY_NEAR_MAX_BLOCKING = 1


def path_for(session_date, session, *, env=None) -> Path:
    return _root(env) / f"{session_date}-{str(session).upper()}-watch-priority.json"


def _blank(session_date, session) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "session_date": str(session_date),
        "session": str(session).upper(),
        "entries": {},
    }


def _load_unlocked(target: Path, session_date, session) -> Dict[str, Any]:
    if not target.exists():
        return _blank(session_date, session)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 -- a corrupt file starts fresh, it
        # never blocks scheduling.
        logger.warning("could not read S6 watch-priority state at %s",
                       target, exc_info=True)
        return _blank(session_date, session)
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION \
            or str(payload.get("session_date")) != str(session_date) \
            or str(payload.get("session")).upper() != str(session).upper():
        return _blank(session_date, session)
    return payload


def read(session_date, session, *, env=None) -> Dict[str, Dict[str, Any]]:
    """Current per-symbol scheduling state, non-destructive.

    Never raises: an absent/misconfigured store means "nothing known
    yet" (no aging boost, no READY-near promotion), the same as a
    genuinely empty one -- scheduling degrades to the plain priority
    order, it does not stop the tick.
    """
    try:
        target = path_for(session_date, session, env=env)
    except Exception:  # noqa: BLE001 -- e.g. no SCANNER_DATA_ROOT/
        # S6_ACTIVE_WATCH_DIR configured at all.
        return {}
    payload = _load_unlocked(target, session_date, session)
    return dict(payload.get("entries") or {})


def update(session_date, session, *, evaluated, deferred, now=None, env=None) -> None:
    """One write per tick, after the evaluation loop finishes.

    `evaluated`: {symbol: {"state": ..., "blocking_count": int}} for
    every symbol this tick actually reached -- consecutive_defers
    resets to 0 for these.

    `deferred`: iterable of symbols the tick's budget ran out before
    reaching -- consecutive_defers increments for these.

    Never raises: a scheduling-state write failing must fall back to
    the existing, safe behaviour (no aging boost), not stop the tick.
    """
    try:
        moment = _utc(now or datetime.now(timezone.utc))
        target = path_for(session_date, session, env=env)
        target.parent.mkdir(parents=True, exist_ok=True)
        lock_path = target.with_suffix(".lock")
        with open(lock_path, "a+", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            payload = _load_unlocked(target, session_date, session)
            entries = payload.get("entries") or {}
            for symbol, info in (evaluated or {}).items():
                symbol = _normal_symbol(symbol)
                if not symbol:
                    continue
                entries[symbol] = {
                    "last_state": info.get("state"),
                    "last_blocking_count": info.get("blocking_count"),
                    "consecutive_defers": 0,
                    "last_evaluated_at": moment.isoformat(),
                }
            for symbol in (deferred or ()):
                symbol = _normal_symbol(symbol)
                if not symbol:
                    continue
                existing = entries.get(symbol) or {}
                entries[symbol] = {
                    **existing,
                    "consecutive_defers": int(existing.get("consecutive_defers") or 0) + 1,
                }
            payload["entries"] = entries
            payload["updated_at"] = moment.isoformat()
            temp = target.with_suffix(".tmp")
            temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            temp.replace(target)
    except Exception:  # noqa: BLE001 -- scheduling metadata, not a
        # trading decision; must not fail the tick that computed it.
        logger.warning("S6 watch-priority state update failed", exc_info=True)


def is_ready_near(state: Dict[str, Any]) -> bool:
    """True when the last real evaluation for this symbol left few
    enough blocking conditions to treat it as close to READY."""
    count = state.get("last_blocking_count")
    return (state.get("last_state") == "READY"
            or (isinstance(count, int) and 0 <= count <= READY_NEAR_MAX_BLOCKING))


def consecutive_defers(state: Dict[str, Any]) -> int:
    value = state.get("consecutive_defers")
    return int(value) if isinstance(value, int) else 0
