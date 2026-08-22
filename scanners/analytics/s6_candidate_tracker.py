"""What happened after an S6 candidate fired.

Why this is separate from the exit policy
-----------------------------------------
The exit policy decides. This measures, and it measures candidates
NOBODY BOUGHT as well as ones that were. That is the comparison the
month-1 review needs: "the ones we took did better than the ones we
skipped" is only a finding if both sides went through the same
arithmetic, and a study of only the live half is a study of survivors.

Nothing computed here feeds back into a scanner condition or a score.
A measurement that changes the thing it measures has stopped being one.

False breakout is a LABEL, not a rule
-------------------------------------
`range_reentry_*` records that price returned inside the range the
candidate broke out of. It is deliberately not wired to anything: §2
asks for the research label, and S6_EXIT_V0's own re-entry rule is a
separate decision made on live positions with live prices. Naming the
same phenomenon twice does not make it one mechanism.

Absent stays absent
-------------------
Every horizon that cannot be measured is None -- never 0, never the last
known price carried forward. A candidate 20 minutes old genuinely has no
+1h return, and filling it with anything would put a fabricated number
into an average that a threshold decision later rests on.
"""

import logging
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: Minutes after the candidate at which price is sampled.
HORIZONS = (5, 15, 30, 60)

#: Horizons at which the research labels are evaluated.
REENTRY_HORIZONS = (5, 15, 30, 60)
VWAP_FAILURE_HORIZONS = (5, 15, 30)

#: Populations reported separately. Merging them would let the
#: instruments that happen to be tradeable be judged by the ones that
#: are not, and vice versa.
POP_ALL = "ALL_OBSERVED"
POP_LIVE = "LIVE_ELIGIBLE_COMMON_STOCK"
POP_ETP = "ETP_ETF"
POP_UNKNOWN = "UNKNOWN"


def _num(value) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number or number in (
        float("inf"), float("-inf")) else number


def _minutes(start, end) -> Optional[float]:
    if start is None or end is None:
        return None
    try:
        delta = (end - start).total_seconds() / 60.0
    except Exception:  # noqa: BLE001
        return None
    return delta if delta >= 0 else None


def _bars_after(bars: Sequence[Dict[str, Any]], start):
    """Bars at or after `start`, oldest first."""
    if start is None:
        return list(bars or [])
    out = []
    for bar in bars or []:
        stamp = bar.get("timestamp")
        if stamp is None:
            continue
        try:
            if stamp >= start:
                out.append(bar)
        except TypeError:
            continue
    return out


def _price_at(bars, start, minutes) -> Optional[float]:
    """Close of the last bar within `minutes` of `start`.

    None when the window has not elapsed. Deliberately not the last
    known price: a candidate 20 minutes old has no +1h return, and
    carrying one forward would report the present as the future.
    """
    if not bars or start is None:
        return None
    latest = None
    reached = False
    for bar in bars:
        elapsed = _minutes(start, bar.get("timestamp"))
        if elapsed is None:
            continue
        if elapsed <= minutes:
            close = _num(bar.get("close"))
            if close is not None:
                latest = close
        if elapsed >= minutes:
            reached = True
            break
    return latest if reached else None


def _pct(base, value) -> Optional[float]:
    base, value = _num(base), _num(value)
    if base is None or value is None or base <= 0:
        return None
    return (value / base - 1.0) * 100.0


def follow(candidate: Dict[str, Any], bars: Sequence[Dict[str, Any]], *,
           candidate_time=None, session_close_price=None) -> Dict[str, Any]:
    """Everything §1-§3 asks for, for one candidate.

    `bars` are the candidate's own minute bars from the candidate time
    onward: dicts with `timestamp`, `high`, `low`, `close` and optionally
    `vwap`.
    """
    price = _num(candidate.get("price"))
    range_high = _num(candidate.get("range_high"))
    start = candidate_time or candidate.get("candidate_time")
    forward = _bars_after(bars, start)

    row: Dict[str, Any] = {
        "symbol": candidate.get("symbol"),
        "variant": candidate.get("variant"),
        "session": candidate.get("session"),
        "trading_day": candidate.get("trading_day"),
        "candidate_time": start.isoformat() if hasattr(start, "isoformat") else start,
        "candidate_price": price,
        "bars_seen": len(forward),
    }

    # -- quality features, carried through unchanged -------------------
    for field in ("security_type", "live_eligible", "score",
                  "volume_expansion", "daily_relative_volume",
                  "absolute_volume", "dollar_volume", "breakout_pct",
                  "opening_range_width_pct", "normalized_breakout_by_range",
                  "vwap_distance_pct", "ema_spread_pct", "range_high",
                  "range_low", "range_minutes"):
        row[field] = candidate.get(field)
    for field in ("score_breakout_quality", "score_volume_expansion",
                  "score_entry_proximity", "score_vwap", "score_retest"):
        row[field] = candidate.get(field)

    # -- forward prices and returns ------------------------------------
    for minutes in HORIZONS:
        at = _price_at(forward, start, minutes)
        row[f"price_{minutes}m"] = at
        row[f"return_{minutes}m"] = _pct(price, at)
    row["session_close_price"] = _num(session_close_price)
    row["return_close"] = _pct(price, session_close_price)

    # -- excursions ----------------------------------------------------
    highs = [(_num(b.get("high")), b.get("timestamp")) for b in forward]
    lows = [(_num(b.get("low")), b.get("timestamp")) for b in forward]
    highs = [(h, t) for h, t in highs if h is not None]
    lows = [(l, t) for l, t in lows if l is not None]

    if price and highs:
        best, best_at = max(highs, key=lambda pair: pair[0])
        # Clamped at 0: an excursion in the favourable direction that
        # never went favourable is zero, not a negative favourable one.
        row["mfe_pct"] = max(0.0, _pct(price, best) or 0.0)
        row["time_to_peak_minutes"] = _minutes(start, best_at)
    else:
        row["mfe_pct"] = row["time_to_peak_minutes"] = None

    if price and lows:
        worst, worst_at = min(lows, key=lambda pair: pair[0])
        row["mae_pct"] = min(0.0, _pct(price, worst) or 0.0)
        row["time_to_max_adverse_minutes"] = _minutes(start, worst_at)
    else:
        row["mae_pct"] = row["time_to_max_adverse_minutes"] = None

    # -- research labels ------------------------------------------------
    row.update(_reentry_labels(forward, start, range_high))
    row.update(_vwap_labels(forward, start))
    # The headline label, defined the way S6_EXIT_V0 defines its primary
    # exit: back inside the range it broke out of. Recorded, not applied.
    row["false_breakout"] = row.get("range_reentry_30m")
    row["time_to_range_reentry_minutes"] = _first_reentry_minutes(
        forward, start, range_high)
    return row


