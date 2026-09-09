"""S6's own durable BUY_INTENT queue: the hand-off between fast-watch
READY detection and the (slow) shared qualification/broker pipeline.

Why this exists
----------------
`kis_live_trading.run_live_buy_entry_cycle()` -- the ONE shared
qualify -> risk -> sizing -> KIS -> broker path every strategy submits
through -- does three-plus sequential, rate-limited KIS network calls
PER READY CANDIDATE (price re-check, account snapshot, orderable cash,
open orders, positions). Measured 2026-09-09: ~44-45 seconds per
candidate. S6's fast-watch tick used to call that cycle inline, so a
tick with even one READY candidate could not return in under a minute,
and the next cron trigger was OVERLAP_SKIPPED.

This store lets fast-watch record "this symbol looked READY as of this
tick" and return immediately -- it carries no trading authority by
itself. A separate process (the execution worker,
`scripts/run_s6_buy_execution.py`) claims the queue and is the one that
actually calls the shared cycle, on its own cron/lock, decoupled from
fast-watch's cadence. Every existing gate -- qualification, risk,
sizing, cash, kill switch, session capability, the Execution Engine's
own idempotency and state machine -- still runs, unchanged, inside that
call; this module adds no new trading decision, only a mailbox.

Concurrency model, and why no cash-reservation ledger sits here
-----------------------------------------------------------------
`claim_ready()` atomically reads-and-empties the store under one flock,
so a fast-watch tick (producer, ADD-only) and the execution worker
(consumer, atomic drain) can never observe or lose each other's write.
The worker itself still runs one candidate at a time, serialized by its
own lock file, exactly as `run_live_buy_entry_cycle` always has -- so
this hand-off introduces no NEW concurrency into order submission, and
KIS's own per-candidate `get_orderable_usd()` check remains the sole
and sufficient cash authority, exactly as it is today. A local cash
ledger would be solving a race this design does not create.

File shape mirrors `active_watch.py`'s provisional-pass store on
purpose: same flock discipline, same atomic tmp-then-replace write, same
(session_date, session) scope check.
"""

import fcntl
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from s6_live.active_watch import _normal_symbol, _root, _utc

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "s6_buy_intent_v1"


def path_for(session_date, session, *, env=None) -> Path:
    return _root(env) / f"{session_date}-{str(session).upper()}-buy-intent.json"


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
    except Exception:  # noqa: BLE001 -- a corrupt file is a fresh queue,
        # not a reason to stop admitting or claiming intents.
        logger.warning("could not read S6 buy-intent store at %s",
                       target, exc_info=True)
        return _blank(session_date, session)
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION \
            or str(payload.get("session_date")) != str(session_date) \
            or str(payload.get("session")).upper() != str(session).upper():
        # Wrong/stale/missing scope: never carry another session's
        # intents forward, same rule active_watch applies to its own
        # provisional store.
        return _blank(session_date, session)
    return payload


def write_ready(session_date, session, rows: List[Dict[str, Any]], *,
                now=None, env=None) -> int:
    """Add/refresh READY candidates. Never raises -- an admission fault
    must not slow or fail the fast-watch tick that is trying to hand
    off quickly. Returns the number of symbols now pending.

    Idempotent per symbol: a symbol already pending keeps its
    `first_ready_at` (so READY->intent latency is measured from the
    first observation, not the most recent one) and has its row
    refreshed to the latest candidate data.
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
            written = 0
            for row in rows or ():
                symbol = _normal_symbol((row or {}).get("symbol"))
                if not symbol:
                    continue
                existing = entries.get(symbol) or {}
                entries[symbol] = {
                    "symbol": symbol,
                    "candidate": dict(row),
                    "first_ready_at": existing.get("first_ready_at") or moment.isoformat(),
                    "last_seen_ready_at": moment.isoformat(),
                }
                written += 1
            payload["entries"] = entries
            payload["updated_at"] = moment.isoformat()
            temp = target.with_suffix(".tmp")
            temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            temp.replace(target)
            return written
    except Exception:  # noqa: BLE001 -- see module docstring: a mailbox
        # fault must not become a fast-watch tick fault.
        logger.exception("S6 buy-intent admission failed")
        return 0


def read_ready(session_date, session, *, env=None) -> Dict[str, Dict[str, Any]]:
    """Non-destructive read, for health/funnel reporting only."""
    target = path_for(session_date, session, env=env)
    payload = _load_unlocked(target, session_date, session)
    return dict(payload.get("entries") or {})


def claim_ready(session_date, session, *, env=None) -> Dict[str, Dict[str, Any]]:
    """Atomically read-and-empty the queue: the only way the execution
    worker consumes it. One flock covers both the read and the clear,
    so a fast-watch tick writing concurrently can only land its
    addition strictly before or strictly after a claim, never lose it
    to one and never be claimed twice by two workers."""
    target = path_for(session_date, session, env=env)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_suffix(".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        payload = _load_unlocked(target, session_date, session)
        entries = dict(payload.get("entries") or {})
        if not entries:
            return {}
        empty = _blank(session_date, session)
        empty["updated_at"] = _utc(datetime.now(timezone.utc)).isoformat()
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(empty, indent=2, sort_keys=True), encoding="utf-8")
        temp.replace(target)
        return entries
