"""S6 qualification: the published breakout row, and nothing else.

Scope is the source's own question -- is this symbol one of this
session's S6 candidates for this variant, with a usable row behind it.
COMMON_STOCK, cash, reconciliation, the kill switch, position limits and
the day-range check belong to the shared BUY cycle and are not
duplicated: a second opinion about safety is two opinions, and they
diverge.
"""

from typing import Any, Dict, Optional

from config import s6_sessions
from s1_live.qualification import Qualification

S6_STRATEGY_ID = s6_sessions.STRATEGY_ID
S6_ENTRY_REASON = "s6_session_range_breakout"

REASON_NOT_AN_S6_CANDIDATE = "NOT_AN_S6_CANDIDATE"
REASON_UNUSABLE_CANDIDATE = "UNUSABLE_CANDIDATE_ROW"
REASON_WRONG_STRATEGY = "CANDIDATE_BELONGS_TO_ANOTHER_STRATEGY"
REASON_NO_RANGE = "CANDIDATE_HAS_NO_RANGE"

#: S6 does not rank or gate on a generic score at all -- its own ORB/
#: precision-watch conditions are the entire readiness decision (this
#: module ignores `score_threshold` for exactly that reason, above).  A
#: scanner-published row still carries the scanner's OWN ranking score
#: (scanner_base.py computes one for every signal), so that value is
#: used when present -- it is real information, just not a gate.  A row
#: discovered by the active-watch fast path never went through the
#: scanner's scoring pass at all (the fast-watch source's own row
#: builder sets score=None deliberately, having no rank to report), and
#: `domain.signal.Signal.score` is a required finite field shared by
#: every strategy -- there is no `Optional[float]` escape hatch for S6
#: alone without weakening that field for S1-S5 too, which the score
#: check does not otherwise require to be positive.  This sentinel fills
#: exactly that gap: a fixed, documented, explicitly NOT a quality
#: number, distinguishable at a glance from any real scanner score
#: (which score_weight_* combinations in scanners/orb/config.json
#: produce as 0-100).  Nothing downstream compares Signal.score against
#: a threshold for S6 (confirmed: no risk/sizing/execution path reads
#: it), so this cannot change which candidate qualifies or how it sizes
#: -- only whether the shared Signal dataclass can be constructed at
#: all for a source that never had a rank to give it.
S6_SCORE_NOT_APPLICABLE = 0.0


def _num(value) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number or number in (
        float("inf"), float("-inf")) else number


def qualify_s6(symbol, *, candidate_row: Optional[Dict[str, Any]]
               ) -> Qualification:
    if not candidate_row:
        return Qualification(
            False, symbol, reason_code=REASON_NOT_AN_S6_CANDIDATE,
            detail="symbol is not in this session's S6 candidate set")

    if str(candidate_row.get("strategy_id")) != S6_STRATEGY_ID:
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

    # A breakout candidate without its range is not interpretable: the
    # exit policy's primary signal is re-entry INTO that range, and a
    # position opened without one could never produce it.
    if _num(candidate_row.get("range_high")) is None:
        return Qualification(
            False, symbol, reason_code=REASON_NO_RANGE,
            detail="the row carries no range high; re-entry could not be "
                   "detected for a position opened from it")

    score = _num(candidate_row.get("score"))
    return Qualification(
        True, symbol,
        price=price, score=(score if score is not None else S6_SCORE_NOT_APPLICABLE),
        strategy_id=S6_STRATEGY_ID, entry_reason=S6_ENTRY_REASON,
        source_signal_id=signal_id,
        source_signal_timestamp=provenance.get("signal_timestamp"))
