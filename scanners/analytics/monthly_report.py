"""Per-scanner monthly comparison (spec section 16).

The month-1 deliverable. Everything else in this package exists to make
this report trustworthy: frozen parameters (section 11), a fingerprint
proving they stayed frozen (section 19), the same feature pass behind
every scanner (section 7), and forward returns measured from the price
the scanner actually saw (section 12).

Two kinds of statistic, kept apart (section 14)
-----------------------------------------------
Scanner metrics -- signals, hit rate, mean/median return, mean/median
MFE and MAE, MFE/MAE ratio -- describe whether a scanner FOUND good
symbols. They are computed for every scanner, from signals alone, with
no entry rule involved.

Trading metrics -- profit factor, win rate, average win/loss, max
drawdown -- describe whether an entry rule TRADED them well. Section 16
says to compute those only for scanners an entry was actually applied
to. In v1.0 no scanner has one (section 30 keeps these scanners out of
the order path entirely), so `trading` is reported as not applicable
with the reason stated, rather than being silently computed from signal
returns as if they were trades. Computing it from signals would produce
a "profit factor" for a strategy nobody ran -- the single most
misleading number this report could contain.

`compute_trading_metrics` is written and tested so that when an entry
engine does exist, its realised trades can be passed in and the section
16 block fills itself in. It is never fed signal returns.
"""

import json
import logging
import math
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from scanners.analytics.common import (
    format_split_warning,
    group_by_experiment,
    numbers,
    split_experiments,
    summarise,
)
from scanners.analytics.intersection_analysis import analyse
from scanners.base import result_store

logger = logging.getLogger(__name__)


def month_bounds(year: int, month: int) -> Tuple[str, str]:
    """First and last calendar day of a month, inclusive, as ISO dates."""
    start = date(year, month, 1)
    next_month_start = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start.isoformat(), (next_month_start - timedelta(days=1)).isoformat()


def build(
    start_day: str,
    end_day: str,
    *,
    hit_horizon: str = "return_5d",
    trades: Optional[Iterable[Dict[str, Any]]] = None,
    include_intersections: bool = True,
) -> Dict[str, Any]:
    """The month's report.

    `trades` is the optional realised-trade list for the trading-metrics
    block. Passing signal rows here would be a category error and the
    report says so where the block would be.
    """
    rows = result_store.joined_rows(start_day, end_day)

    scanners = []
    splits = split_experiments(rows)
    # A (scanner, version) that appears under more than one experiment
    # key is flagged on every one of its rows, so a reader of a single
    # table line can see that this is one half of a split rather than a
    # whole month.
    split_index = {(item["scanner_name"], item["scanner_version"]): item
                   for item in splits}
    for key, members in sorted(
            group_by_experiment(rows).items(), key=lambda item: tuple(
                "" if part is None else str(part) for part in item[0])):
        summary = summarise(members, hit_horizon=hit_horizon)
        summary["scanner_name"] = key.scanner_name
        summary["scanner_version"] = key.scanner_version
        summary["config_fingerprint"] = key.config_fingerprint
        summary["market_data_provider"] = key.market_data_provider
        summary["trading_days_active"] = len({str(row.get("trading_day")) for row in members})
        summary["unique_symbols"] = len({str(row.get("symbol")) for row in members})
        split = split_index.get((key.scanner_name, key.scanner_version))
        summary["experiment_split"] = split
        # Sections 11/12: stable means this scanner+version produced ONE
        # experiment all month -- one parameter set, one data vendor.
        summary["parameters_stable"] = split is None
        summary["extension"] = _extension_profile(members, hit_horizon)
        scanners.append(summary)

    report: Dict[str, Any] = {
        "report": "monthly",
        "start_day": start_day,
        "end_day": end_day,
        "generated_at": datetime.now().astimezone().isoformat(),
        "hit_horizon": hit_horizon,
        "total_signals": len(rows),
        "trading_days": sorted({str(row.get("trading_day")) for row in rows
                                if row.get("trading_day")}),
        "scanners": scanners,
        "experiment_splits": splits,
    }

    if include_intersections:
        report["intersections"] = analyse(rows, hit_horizon=hit_horizon)

    if trades is None:
        report["trading"] = {
            "applicable": False,
            "reason": ("No entry engine is applied to these scanners in v1.0 "
                       "(spec sections 23 and 30), so there are no trades to measure. "
                       "Section 16's trading metrics are computed only for scanners an "
                       "entry was actually applied to; deriving them from signal returns "
                       "would report a profit factor for a strategy that was never run."),
        }
    else:
        report["trading"] = compute_trading_metrics(trades)
    return report


