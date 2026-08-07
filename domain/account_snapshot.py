"""`AccountSnapshot` -- the KIS-authoritative account facts Risk/Sizing
read (spec §14/§18). Distinct from `live_readiness/account_engine.
AccountSnapshot` (the existing Alpaca-KRW-percent pilot model, kept
unchanged for that still-supported path) -- this one separates KRW cash
from USD cash and tracks USD reserved-in-open-orders explicitly, since
KIS's overseas order flow settles and reserves in USD, not KRW.

"Unknown" is not zero
---------------------
Every cash field is Optional, and None means "this source did not report
it" -- never "$0". The distinction is the whole point of this type:

    ORACLE-CASH-01. `get_account_snapshot()` read `frcr_dncl_amt1` and
    `frcr_use_psbl_amt` out of the balance response (TTTS3012R) with a
    `.get(field, 0) or 0` fallback. A live read on the Oracle host showed
    that TTTS3012R's `output2` carries NEITHER field -- it returns nine
    purchase/valuation/P&L fields and no deposit at all. The fallback
    turned "this endpoint does not report cash" into a confident $0, so a
    funded account sized every candidate to zero shares and the entry
    path blocked at INSUFFICIENT_CASH before any other gate ran.

A caller that needs a number must therefore say so explicitly, via
`require_usd_available_for_new_order()`, and handle the raise. Reading
the attribute and doing arithmetic on it gets None, not a silent zero.
"""

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

CASH_AVAILABLE = "AVAILABLE"
CASH_UNAVAILABLE = "UNAVAILABLE"

# Why a source could not supply the USD cash figures. Named here so the
# broker states it once and preflight/audit quote the same string.
CASH_SOURCE_BALANCE_LACKS_FIELDS = "TTTS3012R_DOES_NOT_PROVIDE"


class AccountSnapshotError(Exception):
    """Raised when an AccountSnapshot cannot be safely constructed."""


class AccountCashUnavailableError(Exception):
    """Raised when a caller demands a cash figure this snapshot's source
    never reported. Deliberately NOT a "zero cash" outcome."""

    def __init__(self, message, *, cash_source=""):
        super().__init__(message)
        self.reason_code = "ORDERABLE_CASH_UNAVAILABLE"
        self.cash_source = cash_source


def _is_usable_cash(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


@dataclass(frozen=True)
class AccountSnapshot:
    krw_cash: Optional[float]
    usd_cash: Optional[float]
    usd_orderable_cash: Optional[float]
    usd_reserved_in_open_orders: float
    as_of: datetime
    source: str
    account_id: str
    # Where the cash figures came from, or why they are absent. Required
    # whenever a cash field is None, so "unknown" always carries its
    # reason instead of being an unexplained blank.
    cash_source: str = ""

    def __post_init__(self):
        for name in ("krw_cash", "usd_cash", "usd_orderable_cash"):
            value = getattr(self, name)
            if value is None:
                continue
            if not _is_usable_cash(value):
                raise AccountSnapshotError(
                    f"{name} must be None or a non-negative finite number, got {value!r}")
        if not _is_usable_cash(self.usd_reserved_in_open_orders):
            raise AccountSnapshotError(
                "usd_reserved_in_open_orders must be a non-negative finite number, "
                f"got {self.usd_reserved_in_open_orders!r}")
        if not isinstance(self.as_of, datetime) or self.as_of.tzinfo is None:
            raise AccountSnapshotError("as_of must be a timezone-aware datetime")
        for name in ("source", "account_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise AccountSnapshotError(f"{name} must be a non-empty string, got {value!r}")
        if not isinstance(self.cash_source, str):
            raise AccountSnapshotError(f"cash_source must be a string, got {self.cash_source!r}")
        # An absent figure without a stated reason is how "unknown"
        # degrades back into an unexplained blank a reader guesses at.
        if any(getattr(self, name) is None
               for name in ("krw_cash", "usd_cash", "usd_orderable_cash")) \
                and not self.cash_source.strip():
            raise AccountSnapshotError(
                "cash_source must say why a cash field is unavailable")

    @property
    def cash_status(self):
        """Derived, never stored: a second stored flag is how a status and
        the values it describes drift apart."""
        return CASH_AVAILABLE if self.usd_orderable_cash is not None else CASH_UNAVAILABLE

    @property
    def usd_available_for_new_order(self):
        """None when KIS's orderable figure is unknown.

        When it IS known: KIS's own `usd_orderable_cash` is already net of
        what KIS itself considers reserved -- `usd_reserved_in_open_orders`
        here is this codebase's OWN durable ledger figure (spec §17's
        idempotency/duplicate tracking), subtracted again as a second,
        independent floor so a KIS-side accounting lag never lets two
        systems' views of "available cash" silently diverge upward.
        """
        if self.usd_orderable_cash is None:
            return None
        return max(0.0, self.usd_orderable_cash - self.usd_reserved_in_open_orders)

    def require_usd_available_for_new_order(self):
        """The figure, or a raise. For callers that must not proceed on an
        unknown balance -- which is every sizing caller."""
        value = self.usd_available_for_new_order
        if value is None:
            raise AccountCashUnavailableError(
                f"{self.source} did not report USD orderable cash "
                f"({self.cash_source or 'no reason recorded'})",
                cash_source=self.cash_source,
            )
        return value

    def is_stale(self, *, max_age_seconds, now=None):
        from datetime import timezone
        current = now or datetime.now(timezone.utc)
        age_seconds = (current - self.as_of).total_seconds()
        return age_seconds < 0 or age_seconds > max_age_seconds
