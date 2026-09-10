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
         max_age_seconds=account_cash_cache.DEFAULT_MAX_AGE_SECONDS
         ) -> Tuple[str, Dict[str, Any]]:
    """(status, detail). status is OK / BLOCKED / UNAVAILABLE.

    BLOCKED only when a reading -- cached or freshly read -- actually
    shows `available_usd < price`. Any other outcome (no broker, no
    price, a read that failed) is UNAVAILABLE, and the caller's policy
    is to let the intent through (see module docstring).
    """
    moment = now or datetime.now(timezone.utc)
    detail: Dict[str, Any] = {"symbol": symbol, "price": price}
    if broker is None or price is None or price <= 0:
        detail["reason"] = "no broker or no price"
        return UNAVAILABLE, detail

    cached = account_cash_cache.read(now=moment, max_age_seconds=max_age_seconds, env=env)
    if cached is not None:
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

    detail["available_cash"] = available
    detail["required_for_1_share"] = price
    if available < price:
        detail["shortfall"] = max(0.0, price - available)
        return BLOCKED, detail
    return OK, detail
