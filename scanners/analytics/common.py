"""Statistics shared by the weekly, monthly and intersection reports.

One implementation of "average return" for all three, so a weekly figure
and the monthly figure covering the same week cannot disagree because
one of them counted the signals whose horizon had not matured and the
other did not.

The rules, stated once
----------------------
* Nulls are EXCLUDED, not treated as zero. A signal two days old has no
  5-day return; averaging it in as 0.0 would pull every 5-day average
  toward zero in proportion to how much recent data the window contains,
  which would make the most recent week of any report look worst.
* Every statistic is reported with the count it was computed from
  (`return_5d_n`). An average over four signals and one over four
  hundred are not comparable, and month-2 calibration acting on the
  former without knowing it is exactly the overfitting section 20 warns
  against.
* Medians alongside means, because these distributions have long right
  tails: one +140% signal moves a mean and does not move a median, and
  the gap between the two is itself the interesting reading.
"""

import math
import statistics
from collections import namedtuple
from typing import Any, Dict, Iterable, List, Optional, Sequence

#: The return horizons every report summarises (section 15/16).
RETURN_FIELDS = ("return_30m", "return_1h", "return_2h", "return_close",
                 "return_1d", "return_3d", "return_5d")

#: Excursion fields (section 13).
MFE_FIELDS = ("mfe_1d", "mfe_3d", "mfe_5d")
MAE_FIELDS = ("mae_1d", "mae_3d", "mae_5d")


def numbers(rows: Iterable[Dict[str, Any]], field: str) -> List[float]:
    """Finite values of `field` across `rows`. Nulls and NaN dropped."""
    values = []
    for row in rows:
        value = row.get(field)
        if value is None or isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            values.append(number)
    return values


def mean(values: Sequence[float]) -> Optional[float]:
    return round(statistics.fmean(values), 4) if values else None


def median(values: Sequence[float]) -> Optional[float]:
    return round(statistics.median(values), 4) if values else None


