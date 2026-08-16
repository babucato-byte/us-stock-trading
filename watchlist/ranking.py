"""`manual_watch_score` -- a post-hoc ordering, never a decision.

The distinction this module has to keep, and the reason it is so plain:

    a scanner decides   PASS / REJECT      -- month 1 freezes this
    this decides        1st / 2nd / 3rd    -- of the ones that passed

Nothing here can promote a REJECT into the list or drop a PASS out of
it. The input is the set of signals the scanners already stored; the
output is the same set, ordered, annotated, and truncated for display.

Determinism
-----------
Ties break on `symbol`, ascending. Two runs over the same stored signals
must produce byte-identical files, or "yesterday's list versus today's"
becomes unreadable -- and a report nobody can diff is a report nobody
checks. Every float in the output is rounded at the point of
computation for the same reason.
"""

import math
from typing import Any, Dict, Iterable, List, Optional

from watchlist import config


def _finite(value) -> Optional[float]:
    """A usable float, or None. bool is excluded: `True` is not a price."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


#: Public alias: `builder` needs the same "is this a usable number"
#: rule, and two copies of that predicate is exactly how a bool starts
#: being treated as a price in one module and not the other.
finite = _finite


def _ramp(value: Optional[float], low: float, high: float) -> float:
    """0 at `low`, 100 at `high`, linear between, clamped outside.

    A missing value scores 0 rather than raising or being skipped: the
    component simply contributes nothing, which is the honest reading of
    "this was not measured" and keeps the total comparable across
    symbols whose scanners populate different fields.
    """
    if value is None or high <= low:
        return 0.0
    if value <= low:
        return 0.0
    if value >= high:
        return 100.0
    return (value - low) / (high - low) * 100.0


def group_by_symbol(rows: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        grouped.setdefault(symbol, []).append(row)
    return grouped


def overextension(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Has this name already run? For display and for a small penalty.

    Uses only fields the scanners already stored (track C-5): extension
    against HMA200/HMA89, the day's own change, and distance to the
    52-week high. Nothing is recomputed from bars -- this module never
    touches market data.

    The 52-week-high proximity is deliberately NOT sufficient on its
    own. A stock making a new high while sitting close to its moving
    averages is a breakout, which is the thing several of these scanners
    are built to find; calling that "overextended" would penalise
    exactly the setup the reader is looking for.
    """
    hma200 = max((_finite(row.get("extension_hma200_pct")) or -math.inf)
                 for row in rows) if rows else -math.inf
    hma89 = max((_finite(row.get("extension_hma89_pct")) or -math.inf)
                for row in rows) if rows else -math.inf
    change = max((_finite(row.get("price_change_pct")) or -math.inf)
                 for row in rows) if rows else -math.inf
    to_high = min((_finite(row.get("distance_52w_high")) or math.inf)
                  for row in rows) if rows else math.inf

    reasons = []
    if hma200 > -math.inf and hma200 >= config.OVEREXTENDED_HMA200_PCT:
        reasons.append(f"HMA200 +{hma200:.1f}%")
    if hma89 > -math.inf and hma89 >= config.OVEREXTENDED_HMA89_PCT:
        reasons.append(f"HMA89 +{hma89:.1f}%")
    if change > -math.inf and change >= config.OVEREXTENDED_DAY_CHANGE_PCT:
        reasons.append(f"당일 +{change:.1f}%")
    if reasons and to_high < math.inf and abs(to_high) <= config.NEAR_52W_HIGH_PCT:
        reasons.append("52주 고점 근접")

    return {
        "overextended": bool(reasons),
        "overextended_reasons": reasons,
        "extension_hma200_pct": round(hma200, 4) if hma200 > -math.inf else None,
        "extension_hma89_pct": round(hma89, 4) if hma89 > -math.inf else None,
        "price_change_pct": round(change, 4) if change > -math.inf else None,
        "distance_52w_high": round(to_high, 4) if to_high < math.inf else None,
    }