def _extension_profile(rows: List[Dict[str, Any]], hit_horizon: str) -> List[Dict[str, Any]]:
    """Outcome by how stretched the name was above its HMA200.

    Section 22 lists "do high-extension names perform worse?" as one of
    the questions month 1 exists to answer, and section 8 says to record
    extension without filtering on it in v1.0. Bucketing the recorded
    values here is how that question gets answered from the data instead
    of from an opinion -- and the buckets are deliberately coarse (0-5,
    5-10, 10-20, 20-40, 40+) for the reason section 20 gives: a cut
    chosen to three decimal places from one month of data is a fitted
    artefact, not a finding.
    """
    edges = [(None, 5.0), (5.0, 10.0), (10.0, 20.0), (20.0, 40.0), (40.0, None)]
    profile = []
    for low, high in edges:
        bucket = []
        for row in rows:
            value = row.get("extension_hma200_pct")
            if value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(number):
                continue
            if low is not None and number < low:
                continue
            if high is not None and number >= high:
                continue
            bucket.append(row)
        if not bucket:
            continue
        summary = summarise(bucket, hit_horizon=hit_horizon, include_extremes=False)
        summary["extension_bucket"] = (
            f"<{high:.0f}%" if low is None else
            (f">={low:.0f}%" if high is None else f"{low:.0f}-{high:.0f}%"))
        profile.append(summary)
    return profile


