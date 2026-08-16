"""Compares internal position records against KIS's own reported
positions (spec §16). Pure function -- both sides are passed in already
fetched. A mismatch is reported, never auto-corrected: the caller
(execution_engine.py's gate-context builders) is expected to treat any
non-empty mismatch list as `reconciliation_ok=False`.
"""

from dataclasses import dataclass
from typing import Dict, List

from domain.position import Position


@dataclass(frozen=True)
class PositionMismatch:
    symbol: str
    internal_quantity: int
    kis_quantity: int
    reason: str


def reconcile_positions(internal_positions: List[Position], kis_positions: List[Position]) -> List[PositionMismatch]:
    """Returns an empty list if every symbol's quantity matches exactly.
    A symbol present in KIS but not internally, or vice versa, is also a
    mismatch (treated as internal_quantity/kis_quantity=0 for the
    missing side) -- spec §16 explicitly forbids auto-selling a KIS
    position the internal system doesn't know about, which starts with
    detecting that it exists at all."""
    internal_by_symbol: Dict[str, int] = {p.symbol: p.quantity for p in internal_positions}
    kis_by_symbol: Dict[str, int] = {p.symbol: p.quantity for p in kis_positions}
    all_symbols = set(internal_by_symbol) | set(kis_by_symbol)
    mismatches = []
    for symbol in sorted(all_symbols):
        internal_qty = internal_by_symbol.get(symbol, 0)
        kis_qty = kis_by_symbol.get(symbol, 0)
        if internal_qty != kis_qty:
            if symbol not in internal_by_symbol:
                reason = "position exists at KIS but not tracked internally"
            elif symbol not in kis_by_symbol:
                reason = "position tracked internally but does not exist at KIS"
            else:
                reason = "quantity mismatch"
            mismatches.append(PositionMismatch(
                symbol=symbol, internal_quantity=internal_qty, kis_quantity=kis_qty, reason=reason,
            ))
    return mismatches