def score_symbol(symbol: str, daily_rows: List[Dict[str, Any]], *,
                 premarket_rows: Optional[List[Dict[str, Any]]] = None,
                 intraday_rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """One watchlist entry. Every component is reported alongside the
    total, so a reader can see WHY something ranked where it did without
    re-running anything."""
    premarket_rows = premarket_rows or []
    intraday_rows = intraday_rows or []

    daily_scanners = sorted({str(row.get("scanner_name")) for row in daily_rows
                             if row.get("scanner_name")})
    intersection = len([name for name in daily_scanners
                        if name in config.DAILY_SOURCE_SCANNERS])

    scores = [value for value in
              (_finite(row.get("scanner_score")) for row in daily_rows + premarket_rows)
              if value is not None]
    max_score = max(scores) if scores else None

    confirmed = bool(premarket_rows)
    present = set(daily_scanners)

    components = {
        "intersection": round(
            config.WEIGHT_INTERSECTION
            * _ramp(float(intersection), 0.0, float(len(config.DAILY_SOURCE_SCANNERS)))
            / 100.0, 4),
        "max_scanner_score": round(
            config.WEIGHT_MAX_SCORE * (max_score or 0.0) / 100.0, 4),
        "early_trend": round(
            config.WEIGHT_EARLY_TREND if "hma_early_trend" in present else 0.0, 4),
        "accumulation": round(
            config.WEIGHT_ACCUMULATION if "accumulation" in present else 0.0, 4),
        "breakout_ready": round(
            config.WEIGHT_BREAKOUT_READY if "breakout_ready" in present else 0.0, 4),
        "premarket_confirm": round(
            config.WEIGHT_PREMARKET_CONFIRM if confirmed else 0.0, 4),
    }

    extension = overextension(daily_rows + premarket_rows)
    penalty = config.PENALTY_OVEREXTENDED if extension["overextended"] else 0.0
    components["overextended_penalty"] = round(-penalty, 4)

    total = round(sum(components.values()), 4)

    reasons = []
    for row in daily_rows + premarket_rows:
        for reason in (row.get("reasons") or []) if isinstance(
                row.get("reasons"), list) else _split_reasons(row.get("reasons")):
            text = f"{row.get('scanner_name')}: {reason}"
            if text not in reasons:
                reasons.append(text)

    prices = [value for value in (_finite(row.get("signal_price"))
                                  for row in daily_rows + premarket_rows)
              if value is not None]

    return {
        "symbol": symbol,
        "manual_watch_score": total,
        "manual_watch_version": config.MANUAL_WATCH_VERSION,
        "components": components,
        "daily_scanners": daily_scanners,
        "intersection_count": intersection,
        "max_scanner_score": round(max_score, 4) if max_score is not None else None,
        "premarket_confirmed": confirmed,
        "intraday_observed": sorted({str(row.get("scanner_name"))
                                     for row in intraday_rows if row.get("scanner_name")}),
        "latest_signal_price": round(prices[-1], 4) if prices else None,
        "signal_count": len(daily_rows) + len(premarket_rows),
        "reasons": reasons[:8],
        **extension,
    }


def _split_reasons(value):
    """`joined_rows()` flattens `reasons` into a "; "-joined string; the
    raw signal rows keep it as a list. Accept both rather than requiring
    callers to know which shape they are holding."""
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [part.strip() for part in str(value).split(";") if part.strip()]


def rank(entries: Iterable[Dict[str, Any]], *, top_n: Optional[int] = None) -> List[Dict[str, Any]]:
    """Descending by score, then ascending by symbol. `rank` is 1-based.

    The symbol tiebreak is what makes this reproducible; without it two
    equally-scored names would order by whatever the store happened to
    yield, and yesterday's file would not diff against today's.
    """
    ordered = sorted(entries, key=lambda item: (-float(item.get("manual_watch_score") or 0.0),
                                                str(item.get("symbol"))))
    if top_n is not None:
        ordered = ordered[:max(0, int(top_n))]
    for position, entry in enumerate(ordered, start=1):
        entry["rank"] = position
    return ordered