def compute_trading_metrics(trades: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Section 16's trading block, from REALISED trades.

    Each trade needs a `pnl` (currency or percent, consistently one or
    the other) and optionally `symbol` and `closed_at`. Deliberately
    accepts trades rather than signals: the distinction is section 14's
    whole point, and a function that would accept either would eventually
    be handed the wrong one.

    Max drawdown is computed on the cumulative P&L curve in the order
    given, so the caller must pass trades in close order; an unordered
    list yields a drawdown for a sequence that never occurred.
    """
    records = [trade for trade in trades if trade.get("pnl") is not None]
    if not records:
        return {"applicable": True, "trade_count": 0,
                "reason": "no closed trades in the window"}

    values = []
    for trade in records:
        try:
            values.append(float(trade["pnl"]))
        except (TypeError, ValueError):
            continue

    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)

    return {
        "applicable": True,
        "trade_count": len(values),
        # None rather than infinity when nothing lost: a profit factor of
        # `inf` sorts and averages in ways that quietly corrupt any table
        # it lands in.
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss else None,
        "win_rate": round(len(wins) / len(values) * 100.0, 2) if values else None,
        "average_win": round(sum(wins) / len(wins), 4) if wins else None,
        "average_loss": round(sum(losses) / len(losses), 4) if losses else None,
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
        "net_pnl": round(sum(values), 4),
        "max_drawdown": round(max_drawdown, 4),
    }


def format_report(report: Dict[str, Any]) -> str:
    lines = [
        f"Scanner monthly report  {report['start_day']} .. {report['end_day']}",
        f"generated {report['generated_at']}",
        f"trading days: {len(report['trading_days'])}   "
        f"total signals: {report['total_signals']}   "
        f"hit horizon: {report['hit_horizon']}",
        "",
        "SCANNER PERFORMANCE  (did it find good symbols?)",
    ]
    header = (f"{'scanner':20} {'provider':10} {'n':>5} {'sym':>5} {'hit%':>7} "
              f"{'avg5d':>8} {'med5d':>8} {'avgMFE':>8} {'medMFE':>8} "
              f"{'avgMAE':>8} {'medMAE':>8} {'MFE/MAE':>8}")
    lines.append(header)
    lines.append("-" * len(header))
    for item in report["scanners"]:
        lines.append(
            f"{str(item['scanner_name'])[:20]:20} "
            f"{str(item.get('market_data_provider') or '-')[:10]:10} "
            f"{item['signal_count']:5} "
            f"{item['unique_symbols']:5} {_num(item['hit_rate']):>7} "
            f"{_num(item['avg_return_5d']):>8} {_num(item['median_return_5d']):>8} "
            f"{_num(item['avg_mfe']):>8} {_num(item['median_mfe']):>8} "
            f"{_num(item['avg_mae']):>8} {_num(item['median_mae']):>8} "
            f"{_num(item['mfe_mae_ratio']):>8}")

    lines.append("")
    lines.append("DISTRIBUTION  (section 30: one +100% signal moves a mean, not a median)")
    dist_header = (f"{'scanner':20} {'provider':10} {'field':10} {'p25':>9} {'median':>9} "
                   f"{'p75':>9} {'min':>9} {'max':>9} {'n':>5}")
    lines.append(dist_header)
    lines.append("-" * len(dist_header))
    for item in report["scanners"]:
        for field in ("return_1d", "return_5d", "mfe_5d", "mae_5d"):
            lines.append(
                f"{str(item['scanner_name'])[:20]:20} "
                f"{str(item.get('market_data_provider') or '-')[:10]:10} "
                f"{field:10} {_num(item.get(f'p25_{field}')):>9} "
                f"{_num(item.get(f'median_{field}')):>9} "
                f"{_num(item.get(f'p75_{field}')):>9} "
                f"{_num(item.get(f'min_{field}')):>9} "
                f"{_num(item.get(f'max_{field}')):>9} "
                f"{item.get(f'{field}_n', 0):>5}")

    for finding in report.get("experiment_splits") or []:
        lines.append("")
        lines.append(format_split_warning(finding))

    lines.append("")
    lines.append("EXTENSION PROFILE  (section 22: do stretched names do worse?)")
    for item in report["scanners"]:
        if not item["extension"]:
            continue
        lines.append(f"  {item['scanner_name']}")
        for bucket in item["extension"]:
            lines.append(f"    {bucket['extension_bucket']:>8}  n={bucket['signal_count']:4}  "
                         f"hit={_num(bucket['hit_rate'])}%  "
                         f"avg5d={_num(bucket['avg_return_5d'])}%  "
                         f"MFE/MAE={_num(bucket['mfe_mae_ratio'])}")

    lines.append("")
    lines.append("TRADING PERFORMANCE  (did it trade them well?)")
    trading = report.get("trading") or {}
    if not trading.get("applicable"):
        lines.append(f"  not applicable -- {trading.get('reason')}")
    elif not trading.get("trade_count"):
        lines.append(f"  {trading.get('reason')}")
    else:
        lines.append(f"  trades {trading['trade_count']}   "
                     f"profit factor {_num(trading['profit_factor'])}   "
                     f"win rate {_num(trading['win_rate'])}%")
        lines.append(f"  avg win {_num(trading['average_win'])}   "
                     f"avg loss {_num(trading['average_loss'])}   "
                     f"max drawdown {_num(trading['max_drawdown'])}")

    intersections = report.get("intersections")
    if intersections:
        lines.append("")
        lines.append("SCANNER INTERSECTIONS  (section 17; analysis only, not an entry rule)")
        for item in intersections["combinations"][:15]:
            lines.append(f"  {item['combination'][:46]:46} n={item['signal_count']:4}  "
                         f"hit={_num(item['hit_rate'])}%  "
                         f"avg5d={_num(item['avg_return_5d'])}%  "
                         f"MFE/MAE={_num(item['mfe_mae_ratio'])}")
    return "\n".join(lines)


def _num(value, digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}"


def write(report: Dict[str, Any]) -> str:
    path = result_store.reports_dir() / f"monthly_{report['start_day']}_{report['end_day']}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return str(path)
