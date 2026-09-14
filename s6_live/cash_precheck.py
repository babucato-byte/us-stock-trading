"""READY -> BUY_INTENT cash precheck (§7-9): skip an intent nobody could
ever afford, without adding a second cash authority.

Not authoritative. The execution worker's own `get_orderable_usd()`
check (`kis_live_trading.run_live_buy_entry_cycle`) remains the sole
source of truth for whether an order can actually be placed -- this
only avoids handing it a candidate that a recent, coarser account-level
read already shows the account plainly cannot afford at all. See
`s6_live/account_cash_cache.py` for why the read is account-level (not
per-price) and cached rather than fresh every tick.

ACCOUNT_STATE_FOLLOWUP_REQUIRED (§9): the durable state this repo
already keeps (`s6_positions`, `kis_order_idempotency`, ...) has no
place a per-symbol orderable-cash figure could be read from without a
network call, and building one is out of scope for this change. This
precheck uses the safest thing that already exists --
`KISBroker.get_account_cash_usd()`, proven to match
`get_orderable_usd()` exactly on a live probe -- rather than inventing a
new account ledger.

Fails open. A read that cannot be trusted (cache miss and a fresh call
also fails) allows the intent through rather than blocking it: this
gate exists to skip WASTED work, and the worker's authoritative check
still runs regardless. Blocking on an unreadable precheck would turn an
optimisation into a second, less-tested gate of record.

A CACHED read may only clear a candidate, never block one
-----------------------------------------------------------
Observed in production: the cache held `available_usd=11.98` -- true at
the moment it was written, stale by the time it was read -- and blocked
IOT/UL/RELX/CLX/NVS, all comfortably affordable against the real KIS
orderable amount of $149.63. A cache is, by construction, a reading of
the PAST; treating its low figure as grounds to refuse an order is
exactly the mistake "not authoritative" above already warned against,
it was simply never enforced on the blocking path -- only the
allowing one.

So a cached figure that looks insufficient is no longer trusted on its
own: `check()` now performs exactly one fresh, authoritative
`get_account_cash_usd()` read before blocking, and blocks only if THAT
also shows insufficient funds. The fresh read is deliberately
fail-CLOSED here, unlike the module's general fail-open default above:
a low cached figure is already one reason for caution, and a refresh
that cannot confirm the account can afford the order does not get to
fall back to "let it through" on top of that. `tick_cache`, when a
caller supplies one, remembers that this tick already paid for one live
refresh so a second, third, ... candidate hitting the same low cached
figure in the same tick reuses it rather than calling KIS again.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from s6_live import account_cash_cache

logger = logging.getLogger(__name__)

INSUFFICIENT_CASH_PRECHECK = "INSUFFICIENT_CASH_PRECHECK"

OK = "OK"
BLOCKED = "BLOCKED"
UNAVAILABLE = "UNAVAILABLE"


def _fresh_read(broker, *, now, env) -> Optional[float]:
    try:
        available = broker.get_account_cash_usd()
    except Exception:  # noqa: BLE001 -- a failed read is UNAVAILABLE,
        # never a fabricated $0 (same principle as get_orderable_usd).
        logger.warning("S6 cash precheck: account cash read failed", exc_info=True)
        return None
    account_cash_cache.write(available, now=now, env=env)
    return available


def check(symbol, price, *, broker, now=None, env=None,
         max_age_seconds=account_cash_cache.DEFAULT_MAX_AGE_SECONDS,
         tick_cache: Optional[Dict[str, Any]] = None,
         ) -> Tuple[str, Dict[str, Any]]:
    """(status, detail). status is OK / BLOCKED / UNAVAILABLE.

    A reading that shows `available_usd < price` blocks only when it is
    AUTHORITATIVE -- a fresh `get_account_cash_usd()` read, whether that
    was the first read this call made or a one-time refresh triggered by
    a cached figure that looked insufficient (see module docstring). A
    cached figure alone may only clear a candidate, never block one.

    `tick_cache`, when given, is a plain dict the caller owns for the
    DURATION OF ONE TICK -- shared across every symbol `check()` is
    called for in that tick -- so that a low cached figure triggers at
    most one live KIS call no matter how many candidates hit it.

    UNAVAILABLE only for no broker/no price, or a cache miss whose OWN
    first fresh read fails -- the caller's policy there is to let the
    intent through. A failed refresh triggered BY a low cached figure is
    different: it blocks (see module docstring's fail-closed note).
    """
    moment = now or datetime.now(timezone.utc)
    detail: Dict[str, Any] = {"symbol": symbol, "price": price}
    if broker is None or price is None or price <= 0:
        detail["reason"] = "no broker or no price"
        return UNAVAILABLE, detail

    cached = account_cash_cache.read(now=moment, max_age_seconds=max_age_seconds, env=env)
    from_cache = cached is not None
    if from_cache:
        available = cached.get("available_usd")
        detail["cash_state_timestamp"] = cached.get("checked_at")
        detail["cash_source"] = "cached:" + str(cached.get("source"))
    else:
        available = _fresh_read(broker, now=moment, env=env)
        detail["cash_state_timestamp"] = moment.isoformat()
        detail["cash_source"] = "fresh:get_account_cash_usd"

    if available is None:
        detail["reason"] = "account cash unavailable"
        return UNAVAILABLE, detail

    if from_cache and available < price:
        if tick_cache is not None and "authoritative_available_usd" in tick_cache:
            refreshed = tick_cache["authoritative_available_usd"]
            detail["cash_source"] = "refreshed:tick_cache"
        else:
            refreshed = _fresh_read(broker, now=moment, env=env)
            if tick_cache is not None:
                tick_cache["authoritative_available_usd"] = refreshed
            detail["cash_source"] = "refreshed:get_account_cash_usd"
        detail["cash_state_timestamp"] = moment.isoformat()
        if refreshed is None:
            # Fail CLOSED here specifically: the cache already gave one
            # reason for caution about this candidate, and the one
            # authoritative check that could clear it did not answer.
            detail["reason"] = "authoritative refresh failed after a low cached read"
            detail["available_cash"] = available
            detail["required_for_1_share"] = price
            detail["shortfall"] = max(0.0, price - available)
            return BLOCKED, detail
        available = refreshed

    detail["available_cash"] = available
    detail["required_for_1_share"] = price
    if available < price:
        detail["shortfall"] = max(0.0, price - available)
        return BLOCKED, detail
    return OK, detail
