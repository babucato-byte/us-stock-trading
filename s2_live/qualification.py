"""S2 qualification: the published candidate row, and nothing else.

Scope
-----
This answers only the question the candidate SOURCE owns: is this symbol
one of today's S2 candidates for this session, with a usable row behind
it. Nothing here re-checks COMMON_STOCK, orderable cash, reconciliation,
the kill switch, the position limits or the day-range price -- those are
the shared BUY cycle's gates and they run identically whatever answered
this. Re-implementing one here would be a second opinion about safety,
and two opinions diverge.

No second score is applied
--------------------------
`analyze` and `score_threshold` are accepted so the cycle can call every
source the same way, and are deliberately ignored -- exactly as S1 does.
Requiring an S2 candidate to also clear the legacy scoring model would
mean the thing that actually trades is "S2 AND legacy score", which is
not the strategy being measured and not what any report describes.

The row already survived validation: it was published only because the
scanner's own PASS put it there, for this trading day and this session,
and the source re-checked both before offering the symbol.
"""

from typing import Any, Dict, Optional

from s1_live.qualification import Qualification

S2_STRATEGY_ID = "S2_VOLUME_ACCUMULATION_V1"
S2_ENTRY_REASON = "s2_volume_accumulation_candidate"

REASON_NOT_AN_S2_CANDIDATE = "NOT_AN_S2_CANDIDATE"
REASON_UNUSABLE_CANDIDATE = "UNUSABLE_CANDIDATE_ROW"
REASON_WRONG_STRATEGY = "CANDIDATE_BELONGS_TO_ANOTHER_STRATEGY"


def _num(value) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number or number in (
        float("inf"), float("-inf")) else number


def qualify_s2(symbol, *, candidate_row: Optional[Dict[str, Any]]
               ) -> Qualification:
    """From the already-validated S2 candidate row."""
    if not candidate_row:
        return Qualification(
            False, symbol, reason_code=REASON_NOT_AN_S2_CANDIDATE,
            detail="symbol is not in this session's S2 candidate set")

    if str(candidate_row.get("strategy_id")) != S2_STRATEGY_ID:
        # The source filters by strategy already; this catches a row
        # arriving by some other route rather than trusting that it
        # cannot happen.
        return Qualification(
            False, symbol, reason_code=REASON_WRONG_STRATEGY,
            detail=f"row belongs to {candidate_row.get('strategy_id')!r}")

    price = _num(candidate_row.get("price"))
    provenance = candidate_row.get("provenance") or {}
    signal_id = str(provenance.get("signal_id") or "").strip()
    if price is None or price <= 0 or not signal_id:
        return Qualification(
            False, symbol, reason_code=REASON_UNUSABLE_CANDIDATE,
            detail="the candidate row lacks a usable price or signal id")

    return Qualification(
        True, symbol, price=price, score=_num(candidate_row.get("score")),
        strategy_id=S2_STRATEGY_ID, entry_reason=S2_ENTRY_REASON,
        source_signal_id=signal_id,
        source_signal_timestamp=provenance.get("signal_timestamp"))
