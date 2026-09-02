"""TCN-02A: what a strategy's own position row says about itself.

A value object, nothing more. Built by an exit runtime from the row it
is about to sell and handed to the broker adapter, which combines it
with its own reads into `execution.sell_safe_evidence.SellSafeEvidence`.
It lives in `domain/` because the strategy runtimes (`s1_live`,
`s2_live`, `s6_live`) are forbidden from importing `execution` -- they
decide, they do not submit -- and this is a fact about a position, not
an execution decision.
"""

from dataclasses import dataclass
from typing import Optional

#: Statuses a strategy position store uses for "shares are held and the
#: row was opened from a fill". Every per-strategy store (S1, S2, S6)
#: uses these two words; EXIT_SUBMITTED is deliberately absent.
FILL_BACKED_STATUSES = frozenset({"OPEN", "EXIT_PENDING"})


def _int_or_none(value) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _finite_positive(value) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number == number and number not in (float("inf"), float("-inf")) \
        and number > 0


@dataclass(frozen=True)
class LocalPositionEvidence:
    """What the strategy's own position row says. Built by the exit
    runtime from the row it is about to sell, never from a broker."""

    position_id: Optional[str]
    status: Optional[str]
    remaining_quantity: Optional[int]
    entry_price: Optional[float]
    exit_submitted: bool

    @classmethod
    def from_row(cls, row, *, position_id=None) -> "LocalPositionEvidence":
        get = row.get if hasattr(row, "get") else (lambda k, d=None: d)
        return cls(
            position_id=position_id or get("position_id"),
            status=(str(get("status")).upper() if get("status") is not None else None),
            remaining_quantity=_int_or_none(get("quantity")),
            entry_price=(float(get("entry_price"))
                         if _finite_positive(get("entry_price")) else None),
            exit_submitted=bool(get("exit_submitted")),
        )

    @property
    def fill_backed(self) -> bool:
        """A real entry price and a real quantity in a held status. Every
        per-strategy store sets `entry_price` only from an actual fill
        (S6 refuses to open without one), so this is what "opened from a
        fill" looks like from the outside."""
        return (self.status in FILL_BACKED_STATUSES
                and _finite_positive(self.entry_price)
                and (self.remaining_quantity or 0) >= 1)
