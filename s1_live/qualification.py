"""What makes a candidate worth evaluating -- per source.

The split PHASE 4A §2 asks for:

    candidate strategy qualification   source-specific
    broker / execution safety          shared, exactly once

The legacy watchlist qualifies a symbol with
`paper_strategy_order.analyze_stock()` and `SCORE_THRESHOLD = 70`. That
is unchanged and stays unchanged: it is the strategy the legacy source
has always used, and this phase does not touch it.

S1 does NOT reuse it. `analyze_stock` is a different, older scoring
model with no relationship to the HMA/ADX conditions S1 was measured on
for a month. Requiring an S1 candidate to also clear it would mean the
thing that actually trades is "S1 AND legacy score", which is not what
month 1 recorded and not what any report describes. So the S1 source
qualifies from its own already-validated candidate row.

What stays shared, deliberately: the live price re-check, instrument
construction and tradability, the Order Gate, entry limits, idempotency,
the kill switch, reconciliation, and the Execution Engine. Qualification
answers "is this setup worth an order"; none of those do.

Both qualifications produce the SAME shape, because the pipeline
downstream reads exactly two things out of it -- a price and a score --
to build its `Signal`.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: The legacy strategy id the existing pipeline stamps on its signals.
LEGACY_STRATEGY_ID = "PAPER_STRATEGY_ORDER_SCORE_V1"
LEGACY_ENTRY_REASON = "score_threshold_breakout"

#: S1's own identity. A trade recorded under this id is traceable to the
#: scanner and the month of discovery data behind it.
S1_STRATEGY_ID = "S1_HMA_EARLY_TREND_V1"
S1_ENTRY_REASON = "s1_hma_early_trend_candidate"

REASON_BELOW_SCORE_THRESHOLD = "BELOW_SCORE_THRESHOLD"
REASON_NO_ANALYSIS = "NO_ANALYSIS"
REASON_NOT_AN_S1_CANDIDATE = "NOT_AN_S1_CANDIDATE"
REASON_UNUSABLE_CANDIDATE = "UNUSABLE_CANDIDATE_ROW"


@dataclass(frozen=True)
class Qualification:
    """The two facts the pipeline needs, plus who decided them."""

    qualified: bool
    symbol: str
    price: Optional[float] = None
    score: Optional[float] = None
    strategy_id: str = ""
    entry_reason: str = ""
    source_signal_id: Optional[str] = None
    source_signal_timestamp: Optional[str] = None
    reason_code: Optional[str] = None
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "qualified": self.qualified, "symbol": self.symbol,
            "price": self.price, "score": self.score,
            "strategy_id": self.strategy_id, "reason_code": self.reason_code,
        }


def _num(value) -> Optional[float]:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def qualify_legacy(symbol, *, analyze, score_threshold) -> Qualification:
    """`analyze_stock` + SCORE_THRESHOLD, exactly as the cycle did inline.

    `analyze` is passed in rather than imported so this function cannot
    re-import `paper_strategy_order` -- the module-identity hazard that
    broke eighteen tests in PHASE 3.
    """
    analysis = analyze(symbol)
    if analysis is None:
        return Qualification(False, symbol, reason_code=REASON_NO_ANALYSIS,
                             detail="analyze_stock returned nothing")
    if analysis["score"] < score_threshold:
        return Qualification(False, symbol, reason_code=REASON_BELOW_SCORE_THRESHOLD,
                             detail="did not meet score threshold")
    return Qualification(
        True, symbol, price=analysis["price"], score=analysis["score"],
        strategy_id=LEGACY_STRATEGY_ID, entry_reason=LEGACY_ENTRY_REASON)


def qualify_s1(symbol, *, candidate_row) -> Qualification:
    """From the already-validated S1 candidate row.

    No threshold is applied here at all. The row only exists because it
    survived `s1_live/store.py`'s full validation -- correct trading day,
    matching run id, intact payload hash, S1 as the source scanner -- and
    the scanner's own PASS decision is what put the symbol in the file.
    Re-judging it with a second score would be re-deciding a question
    month 1 froze.
    """
    if not candidate_row:
        return Qualification(False, symbol, reason_code=REASON_NOT_AN_S1_CANDIDATE,
                             detail="symbol is not in today's validated S1 candidate set")
    price = _num(candidate_row.get("signal_price"))
    score = _num(candidate_row.get("scanner_score"))
    signal_id = str(candidate_row.get("signal_id") or "").strip()
    if price is None or price <= 0 or not signal_id:
        return Qualification(False, symbol, reason_code=REASON_UNUSABLE_CANDIDATE,
                             detail="the candidate row lacks a usable price or signal id")
    return Qualification(
        True, symbol, price=price, score=score,
        strategy_id=S1_STRATEGY_ID, entry_reason=S1_ENTRY_REASON,
        source_signal_id=signal_id,
        source_signal_timestamp=candidate_row.get("signal_timestamp"))
