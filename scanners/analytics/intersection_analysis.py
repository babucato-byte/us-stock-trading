"""Which scanner COMBINATIONS found the better symbols (spec section 17).

The question
------------
Section 6 deliberately keeps duplicates: if NVDA passes the HMA,
accumulation and breakout scanners on the same day, three separate rows
are recorded. This module is why. It regroups those rows by
(trading_day, symbol), works out which set of scanners agreed, and
reports each combination's statistics separately:

    hma_early_trend only
    accumulation only
    hma_early_trend + accumulation
    hma_early_trend + accumulation + breakout_ready
    ...

Deliberately NOT a filter
-------------------------
Section 17 closes by saying agreement must not be a precondition for
entry at this stage, and section 18 says a `confirmation_count` may
inform ranking only after a month of evidence. So `confirmation_count`
is computed and recorded, and nothing acts on it. It is a column in a
report, not a gate.

The combination is a frozenset, ordered
---------------------------------------
Combinations are keyed on a SORTED tuple of scanner names, so
"HMA + Accumulation" and "Accumulation + HMA" are one bucket rather than
two half-sized ones. With six scanners there are 63 possible
combinations and most days will populate only a handful; buckets that
never occur are simply absent rather than reported as zero-signal rows.

Whose return is it?
-------------------
A symbol found by three scanners has three signal rows, three
`signal_price`s and therefore three sets of forward returns -- the
intraday scanners priced it at a different moment than the daily ones.
The combination's return is the MEAN across those rows, and the count of
rows behind it is reported. Picking one arbitrarily would make the
combination's statistics depend on which scanner happened to sort first.
"""

import logging
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

from scanners.analytics.common import RETURN_FIELDS, MAE_FIELDS, MFE_FIELDS, summarise
from scanners.base import result_store

logger = logging.getLogger(__name__)

#: Fields averaged across the several signal rows one symbol-day can have.
_AVERAGED = tuple(RETURN_FIELDS) + tuple(MFE_FIELDS) + tuple(MAE_FIELDS) + ("scanner_score",)


#: How agreement is defined (spec section 22).
#:
#:   "day"  two scanners flagged the symbol on the same trading day
#:   "run"  two scanners flagged it in the SAME runner invocation, from
#:          the same data snapshot
#:
#: The two are genuinely different claims and the weaker one is easy to
#: over-read. Under "day", the premarket scanner's 09:20 signal and the
#: ORB scanner's 09:50 signal count as agreement -- but they saw
#: different prices, half an hour apart, and one may have fired because
#: of the move the other one caused. Under "run", both judged the same
#: bars at the same instant, which is the stronger statement.
#:
#: Month 1 records both so month 2 can find out whether the stricter
#: definition predicts anything the looser one does not.
BY_DAY = "day"
BY_RUN = "run"


def build_symbol_days(
    rows: Iterable[Dict[str, Any]],
    *,
    scope: str = BY_DAY,
) -> List[Dict[str, Any]]:
    """Collapse signal rows into one record per symbol per scope.

    `scope=BY_DAY` groups on (trading_day, symbol); `scope=BY_RUN`
    groups on (scanner_run_id, symbol). See the note above for why both
    exist.

    Each record carries the set of scanners that flagged it, the
    `confirmation_count` section 18 asks for, and the mean of each
    forward-return field across the contributing rows.
    """
    if scope not in (BY_DAY, BY_RUN):
        raise ValueError(f"unknown intersection scope {scope!r}")

    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        day = row.get("trading_day")
        symbol = row.get("symbol")
        if not day or not symbol:
            continue
        if scope == BY_RUN:
            run_id = row.get("scanner_run_id")
            # A row with no run id cannot participate in run-scoped
            # agreement. Dropping it is right: bucketing every such row
            # under a shared "unknown" key would invent agreement
            # between signals that have nothing in common but a missing
            # field.
            if not run_id:
                continue
            grouped[(str(run_id), str(symbol))].append(row)
        else:
            grouped[(str(day), str(symbol))].append(row)

    records = []
    for (bucket, symbol), members in grouped.items():
        scanners = sorted({str(member.get("scanner_name")) for member in members
                           if member.get("scanner_name")})
        day = str(members[0].get("trading_day"))
        record: Dict[str, Any] = {
            "scope": scope,
            "trading_day": day,
            "scanner_run_id": members[0].get("scanner_run_id"),
            "symbol": symbol,
            "scanners": scanners,
            "combination": " + ".join(scanners),
            "confirmation_count": len(scanners),
            "signal_rows": len(members),
        }
        for field in _AVERAGED:
            values = [member.get(field) for member in members]
            usable = [float(value) for value in values
                      if value is not None and not isinstance(value, bool)]
            record[field] = round(sum(usable) / len(usable), 4) if usable else None
        # Kept so `extreme()` can report a combination's best and worst
        # symbol-day with something to look the signal up by.
        record["scanner_name"] = record["combination"]
        record["signal_id"] = members[0].get("signal_id")
        records.append(record)
    return records


