"""Two populations of candidate, kept apart.

The problem this separates
--------------------------
Every symbol a scanner passes is worth OBSERVING. Only some are worth
showing an operator as something they could act on. On 2026-08-21 S6's
only candidate was IEFA -- an ETP -- so the channel read "후보 수: 1"
while the number of symbols that could actually be bought was zero. The
BUY gate would have refused it correctly and silently, which is the worst
combination: correct behaviour that looks like an opportunity.

So a candidate carries two facts now:

    OBSERVED_CANDIDATE       it passed the scanner
    LIVE_ELIGIBLE_CANDIDATE  and KIS classifies it COMMON_STOCK

ETPs, indices and warrants stay in the dataset -- they are the research
population and dropping them would bias every study toward the
instruments that happen to be tradeable. They are excluded from the
LIVE ranking only.

Unknown fails closed
--------------------
A symbol the KIS master cannot classify is NOT live-eligible. The
classification is what stands between a research candidate and a real
order, and "we could not tell" is not a reason to treat something as
ordinary stock. It is still observed, and its type is recorded as
UNKNOWN so the gap is visible rather than absent.

Nothing here decides an order
-----------------------------
This is a labelling and display concern. `require_live_eligible()` in
the shared BUY cycle remains the thing that actually refuses an order,
and it is not weakened, bypassed or duplicated: a second opinion about
what may be traded is two opinions, and they diverge.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

OBSERVED = "OBSERVED_CANDIDATE"
LIVE_ELIGIBLE = "LIVE_ELIGIBLE_CANDIDATE"

UNKNOWN_TYPE = "UNKNOWN"

#: Types that may be shown as actionable. Deliberately the same set the
#: BUY gate enforces -- read from it rather than restated, so the two
#: cannot drift into disagreeing about what is tradeable.
def live_eligible_types():
    try:
        from s1_live import security_type

        return frozenset(security_type.LIVE_ELIGIBLE_TYPES)
    except Exception:  # noqa: BLE001
        return frozenset({"COMMON_STOCK"})


def classify_symbol(symbol, *, index=None) -> Dict[str, Any]:
    """`security_type` and `live_eligible` for one symbol.

    Never raises. A lookup failure is UNKNOWN and not live-eligible --
    the same direction the BUY gate fails in, so a classification outage
    narrows what is shown as actionable rather than widening it.
    """
    try:
        from s1_live import security_type

        source = index if index is not None else security_type.load_index()
        classification = source.classify(symbol)
        return {
            "security_type": classification.security_type,
            "etp_type": getattr(classification, "etp_type", None),
            "exchange": getattr(classification, "exchange", None),
            "live_eligible": bool(classification.live_eligible),
            "classified_at": getattr(classification, "asof", None),
        }
    except Exception as exc:  # noqa: BLE001
        logger.info("could not classify %s: %s", symbol, str(exc)[:120])
        return {"security_type": UNKNOWN_TYPE, "etp_type": None,
                "exchange": None, "live_eligible": False,
                "classified_at": None,
                "classification_error": str(exc)[:200]}


def _num(value) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number or number in (
        float("inf"), float("-inf")) else number


def derived_metrics(row: Dict[str, Any]) -> Dict[str, Any]:
    """Measures that make two candidates comparable.

    `breakout_pct` alone is not comparable across instruments: a 0.06%
    move out of a 0.12%-wide range is a clean break, and the same 0.06%
    out of a 2%-wide range is noise. IEFA scored full entry-proximity
    marks on exactly that ambiguity, so the range-normalised figure is
    computed alongside it -- for SHADOW comparison, not to replace the
    live metric while the sample is one row.
    """
    price = _num(row.get("price"))
    high = _num(row.get("range_high"))
    low = _num(row.get("range_low"))
    vwap = _num(row.get("vwap"))
    ema9 = _num(row.get("ema9"))
    ema21 = _num(row.get("ema21"))

    width_pct = None
    if high is not None and low is not None and low > 0:
        width_pct = (high / low - 1.0) * 100.0

    breakout_pct = None
    if price is not None and high is not None and high > 0:
        breakout_pct = (price / high - 1.0) * 100.0

    normalised = None
    if breakout_pct is not None and width_pct not in (None, 0):
        # How far past the range, measured in RANGE WIDTHS. 1.0 means the
        # move since the break equals the range that produced it.
        normalised = breakout_pct / width_pct

    vwap_distance = None
    if price is not None and vwap not in (None, 0):
        vwap_distance = (price / vwap - 1.0) * 100.0

    ema_spread = None
    if ema9 is not None and ema21 not in (None, 0):
        ema_spread = (ema9 / ema21 - 1.0) * 100.0

    return {
        "opening_range_width_pct": width_pct,
        "breakout_pct": breakout_pct,
        "normalized_breakout_by_range": normalised,
        "vwap_distance_pct": vwap_distance,
        "ema_spread_pct": ema_spread,
    }


def enrich(rows: List[Dict[str, Any]], *, index=None) -> List[Dict[str, Any]]:
    """Add classification and derived metrics to published rows."""
    enriched = []
    for row in rows or []:
        merged = dict(row)
        merged.update(classify_symbol(row.get("symbol"), index=index))
        merged.update(derived_metrics(row))
        merged["candidate_class"] = (
            LIVE_ELIGIBLE if merged.get("live_eligible") else OBSERVED)
        enriched.append(merged)
    return enriched


def split(rows: List[Dict[str, Any]]):
    """(observed, live_eligible). Observed is EVERYTHING, not the remainder.

    A study of "what the scanner found" that silently excluded the
    untradeable half would be a study of the tradeable half wearing the
    wrong name.
    """
    observed = list(rows or [])
    live = [r for r in observed if r.get("live_eligible")]
    return observed, live


def top_live(rows: List[Dict[str, Any]], limit: int = 3):
    """The ranking an operator may act on: COMMON_STOCK only.

    Re-ranked within the eligible set rather than filtered after ranking,
    so "1위" means first among the ones that could be bought instead of
    whatever survived a cut.
    """
    _observed, live = split(rows)
    ordered = sorted(live, key=lambda r: (-(_num(r.get("score")) or 0.0),
                                          str(r.get("symbol") or "")))
    return ordered[:max(0, int(limit))]
