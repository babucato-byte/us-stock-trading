"""Compares this codebase's own durable reservation/exposure tracking
against KIS's own reported account state (spec §16). A mismatch here
means the internal ledger's belief about how much USD is committed no
longer agrees with what KIS itself reports as orderable -- treated as a
hard block on new entries, same as position/order mismatches.
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class AccountMismatch:
    field: str
    internal_value: float
    kis_value: float
    difference: float
    reason: str


def reconcile_account(
    *, internal_reserved_usd: float, kis_usd_orderable_cash: float, kis_usd_cash: float,
    tolerance_usd: float = 1.0,
) -> list:
    """Returns an empty list if `kis_usd_cash - kis_usd_orderable_cash`
    (what KIS itself believes is committed to open orders) is within
    `tolerance_usd` of `internal_reserved_usd` (this codebase's own
    durable count of the same thing). A drift beyond tolerance means
    either an order KIS knows about that this codebase's idempotency
    ledger doesn't (or vice versa) -- exactly the class of bug spec §16
    exists to catch."""
    mismatches = []
    for name, value in (("internal_reserved_usd", internal_reserved_usd),
                        ("kis_usd_orderable_cash", kis_usd_orderable_cash),
                        ("kis_usd_cash", kis_usd_cash)):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            mismatches.append(AccountMismatch(
                field=name, internal_value=float("nan"), kis_value=float("nan"), difference=float("nan"),
                reason=f"{name} is not a finite number: {value!r}",
            ))
    if mismatches:
        return mismatches

    kis_reserved = kis_usd_cash - kis_usd_orderable_cash
    difference = abs(kis_reserved - internal_reserved_usd)
    if difference > tolerance_usd:
        mismatches.append(AccountMismatch(
            field="reserved_usd", internal_value=internal_reserved_usd, kis_value=kis_reserved,
            difference=difference,
            reason=(
                f"internal reserved-in-open-orders (${internal_reserved_usd:.2f}) diverges from "
                f"KIS-implied reserved (${kis_reserved:.2f}) by ${difference:.2f}, exceeding "
                f"${tolerance_usd:.2f} tolerance"
            ),
        ))
    return mismatches