def percentile(values: Sequence[float], fraction: float) -> Optional[float]:
    """Linear-interpolated percentile (spec section 30).

    Hand-rolled rather than `statistics.quantiles`, which needs at least
    two data points and raises on one. A scanner with a single matured
    signal is a real and frequent state in week 1, and a report that
    raised there would be a report nobody could run early on.

    With one value, that value IS every percentile, which is the honest
    answer -- and `*_n` alongside it says the sample is one.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 4)
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 4)


def positive_rate(values: Sequence[float]) -> Optional[float]:
    """Share of values strictly above zero, as a percentage.

    Strictly above: an exactly-flat outcome is not a win. It is rare on
    a percentage return and common enough on a clamped MFE that letting
    it count would inflate the rate for the worst signals.
    """
    if not values:
        return None
    wins = sum(1 for value in values if value > 0)
    return round(wins / len(values) * 100.0, 2)


def describe(rows: List[Dict[str, Any]], field: str) -> Dict[str, Any]:
    """Mean, median, quartiles and count for one field.

    Section 30: the mean alone is not enough. One +100% signal drags an
    average MFE somewhere no individual signal went, and the only way to
    see that from a report is to have the median and the quartiles
    printed beside it. The p25/p75 gap is also the cheapest read on
    whether a scanner's edge is broad or carried by two names.
    """
    values = numbers(rows, field)
    return {
        f"avg_{field}": mean(values),
        f"median_{field}": median(values),
        f"p25_{field}": percentile(values, 0.25),
        f"p75_{field}": percentile(values, 0.75),
        f"min_{field}": round(min(values), 4) if values else None,
        f"max_{field}": round(max(values), 4) if values else None,
        f"{field}_n": len(values),
    }


def extreme(rows: List[Dict[str, Any]], field: str, *, largest: bool) -> Optional[Dict[str, Any]]:
    """The best or worst row by `field`, with enough context to look it up."""
    candidates = [row for row in rows if _finite(row.get(field))]
    if not candidates:
        return None
    chosen = (max if largest else min)(candidates, key=lambda row: float(row[field]))
    return {
        "symbol": chosen.get("symbol"),
        "trading_day": chosen.get("trading_day"),
        "scanner_name": chosen.get("scanner_name"),
        "signal_id": chosen.get("signal_id"),
        "scanner_score": chosen.get("scanner_score"),
        field: round(float(chosen[field]), 4),
    }


def _finite(value) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def summarise(
    rows: List[Dict[str, Any]],
    *,
    hit_horizon: str = "return_1d",
    include_extremes: bool = True,
) -> Dict[str, Any]:
    """The standard block of statistics for any group of signal rows.

    Used for a scanner's week, a scanner's month, and a scanner
    combination in the intersection analysis, so the three are directly
    comparable rather than three similar-looking calculations.
    """
    summary: Dict[str, Any] = {"signal_count": len(rows)}

    for field in RETURN_FIELDS:
        summary.update(describe(rows, field))
    for field in MFE_FIELDS + MAE_FIELDS:
        summary.update(describe(rows, field))

    # Headline MFE/MAE. Section 15 asks for avg/median of each without
    # naming a horizon; 5 days is the longest one tracked and the one
    # section 16's MFE/MAE ratio is most meaningful over.
    summary["avg_mfe"] = summary.get("avg_mfe_5d")
    summary["median_mfe"] = summary.get("median_mfe_5d")
    summary["avg_mae"] = summary.get("avg_mae_5d")
    summary["median_mae"] = summary.get("median_mae_5d")

    average_mfe = summary["avg_mfe"]
    average_mae = summary["avg_mae"]
    if average_mfe is not None and average_mae not in (None, 0):
        # Section 16. Above 1 means the average signal offered more
        # upside than it put at risk before the horizon closed. Reported
        # against the ABSOLUTE MAE so the ratio stays positive and
        # readable; MAE itself is kept signed everywhere else.
        summary["mfe_mae_ratio"] = round(average_mfe / abs(average_mae), 4)
    else:
        summary["mfe_mae_ratio"] = None

    for field in ("return_1d", "return_3d", "return_5d"):
        summary[f"positive_rate_{field[len('return_'):]}"] = positive_rate(
            numbers(rows, field))

    summary["hit_horizon"] = hit_horizon
    summary["positive_rate"] = positive_rate(numbers(rows, hit_horizon))
    summary["hit_rate"] = summary["positive_rate"]

    scores = numbers(rows, "scanner_score")
    summary["avg_scanner_score"] = mean(scores)
    summary["median_scanner_score"] = median(scores)

    if include_extremes:
        summary["best_candidate"] = extreme(rows, hit_horizon, largest=True)
        summary["worst_candidate"] = extreme(rows, hit_horizon, largest=False)
    return summary


def group_by(rows: List[Dict[str, Any]], field: str) -> Dict[Any, List[Dict[str, Any]]]:
    grouped: Dict[Any, List[Dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row.get(field), []).append(row)
    return grouped


#: What makes two rows part of the SAME experiment (spec sections 11,
#: 12 and 19).
#:
#: A scanner's name alone is not an experiment. Four things have to
#: match before two signals may be averaged together:
#:
#:   scanner_name           obviously
#:   scanner_version        section 19: a version bump is a new experiment
#:   config_fingerprint     section 11: catches a parameter edit made
#:                          WITHOUT a version bump, which is the case
#:                          nobody notices until the month is already
#:                          contaminated
#:   market_data_provider   section 12: the same scanner over Yahoo bars
#:                          and over Alpaca bars is two experiments. Bar
#:                          timestamps, adjustment policy and extended-
#:                          hours coverage all differ between vendors, so
#:                          merging them measures the vendor gap and
#:                          calls it scanner performance.
#:
#: `config_fingerprint` lives in the signal's metrics and arrives here
#: flattened as `metric_config_fingerprint` (see result_store.joined_rows).
EXPERIMENT_KEY_FIELDS = (
    "scanner_name",
    "scanner_version",
    "metric_config_fingerprint",
    "market_data_provider",
)

ExperimentKey = namedtuple(
    "ExperimentKey", ["scanner_name", "scanner_version", "config_fingerprint",
                      "market_data_provider"])


def experiment_key(row: Dict[str, Any]) -> ExperimentKey:
    """The experiment a single row belongs to."""
    return ExperimentKey(*(_key_part(row.get(field)) for field in EXPERIMENT_KEY_FIELDS))


def _key_part(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def group_by_experiment(rows: List[Dict[str, Any]]) -> Dict[ExperimentKey, List[Dict[str, Any]]]:
    """Split rows into experiments. Nothing merges across the key.

    This is the function that makes section 12's guarantee structural
    rather than a promise: two providers' results cannot end up in one
    average, because they cannot end up in one bucket.
    """
    grouped: Dict[ExperimentKey, List[Dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(experiment_key(row), []).append(row)
    return grouped


def group_by_scanner_version(rows: List[Dict[str, Any]]) -> Dict[Any, List[Dict[str, Any]]]:
    """Group on (scanner, version) only.

    Kept for callers that genuinely want the looser grouping -- notably
    `split_experiments`, which has to see the rows a scanner produced
    ACROSS experiments in order to notice that there is more than one.
    Reports group by `group_by_experiment` instead.
    """
    grouped: Dict[Any, List[Dict[str, Any]]] = {}
    for row in rows:
        key = (row.get("scanner_name"), row.get("scanner_version"))
        grouped.setdefault(key, []).append(row)
    return grouped


def split_experiments(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Scanner versions whose rows span more than one experiment.

    Splitting the buckets (above) stops bad merges, but silently: a
    reader sees two rows for `hma_early_trend_v1.0` and may well read it
    as a display quirk. This turns each split into an explicit finding
    that names the dimension responsible, so the report can say WHY the
    scanner appears twice.

    Two causes, with very different meanings:

      config_fingerprint differs  someone edited a parameter without
                                  bumping the version (section 11/19).
                                  The month is now a blend of two
                                  parameter sets.
      market_data_provider differs  the data vendor changed mid-window
                                  (section 12).

    Returns one entry per affected (scanner, version), empty when
    everything is clean -- which is what month 1 should look like.
    """
    findings = []
    for (name, version), members in sorted(
            group_by_scanner_version(rows).items(), key=lambda item: str(item[0])):
        keys = {experiment_key(row) for row in members}
        if len(keys) <= 1:
            continue
        fingerprints = sorted({key.config_fingerprint for key in keys}, key=str)
        providers = sorted({key.market_data_provider for key in keys}, key=str)
        causes = []
        if len(fingerprints) > 1:
            causes.append("config_fingerprint")
        if len(providers) > 1:
            causes.append("market_data_provider")
        findings.append({
            "scanner_name": name,
            "scanner_version": version,
            "experiment_count": len(keys),
            "config_fingerprints": fingerprints,
            "market_data_providers": providers,
            "causes": causes,
            "signal_count": len(members),
        })
    return findings


def format_split_warning(finding: Dict[str, Any]) -> str:
    """The section 11 warning text, one finding per call."""
    lines = [
        "WARNING",
        f"Scanner results span {finding['experiment_count']} experiments "
        f"and are NOT merged.",
        "",
        "scanner:",
        f"{finding['scanner_name']} / {finding['scanner_version']}",
        "",
    ]
    if "config_fingerprint" in finding["causes"]:
        lines.append("configuration changed without a version bump; fingerprints:")
        lines.extend(f"  {value}" for value in finding["config_fingerprints"])
    if "market_data_provider" in finding["causes"]:
        lines.append("market data provider changed; providers:")
        lines.extend(f"  {value}" for value in finding["market_data_providers"])
    return "\n".join(lines)
