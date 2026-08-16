"""What happened AFTER an S1 signal -- so an exit policy can be chosen later.

This module decides nothing. It measures price behaviour following
`hma_early_trend` signals and reports distributions, so that a stop, a
target and a holding period can eventually be argued from data instead of
borrowed from the scalping config whose horizon does not match a
daily-bar trend signal.

Post-hoc only, and structurally so
----------------------------------
Nothing here may influence which symbols are scanned, ranked or
published. Every function takes a signal that ALREADY happened and looks
at bars that came AFTER it; none is imported by `scanners/runner.py`,
`scanners/registry.py`, `watchlist/` or `s1_live/`, and
`tests/test_s1_exit_research.py` asserts that against the import graph.

The look-ahead rule, stated precisely: a horizon is only reported when it
has ELAPSED. A signal from yesterday has no 5-day return, and this module
records that as PENDING rather than measuring a shorter window and
labelling it `return_5d`. Mixing a 1-day move into a 5-day statistic
would make short horizons look like long ones and would do it in the
optimistic direction, because a trend that later failed has not failed
yet.

Reuse, not refetch
------------------
`scanners/analytics/performance_tracker.py` already computes and stores
returns for 30m/1h/2h/close/1d/3d/5d and MFE/MAE for 1d/3d/5d. Those are
read from the store as-is; the signal and performance files are opened
READ ONLY and never written.

What it does not already have is the 10-day horizon and the TIMING of
each excursion -- knowing the median MFE is 6% is not actionable without
knowing whether it typically arrives on day 1 or day 8. Those require
bars, so they are computed from daily bars fetched through the ordinary
provider, and only for signals whose window has elapsed.

Resolution, stated rather than implied
--------------------------------------
`time_to_mfe_days` and `time_to_mae_days` are measured in SESSIONS, from
daily bars. A daily bar knows the high and the low of a day but not which
came first, so intraday ordering within a single session is not
recoverable here and is not claimed. Day 0 means the excursion occurred
on the signal day itself.
"""

import logging
import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from scanners.base import result_store

logger = logging.getLogger(__name__)

S1_SCANNER = "hma_early_trend"

#: Holding windows studied, in trading sessions.
HORIZONS = (1, 2, 3, 5, 10)

#: Stop levels studied. RESEARCH ONLY -- these are not risk settings and
#: nothing reads them to place an order. They exist to answer "what would
#: have happened", which is the question a real stop has to be chosen on.
STOP_CANDIDATES = (-0.02, -0.03, -0.04, -0.05, -0.06, -0.08)

#: Target levels studied. Same status: research, not policy.
TARGET_CANDIDATES = (0.03, 0.05, 0.08, 0.10, 0.15)

#: How much later upside counts as the stop having been premature. Named
#: so the report cannot be read as "10% is the right target".
PREMATURE_STOP_RECOVERY = 0.10

# --- maturity vocabulary --------------------------------------------------
#
# Deliberately NOT tied to a hardcoded N that behaves like a strategy
# threshold. The report always prints the actual matured counts; these
# labels are a reading aid on top of those counts, and the boundaries are
# stated here so a reader can disagree with them while still seeing the
# raw numbers.
INSUFFICIENT = "INSUFFICIENT"
EARLY = "EARLY"
PROVISIONAL = "PROVISIONAL"
MATURE = "MATURE"

MATURITY_BANDS = ((0, INSUFFICIENT), (20, EARLY), (60, PROVISIONAL), (150, MATURE))


def classify_maturity(matured_count: int) -> str:
    label = INSUFFICIENT
    for floor, name in MATURITY_BANDS:
        if matured_count >= floor:
            label = name
    return label


# --- statistics -----------------------------------------------------------

