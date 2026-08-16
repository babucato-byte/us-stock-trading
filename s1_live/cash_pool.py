"""Establishing the S1 live cash pool from the broker, or refusing to.

The problem, stated plainly
---------------------------
KIS has no account-level orderable-cash endpoint. `domain/account_snapshot.py`
documents the live finding: TTTS3012R's `output2` returns nine
purchase/valuation/P&L fields and NO deposit at all, which is why
`usd_cash` and `usd_orderable_cash` are Optional and normally None. The
only orderable figure KIS gives is `get_orderable_usd(instrument, price)`
-- per symbol, per exchange, per limit price.

So "the account's available cash" is not a number this broker answers.

What this module does about it
------------------------------
It never invents one. `establish()` returns a `CashPool` whose `status`
is one of:

    AVAILABLE     a usable figure, with `source` naming where it came from
    UNAVAILABLE   no figure could be established -- `amount_usd` is None

UNAVAILABLE is not zero. A zero pool would allocate zero shares and look
like a poor day; an UNAVAILABLE pool blocks and says the account could
not be read. Those need different operator responses, and conflating
them is the exact defect ORACLE-CASH-01 recorded.

Where a figure can come from, in order of preference:

    1. `usd_orderable_cash` on the account snapshot, if the response
       carried it. Account-level and therefore the right shape.
    2. A per-symbol probe: `get_orderable_usd()` for the highest-ranked
       candidate at its own price. Marked `PROBE` in `source`, because it
       is an approximation -- KIS answers per symbol, and another symbol
       could answer differently.

Option 2 is offered because for a cash (non-margin) overseas account the
per-symbol answer is dominated by available deposit, so it is a
reasonable proxy. It is labelled, not laundered: every plan built on it
carries `source="probe:<symbol>"` so a reader knows the pool was
inferred rather than reported.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from domain.cash_sizing import is_usable_amount

logger = logging.getLogger(__name__)

AVAILABLE = "AVAILABLE"
UNAVAILABLE = "UNAVAILABLE"

SOURCE_SNAPSHOT = "account_snapshot.usd_orderable_cash"
REASON_NO_ACCOUNT_FIGURE = "ACCOUNT_ORDERABLE_CASH_UNAVAILABLE"
REASON_PROBE_FAILED = "ORDERABLE_PROBE_FAILED"
REASON_NO_CANDIDATE = "NO_CANDIDATE_TO_PROBE"


@dataclass(frozen=True)
class CashPool:
    status: str
    amount_usd: Optional[float]
    source: str
    as_of: datetime
    reason_code: Optional[str] = None
    detail: str = ""

    @property
    def available(self) -> bool:
        return self.status == AVAILABLE and self.amount_usd is not None

    def require(self) -> float:
        if not self.available:
            raise CashPoolUnavailable(
                f"the S1 cash pool could not be established: {self.detail}",
                reason_code=self.reason_code or REASON_NO_ACCOUNT_FIGURE)
        return float(self.amount_usd)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status, "amount_usd": self.amount_usd,
            "source": self.source, "as_of": self.as_of.isoformat(),
            "reason_code": self.reason_code, "detail": self.detail,
        }


class CashPoolUnavailable(Exception):
    """No cash figure could be established. A hard block on new entries."""

    def __init__(self, message, *, reason_code=REASON_NO_ACCOUNT_FIGURE):
        super().__init__(message)
        self.reason_code = reason_code


def from_amount(amount_usd, *, source="caller", now=None) -> CashPool:
    """Wrap a figure the caller already has -- used by the dry run."""
    stamp = now or datetime.now(timezone.utc)
    if not is_usable_amount(amount_usd):
        return CashPool(UNAVAILABLE, None, source, stamp,
                        reason_code=REASON_NO_ACCOUNT_FIGURE,
                        detail=f"{amount_usd!r} is not a usable cash amount")
    return CashPool(AVAILABLE, float(amount_usd), source, stamp)


def establish(*, account_snapshot=None, broker=None, probe_instrument=None,
              probe_price_usd=None, now=None) -> CashPool:
    """The pool, or an explicit refusal. Never raises for an unusable read."""
    stamp = now or datetime.now(timezone.utc)

    reported = getattr(account_snapshot, "usd_orderable_cash", None)
    if is_usable_amount(reported):
        return CashPool(AVAILABLE, float(reported), SOURCE_SNAPSHOT, stamp)

    cash_source = getattr(account_snapshot, "cash_source", "") or "unreported"

    if broker is None or probe_instrument is None or not is_usable_amount(probe_price_usd):
        return CashPool(
            UNAVAILABLE, None, "none", stamp, reason_code=REASON_NO_CANDIDATE,
            detail=f"the account response carried no orderable cash ({cash_source}) "
                   "and no candidate was available to probe with")

    symbol = getattr(probe_instrument, "symbol", None) or str(probe_instrument)
    try:
        probed = broker.get_orderable_usd(probe_instrument, float(probe_price_usd))
    except Exception as exc:  # noqa: BLE001 - any failure is "unknown", not zero
        return CashPool(
            UNAVAILABLE, None, f"probe:{symbol}", stamp,
            reason_code=REASON_PROBE_FAILED,
            detail=f"the orderable-amount probe for {symbol} failed: "
                   f"{type(exc).__name__}")

    if not is_usable_amount(probed):
        return CashPool(
            UNAVAILABLE, None, f"probe:{symbol}", stamp,
            reason_code=REASON_PROBE_FAILED,
            detail=f"the orderable-amount probe for {symbol} returned {probed!r}")

    logger.info("S1 cash pool established by probe on %s: $%.2f", symbol, float(probed))
    return CashPool(AVAILABLE, float(probed), f"probe:{symbol}", stamp,
                    detail="inferred from a per-symbol orderable read; KIS reports "
                           "no account-level orderable cash")
