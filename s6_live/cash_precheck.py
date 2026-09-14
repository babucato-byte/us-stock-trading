"""READY -> BUY_INTENT affordability precheck using KIS buying power.

KIS exposes two different USD amounts with different meanings:

* ``get_account_cash_usd()`` reads settled/withdrawable USD cash from
  ``inquire-present-balance``.
* ``get_orderable_usd(instrument, price)`` reads KIS's final overseas
  orderable amount from ``inquire-psamount``.

Only the second amount can decide whether a BUY is affordable. Reusable
sale proceeds are already incorporated by KIS into
``output.ovrs_ord_psbl_amt``. This module must neither substitute the
settled-cash figure nor reconstruct buying power from response components.

The read is candidate-specific because KIS requires the instrument and the
price being tested. A failed exchange resolution or orderability read is a
fail-closed BLOCKED result; an unknown balance is never treated as zero.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from domain.cash_sizing import is_usable_amount

logger = logging.getLogger(__name__)

INSUFFICIENT_CASH_PRECHECK = "INSUFFICIENT_CASH_PRECHECK"
ORDERABLE_CASH_UNAVAILABLE_PRECHECK = "ORDERABLE_CASH_UNAVAILABLE_PRECHECK"

OK = "OK"
BLOCKED = "BLOCKED"
UNAVAILABLE = "UNAVAILABLE"


def check(symbol, price, *, broker, now=None, env=None) -> Tuple[str, Dict[str, Any]]:
    """Return ``(status, detail)`` for one-share BUY affordability.

    ``env`` remains in the signature for the existing fast-watch caller; cash
    is no longer read from or written to its account-level cache.
    """
    del env
    moment = now or datetime.now(timezone.utc)
    detail: Dict[str, Any] = {
        "symbol": symbol,
        "price": price,
        "cash_state_timestamp": moment.isoformat(),
        "cash_source": "fresh:get_orderable_usd",
    }
    if broker is None or price is None or price <= 0:
        detail["reason"] = "no broker or no price"
        return UNAVAILABLE, detail

    try:
        from market_data.exchange_registry import build_kis_instrument

        instrument, record = build_kis_instrument(symbol)
        detail["exchange"] = record.exchange.value
        available = broker.get_orderable_usd(instrument, price)
    except Exception as exc:  # noqa: BLE001 -- an unknown buying-power
        # answer cannot authorize a BUY. Keep this distinct from a genuine
        # numeric balance that is too small.
        logger.warning("S6 cash precheck: KIS orderability read failed", exc_info=True)
        detail["reason"] = "authoritative KIS orderability read failed"
        detail["reason_code"] = ORDERABLE_CASH_UNAVAILABLE_PRECHECK
        detail["error_type"] = type(exc).__name__
        return BLOCKED, detail

    if not is_usable_amount(available):
        detail["reason"] = "authoritative KIS orderability amount is unusable"
        detail["reason_code"] = ORDERABLE_CASH_UNAVAILABLE_PRECHECK
        return BLOCKED, detail

    available = float(available)
    detail["available_cash"] = available
    detail["required_for_1_share"] = price
    if available < price:
        detail["shortfall"] = max(0.0, price - available)
        return BLOCKED, detail
    return OK, detail