def _reentry_labels(bars, start, range_high) -> Dict[str, Any]:
    labels = {}
    for minutes in REENTRY_HORIZONS:
        key = f"range_reentry_{minutes}m"
        if range_high is None or not bars:
            labels[key] = None
            continue
        # A re-entry is conclusive as soon as it happens. SURVIVING is
        # not: it can only be asserted once the window has actually
        # elapsed. An earlier version set the label False from a single
        # bar at minute zero, which claimed the candidate had held for an
        # hour it never reached.
        elapsed_window = False
        reentered = False
        for bar in bars:
            elapsed = _minutes(start, bar.get("timestamp"))
            if elapsed is None:
                continue
            if elapsed >= minutes:
                elapsed_window = True
            if elapsed > minutes:
                continue
            close = _num(bar.get("close"))
            if close is not None and close <= range_high:
                reentered = True
                break
        labels[key] = True if reentered else (False if elapsed_window else None)
    return labels


def _first_reentry_minutes(bars, start, range_high) -> Optional[float]:
    if range_high is None:
        return None
    for bar in bars or []:
        close = _num(bar.get("close"))
        if close is not None and close <= range_high:
            return _minutes(start, bar.get("timestamp"))
    return None


def _vwap_labels(bars, start) -> Dict[str, Any]:
    labels = {}
    for minutes in VWAP_FAILURE_HORIZONS:
        key = f"vwap_failure_{minutes}m"
        # Same asymmetry as re-entry: a failure is conclusive when it
        # happens, holding VWAP can only be claimed once the window has
        # elapsed AND a vwap was actually readable.
        elapsed_window = False
        measurable = False
        failed = False
        for bar in bars or []:
            elapsed = _minutes(start, bar.get("timestamp"))
            if elapsed is None:
                continue
            if elapsed >= minutes:
                elapsed_window = True
            if elapsed > minutes:
                continue
            close, vwap = _num(bar.get("close")), _num(bar.get("vwap"))
            if close is None or vwap is None:
                continue
            measurable = True
            if close < vwap:
                failed = True
                break
        labels[key] = True if failed else (
            False if (elapsed_window and measurable) else None)
    return labels


def population_of(row: Dict[str, Any]) -> str:
    if row.get("live_eligible"):
        return POP_LIVE
    kind = str(row.get("security_type") or "").upper()
    if kind in ("ETP", "ETF", "ETN"):
        return POP_ETP
    if kind in ("", "UNKNOWN"):
        return POP_UNKNOWN
    return POP_UNKNOWN


def summarise(rows: List[Dict[str, Any]], *, minimum: int = 1
              ) -> Dict[str, Dict[str, Any]]:
    """Per-population aggregates, never merged.

    ALL_OBSERVED includes everything; the others partition it. Reporting
    one blended number would let the instruments that can be traded be
    judged by the ones that cannot -- and the trading decision rests on
    LIVE_ELIGIBLE_COMMON_STOCK alone.
    """
    groups: Dict[str, List[Dict[str, Any]]] = {POP_ALL: list(rows or [])}
    for row in rows or []:
        groups.setdefault(population_of(row), []).append(row)

    fields = (["return_%dm" % m for m in HORIZONS]
              + ["return_close", "mfe_pct", "mae_pct",
                 "time_to_peak_minutes", "volume_expansion",
                 "daily_relative_volume", "normalized_breakout_by_range",
                 "score"])
    out = {}
    for name, group in groups.items():
        summary = {"candidates": len(group)}
        for field in fields:
            values = [_num(r.get(field)) for r in group]
            values = [v for v in values if v is not None]
            summary[field] = {
                "mean": sum(values) / len(values) if len(values) >= max(1, minimum) else None,
                "n": len(values),
            }
        labelled = [r.get("false_breakout") for r in group
                    if r.get("false_breakout") is not None]
        summary["false_breakout_rate"] = {
            "rate": (sum(1 for v in labelled if v) / len(labelled)
                     if labelled else None),
            "n": len(labelled),
        }
        out[name] = summary
    return out