def _finite(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def numbers(rows: Sequence[Dict[str, Any]], field_name: str) -> List[float]:
    values = [_finite(row.get(field_name)) for row in rows]
    return [value for value in values if value is not None]


def percentile(values: Sequence[float], fraction: float) -> Optional[float]:
    """Linear-interpolated percentile. One value IS every percentile."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 6)
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 6)


def distribution(values: Sequence[float]) -> Dict[str, Any]:
    """Mean AND median AND percentiles, always with n.

    The mean alone is the wrong summary for this data: a single signal
    that ran 140% moves it and does not move the median, and an exit
    policy chosen on a mean would be sized for an outcome most signals
    never have.
    """
    usable = [value for value in values if value is not None]
    if not usable:
        return {"n": 0, "mean": None, "median": None,
                "p25": None, "p50": None, "p75": None, "p90": None, "p95": None,
                "min": None, "max": None}
    return {
        "n": len(usable),
        "mean": round(statistics.fmean(usable), 6),
        "median": round(statistics.median(usable), 6),
        "p25": percentile(usable, 0.25),
        "p50": percentile(usable, 0.50),
        "p75": percentile(usable, 0.75),
        "p90": percentile(usable, 0.90),
        "p95": percentile(usable, 0.95),
        "min": round(min(usable), 6),
        "max": round(max(usable), 6),
    }


# --- excursion path -------------------------------------------------------

@dataclass
class Excursion:
    """MFE/MAE over a window, with WHEN each occurred."""

    horizon_days: int
    mfe_pct: Optional[float] = None
    mae_pct: Optional[float] = None
    time_to_mfe_days: Optional[int] = None
    time_to_mae_days: Optional[int] = None
    sessions_used: int = 0
    complete: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return dict(vars(self))


def excursion(entry_price, sessions: Sequence[Dict[str, Any]], horizon_days: int) -> Excursion:
    """Best and worst move within `horizon_days` sessions, and when.

    `sessions` are the bars AFTER the signal, oldest first, each with
    `high` and `low`. Day numbering is 1-based over that list.

    Incomplete windows are reported as incomplete rather than measured
    over what happened to be available -- see the module docstring on
    look-ahead.
    """
    price = _finite(entry_price)
    result = Excursion(horizon_days=horizon_days)
    if price is None or price <= 0 or not sessions:
        return result

    window = list(sessions)[:horizon_days]
    result.sessions_used = len(window)
    result.complete = len(window) >= horizon_days
    if not result.complete:
        return result

    best, best_day, worst, worst_day = None, None, None, None
    for index, bar in enumerate(window, start=1):
        high, low = _finite(bar.get("high")), _finite(bar.get("low"))
        if high is not None:
            move = (high - price) / price
            if best is None or move > best:
                best, best_day = move, index
        if low is not None:
            move = (low - price) / price
            if worst is None or move < worst:
                worst, worst_day = move, index
    if best is not None:
        # Floored at 0: a window whose highest high never exceeded entry
        # had no favourable excursion, not a negative one.
        result.mfe_pct = round(max(0.0, best), 6)
        result.time_to_mfe_days = best_day if best > 0 else None
    if worst is not None:
        result.mae_pct = round(min(0.0, worst), 6)
        result.time_to_mae_days = worst_day if worst < 0 else None
    return result


# --- exit simulations (RESEARCH ONLY) -------------------------------------

@dataclass
class StopOutcome:
    stop_pct: float
    hit: bool = False
    hit_day: Optional[int] = None
    return_at_stop: Optional[float] = None
    return_if_held: Optional[float] = None
    max_upside_after_stop: Optional[float] = None
    premature: bool = False
    avoided_worse: bool = False
    complete: bool = False

    def as_dict(self):
        return dict(vars(self))


def simulate_stop(entry_price, sessions, stop_pct, *, horizon_days: int) -> StopOutcome:
    """What a stop at `stop_pct` would have done. Places nothing.

    A stop is treated as filled at the stop level the first session whose
    LOW reaches it. That is optimistic on a gap-down day -- a real fill
    would be worse -- and the optimism is stated rather than modelled,
    because modelling slippage here would put an invented number into the
    research this phase exists to keep clean.
    """
    price = _finite(entry_price)
    outcome = StopOutcome(stop_pct=stop_pct)
    if price is None or price <= 0 or not sessions:
        return outcome
    window = list(sessions)[:horizon_days]
    outcome.complete = len(window) >= horizon_days
    if not outcome.complete:
        return outcome

    trigger = price * (1 + stop_pct)
    for index, bar in enumerate(window, start=1):
        low = _finite(bar.get("low"))
        if low is not None and low <= trigger:
            outcome.hit, outcome.hit_day = True, index
            outcome.return_at_stop = round(stop_pct, 6)
            after = window[index:]
            highs = [_finite(b.get("high")) for b in after]
            highs = [h for h in highs if h is not None]
            if highs:
                outcome.max_upside_after_stop = round((max(highs) - price) / price, 6)
                outcome.premature = outcome.max_upside_after_stop >= PREMATURE_STOP_RECOVERY
            break

    closes = [_finite(bar.get("close")) for bar in window]
    closes = [c for c in closes if c is not None]
    if closes:
        outcome.return_if_held = round((closes[-1] - price) / price, 6)
        if outcome.hit and outcome.return_if_held < stop_pct:
            outcome.avoided_worse = True
    return outcome


@dataclass
class TargetOutcome:
    target_pct: float
    hit: bool = False
    hit_day: Optional[int] = None
    max_upside_after_hit: Optional[float] = None
    forgone_pct: Optional[float] = None
    return_if_held: Optional[float] = None
    complete: bool = False

    def as_dict(self):
        return dict(vars(self))


def simulate_target(entry_price, sessions, target_pct, *, horizon_days: int) -> TargetOutcome:
    """What taking profit at `target_pct` would have done, and what it cost."""
    price = _finite(entry_price)
    outcome = TargetOutcome(target_pct=target_pct)
    if price is None or price <= 0 or not sessions:
        return outcome
    window = list(sessions)[:horizon_days]
    outcome.complete = len(window) >= horizon_days
    if not outcome.complete:
        return outcome

    trigger = price * (1 + target_pct)
    for index, bar in enumerate(window, start=1):
        high = _finite(bar.get("high"))
        if high is not None and high >= trigger:
            outcome.hit, outcome.hit_day = True, index
            after = window[index:]
            highs = [_finite(b.get("high")) for b in after]
            highs = [h for h in highs if h is not None]
            if highs:
                outcome.max_upside_after_hit = round((max(highs) - price) / price, 6)
                # Opportunity cost of exiting early: what the best later
                # high would have added on top of the target.
                outcome.forgone_pct = round(
                    max(0.0, outcome.max_upside_after_hit - target_pct), 6)
            break

    closes = [_finite(bar.get("close")) for bar in window]
    closes = [c for c in closes if c is not None]
    if closes:
        outcome.return_if_held = round((closes[-1] - price) / price, 6)
    return outcome


def simulate_time_exit(entry_price, sessions, *, horizon_days: int) -> Dict[str, Any]:
    """Closing at the Nth session's close, with nothing else."""
    price = _finite(entry_price)
    window = list(sessions)[:horizon_days]
    complete = len(window) >= horizon_days
    if price is None or price <= 0 or not complete:
        return {"horizon_days": horizon_days, "complete": complete, "return_pct": None}
    close = _finite(window[-1].get("close"))
    return {
        "horizon_days": horizon_days,
        "complete": True,
        "return_pct": round((close - price) / price, 6) if close is not None else None,
    }


# --- aggregation ----------------------------------------------------------

def read_s1_rows(start_day: str, end_day: str) -> List[Dict[str, Any]]:
    """S1 signals joined with their stored performance. READ ONLY."""
    return [row for row in result_store.joined_rows(start_day, end_day)
            if str(row.get("scanner_name")) == S1_SCANNER]


def summarise_returns(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Return distribution and win rate per horizon, from stored data."""
    out = {}
    for name in ("return_30m", "return_1h", "return_2h", "return_close",
                 "return_1d", "return_3d", "return_5d"):
        values = numbers(rows, name)
        stats = distribution(values)
        stats["positive_rate"] = (round(sum(1 for v in values if v > 0) / len(values), 6)
                                  if values else None)
        out[name] = stats
    return out


def summarise_excursions(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """MFE/MAE distributions from the stored 1d/3d/5d figures."""
    out = {}
    for days in (1, 3, 5):
        out[f"mfe_{days}d"] = distribution(numbers(rows, f"mfe_{days}d"))
        out[f"mae_{days}d"] = distribution(numbers(rows, f"mae_{days}d"))
    return out


def maturity(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """How much of the sample has actually reached each horizon."""
    counts = {"signals": len(rows)}
    for name in ("return_1d", "return_3d", "return_5d"):
        counts[f"matured_{name}"] = len(numbers(rows, name))
    counts["matured_return_10d"] = 0  # computed only by the bar pass
    driver = counts.get("matured_return_5d", 0)
    counts["status"] = classify_maturity(driver)
    counts["status_basis"] = "matured_return_5d"
    counts["bands"] = {name: floor for floor, name in MATURITY_BANDS}
    return counts


def build_report(start_day: str, end_day: str, *,
                 rows: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The research report for a window. Never writes to the source store."""
    data = list(rows) if rows is not None else read_s1_rows(start_day, end_day)
    report = {
        "report": "s1_exit_research",
        "scanner": S1_SCANNER,
        "start_day": start_day,
        "end_day": end_day,
        "maturity": maturity(data),
        "returns": summarise_returns(data),
        "excursions": summarise_excursions(data),
        "stop_candidates": [round(value, 4) for value in STOP_CANDIDATES],
        "target_candidates": [round(value, 4) for value in TARGET_CANDIDATES],
        "policy_status": "BLOCKED_BY_SAMPLE_MATURITY",
        "notice": ("research only -- no stop, target or holding period is "
                   "recommended or applied by this report"),
    }
    status = report["maturity"]["status"]
    if status in (INSUFFICIENT, EARLY):
        # Withholding the simulations at low n is the point: a stop level
        # chosen from four signals is a stop level chosen from noise, and
        # printing it would invite exactly that.
        report["simulations_withheld"] = (
            f"sample is {status}; stop/target/time-exit simulations are not "
            f"reported until the matured 5-day count supports them")
    return report


def format_report(report: Dict[str, Any]) -> str:
    maturity_block = report["maturity"]
    lines = [
        f"S1 EXIT RESEARCH  {report['start_day']} .. {report['end_day']}",
        f"  scanner        : {report['scanner']}",
        f"  policy status  : {report['policy_status']}",
        "",
        f"  signals        : {maturity_block['signals']}",
        f"  matured 1D/3D/5D/10D : {maturity_block['matured_return_1d']}/"
        f"{maturity_block['matured_return_3d']}/{maturity_block['matured_return_5d']}/"
        f"{maturity_block['matured_return_10d']}",
        f"  maturity       : {maturity_block['status']} "
        f"(on {maturity_block['status_basis']})",
        "",
    ]
    header = f"  {'metric':16} {'n':>5} {'median':>9} {'mean':>9} {'p25':>9} {'p75':>9} {'p95':>9}"
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))

    def row(label, stats, extra=""):
        def fmt(value):
            return "-" if value is None else f"{value * 100:.2f}%"
        lines.append(f"  {label:16} {stats['n']:5} {fmt(stats['median']):>9} "
                     f"{fmt(stats['mean']):>9} {fmt(stats['p25']):>9} "
                     f"{fmt(stats['p75']):>9} {fmt(stats['p95']):>9}{extra}")

    for name, stats in report["returns"].items():
        rate = stats.get("positive_rate")
        row(name, stats, f"  win {'-' if rate is None else f'{rate*100:.0f}%'}")
    lines.append("")
    for name, stats in report["excursions"].items():
        row(name, stats)

    if report.get("simulations_withheld"):
        lines.append("")
        lines.append(f"  {report['simulations_withheld']}")
    lines.append("")
    lines.append(f"  {report['notice']}")
    return "\n".join(lines)
