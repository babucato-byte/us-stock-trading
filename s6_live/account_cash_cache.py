"""The last observed account-level settled USD cash.

Why a cache and not a fresh read
---------------------------------
KIS answers orderable cash per (symbol, exchange, limit price) through
``KISBroker.get_orderable_usd``. There is no account-level value that may
substitute for that answer, so S6 BUY affordability does not consume this
cache.

`KISBroker.get_account_cash_usd()` reads settled/withdrawable USD. It can
be materially lower than buying power when KIS permits reuse of unsettled
sale proceeds, so this cache is retained only for consumers that explicitly
need settled account cash.

Same flock + atomic tmp-then-replace idiom as `buy_intent.py` and
`watch_priority_state.py`.
"""

import fcntl
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from s6_live.active_watch import _root, _utc

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "s6_account_cash_cache_v1"

#: How long a cached reading may be reused before a fresh call is made.
#: Deliberately short: this is a precheck meant to avoid OBVIOUSLY
#: wasted work (an empty account), not a substitute for the worker's
#: own fresh, authoritative, per-candidate read.
DEFAULT_MAX_AGE_SECONDS = 90.0


def path_for(*, env=None) -> Path:
    return _root(env) / "account-cash-cache.json"


def read(*, now=None, max_age_seconds=DEFAULT_MAX_AGE_SECONDS, env=None
         ) -> Optional[Dict[str, Any]]:
    """The cached reading, or None if absent/stale/unreadable.

    Never raises: a cache miss is exactly as safe as a cache the
    precheck was never wired to -- the caller falls back to its own
    policy (see `s6_live.cash_precheck`).
    """
    try:
        target = path_for(env=env)
        if not target.exists():
            return None
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.warning("could not read S6 account-cash cache", exc_info=True)
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        return None
    checked_at = payload.get("checked_at")
    try:
        checked = _utc(datetime.fromisoformat(str(checked_at)))
    except Exception:  # noqa: BLE001
        return None
    moment = _utc(now or datetime.now(timezone.utc))
    if (moment - checked).total_seconds() > max_age_seconds:
        return None
    return dict(payload)


def write(available_usd: float, *, now=None, source="get_account_cash_usd",
         env=None) -> None:
    """Record one fresh reading. Never raises -- a failed write only
    costs the next tick one avoidable KIS call, never a trading
    decision."""
    try:
        moment = _utc(now or datetime.now(timezone.utc))
        target = path_for(env=env)
        target.parent.mkdir(parents=True, exist_ok=True)
        lock_path = target.with_suffix(".lock")
        with open(lock_path, "a+", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            payload = {
                "schema_version": SCHEMA_VERSION,
                "available_usd": float(available_usd),
                "checked_at": moment.isoformat(),
                "source": source,
            }
            temp = target.with_suffix(".tmp")
            temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            temp.replace(target)
    except Exception:  # noqa: BLE001
        logger.warning("S6 account-cash cache write failed", exc_info=True)