def analyse(
    rows: Iterable[Dict[str, Any]],
    *,
    hit_horizon: str = "return_5d",
    min_signals: int = 1,
    scope: str = BY_DAY,
) -> Dict[str, Any]:
    """Per-combination statistics, plus a by-confirmation-count rollup.

    `min_signals` suppresses combinations too rare to say anything
    about. It defaults to 1 (report everything) because silently hiding
    small buckets is how a reader ends up believing a combination never
    occurred; the count is on every row so a reader can apply their own
    threshold with the evidence in front of them.
    """
    symbol_days = build_symbol_days(rows, scope=scope)

    by_combination: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_count: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for record in symbol_days:
        by_combination[record["combination"]].append(record)
        by_count[record["confirmation_count"]].append(record)

    combinations = []
    for name, members in by_combination.items():
        if len(members) < min_signals:
            continue
        summary = summarise(members, hit_horizon=hit_horizon)
        summary["combination"] = name
        summary["scanners"] = members[0]["scanners"]
        summary["confirmation_count"] = members[0]["confirmation_count"]
        combinations.append(summary)
    combinations.sort(key=lambda item: (-item["signal_count"], item["combination"]))

    confirmation_levels = []
    for count in sorted(by_count):
        summary = summarise(by_count[count], hit_horizon=hit_horizon)
        summary["confirmation_count"] = count
        confirmation_levels.append(summary)

    return {
        "scope": scope,
        "hit_horizon": hit_horizon,
        "symbol_day_count": len(symbol_days),
        "combinations": combinations,
        "by_confirmation_count": confirmation_levels,
        # Section 18 explicitly defers acting on this. Stated in the
        # output so a reader of the report cannot mistake it for a rule
        # the system is already applying.
        "note": ("Recorded for analysis only. Section 17/18: scanner agreement is "
                 "not an entry condition and is not used in candidate ranking in v1.0."),
    }


def analyse_range(
    start_day: str,
    end_day: str,
    *,
    hit_horizon: str = "return_5d",
    min_signals: int = 1,
    scope: str = BY_DAY,
) -> Dict[str, Any]:
    rows = result_store.joined_rows(start_day, end_day)
    result = analyse(rows, hit_horizon=hit_horizon, min_signals=min_signals, scope=scope)
    result["start_day"] = start_day
    result["end_day"] = end_day
    return result


def analyse_both_scopes(
    start_day: str,
    end_day: str,
    *,
    hit_horizon: str = "return_5d",
    min_signals: int = 1,
) -> Dict[str, Any]:
    """Both definitions of agreement, side by side.

    Reported together on purpose. The gap between them is itself
    informative: if same-day agreement looks predictive but same-run
    agreement does not, the "agreement" was two scanners reacting to the
    same move at different times, not two independent reads on one
    setup.
    """
    rows = result_store.joined_rows(start_day, end_day)
    result = {"start_day": start_day, "end_day": end_day, "scopes": {}}
    for scope in (BY_DAY, BY_RUN):
        analysis = analyse(rows, hit_horizon=hit_horizon,
                           min_signals=min_signals, scope=scope)
        analysis["start_day"] = start_day
        analysis["end_day"] = end_day
        result["scopes"][scope] = analysis
    return result


def format_report(result: Dict[str, Any]) -> str:
    lines = []
    scope = result.get("scope", BY_DAY)
    unit = "symbol-runs" if scope == BY_RUN else "symbol-days"
    lines.append(f"Scanner intersection analysis  "
                 f"{result.get('start_day', '?')} .. {result.get('end_day', '?')}")
    lines.append(f"scope: {scope}  "
                 + ("(agreement within ONE runner invocation, same data snapshot)"
                    if scope == BY_RUN
                    else "(agreement anywhere in the same trading day)"))
    lines.append(f"{unit}: {result['symbol_day_count']}   "
                 f"hit horizon: {result['hit_horizon']}")
    lines.append("")
    header = (f"{'combination':52} {'n':>5} {'hit%':>7} {'avg5d':>8} "
              f"{'avgMFE':>8} {'avgMAE':>8} {'MFE/MAE':>8}")
    lines.append(header)
    lines.append("-" * len(header))
    for item in result["combinations"]:
        lines.append(
            f"{item['combination'][:52]:52} {item['signal_count']:5} "
            f"{_num(item['hit_rate']):>7} {_num(item['avg_return_5d']):>8} "
            f"{_num(item['avg_mfe']):>8} {_num(item['avg_mae']):>8} "
            f"{_num(item['mfe_mae_ratio']):>8}")
    lines.append("")
    lines.append("By number of scanners agreeing:")
    for item in result["by_confirmation_count"]:
        lines.append(
            f"  {item['confirmation_count']} scanner(s): n={item['signal_count']:5} "
            f"hit={_num(item['hit_rate'])}%  avg5d={_num(item['avg_return_5d'])}%  "
            f"MFE/MAE={_num(item['mfe_mae_ratio'])}")
    lines.append("")
    lines.append(result["note"])
    return "\n".join(lines)


def _num(value, digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}"
