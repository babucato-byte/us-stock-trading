"""Per-scanner weekly report (spec section 15).

A week is short enough that most of its signals have not reached their
5-day horizon when the report runs on Friday. That is stated in the
output rather than hidden: every statistic carries the count it was
computed from, and `maturity` says how many of the week's signals have
a 5-day return at all.

Without that, the first report of every month reads as a real finding
("scanner X had a terrible week") when what actually happened is that
the only signals with a matured horizon were Monday's.

The week runs Monday-Sunday over TRADING days. A trading day with no
scan simply contributes nothing; there is no zero-filling, because a
missing scan is an operational fact and dressing it as a day with zero
signals would hide it.
"""

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from scanners.analytics.common import (
    format_split_warning,
    group_by_experiment,
    numbers,
    split_experiments,
    summarise,
)
from scanners.base import result_store

logger = logging.getLogger(__name__)


def week_bounds(reference: Optional[date] = None) -> Tuple[str, str]:
    """The Monday-Sunday week containing `reference` (default: today)."""
    day = reference or date.today()
    monday = day - timedelta(days=day.weekday())
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def build(
    start_day: str,
    end_day: str,
    *,
    hit_horizon: str = "return_1d",
) -> Dict[str, Any]:
    """The week's report.

    `hit_horizon` defaults to the 1-day return here, not the 5-day one
    the monthly report uses. Within a single week the 5-day horizon has
    matured for at most the first day or two of signals, so a hit rate
    computed on it would describe Monday rather than the week. The
    5-day figures are still reported -- with their counts -- they are
    just not the headline.
    """
    rows = result_store.joined_rows(start_day, end_day)
    scanners = []
    # Grouped by EXPERIMENT (scanner, version, config fingerprint, data
    # provider) rather than by scanner. Sections 11 and 12: a parameter
    # edit or a vendor switch mid-window produces two experiments, and
    # averaging across them would report a blend describing neither.
    for key, members in sorted(
            group_by_experiment(rows).items(), key=lambda item: tuple(
                "" if part is None else str(part) for part in item[0])):
        summary = summarise(members, hit_horizon=hit_horizon)
        summary["scanner_name"] = key.scanner_name
        summary["scanner_version"] = key.scanner_version
        summary["config_fingerprint"] = key.config_fingerprint
        summary["market_data_provider"] = key.market_data_provider
        summary["maturity"] = _maturity(members)
        scanners.append(summary)

    return {
        "report": "weekly",
        "start_day": start_day,
        "end_day": end_day,
        "generated_at": datetime.now().astimezone().isoformat(),
        "hit_horizon": hit_horizon,
        "trading_days": sorted({str(row.get("trading_day")) for row in rows
                                if row.get("trading_day")}),
        "total_signals": len(rows),
        "scanners": scanners,
        # Empty is what a clean month 1 looks like. A non-empty list
        # means a scanner's rows were split across experiments, and the
        # entry says which dimension caused it.
        "experiment_splits": split_experiments(rows),
    }


def _maturity(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """How much of this group has actually reached each horizon.

    The single most misread number in a weekly report is an average over
    four matured signals out of ninety. This block is what stops that
    being invisible.
    """
    total = len(rows)
    matured = {}
    for field in ("return_close", "return_1d", "return_3d", "return_5d"):
        count = len(numbers(rows, field))
        matured[field] = {
            "n": count,
            "pct_of_signals": round(count / total * 100.0, 1) if total else None,
        }
    return matured


def format_report(report: Dict[str, Any]) -> str:
    lines = [
        f"Scanner weekly report  {report['start_day']} .. {report['end_day']}",
        f"generated {report['generated_at']}",
        f"trading days with signals: {len(report['trading_days'])}   "
        f"total signals: {report['total_signals']}   "
        f"hit horizon: {report['hit_horizon']}",
        "",
    ]
    header = (f"{'scanner':20} {'version':24} {'provider':10} {'n':>5} {'hit%':>7} "
              f"{'1d':>7} {'3d':>7} {'5d':>7} {'MFE':>7} {'MAE':>7} {'5d n':>5}")
    lines.append(header)
    lines.append("-" * len(header))
    for item in report["scanners"]:
        lines.append(
            f"{str(item['scanner_name'])[:20]:20} {str(item['scanner_version'])[:24]:24} "
            f"{str(item.get('market_data_provider') or '-')[:10]:10} "
            f"{item['signal_count']:5} {_num(item['hit_rate']):>7} "
            f"{_num(item['avg_return_1d']):>7} {_num(item['avg_return_3d']):>7} "
            f"{_num(item['avg_return_5d']):>7} {_num(item['avg_mfe']):>7} "
            f"{_num(item['avg_mae']):>7} {item['maturity']['return_5d']['n']:5}")

    lines.append("")
    lines.append("Distribution and extremes  (section 30: the mean alone hides outliers)")
    for item in report["scanners"]:
        lines.append(f"  {item['scanner_name']} ({item['scanner_version']}) "
                     f"via {item.get('market_data_provider') or 'unknown provider'}")
        lines.append(f"    MFE 5d   avg {_num(item['avg_mfe'])}  med {_num(item['median_mfe'])}  "
                     f"p25 {_num(item.get('p25_mfe_5d'))}  p75 {_num(item.get('p75_mfe_5d'))}")
        lines.append(f"    MAE 5d   avg {_num(item['avg_mae'])}  med {_num(item['median_mae'])}  "
                     f"p25 {_num(item.get('p25_mae_5d'))}  p75 {_num(item.get('p75_mae_5d'))}")
        lines.append(f"    ret 1d   avg {_num(item['avg_return_1d'])}  "
                     f"med {_num(item['median_return_1d'])}  "
                     f"p25 {_num(item.get('p25_return_1d'))}  "
                     f"p75 {_num(item.get('p75_return_1d'))}  "
                     f"MFE/MAE {_num(item['mfe_mae_ratio'])}")
        lines.append(f"    best   {_candidate(item.get('best_candidate'), report['hit_horizon'])}")
        lines.append(f"    worst  {_candidate(item.get('worst_candidate'), report['hit_horizon'])}")

    for finding in report.get("experiment_splits") or []:
        lines.append("")
        lines.append(format_split_warning(finding))
    return "\n".join(lines)


def _candidate(entry: Optional[Dict[str, Any]], horizon: str) -> str:
    if not entry:
        return "-"
    return (f"{entry.get('symbol')} on {entry.get('trading_day')}  "
            f"{horizon}={_num(entry.get(horizon))}%")


def _num(value, digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}"


def write(report: Dict[str, Any]) -> str:
    """Persist the report as JSON under the analytics store's reports/."""
    path = result_store.reports_dir() / f"weekly_{report['start_day']}_{report['end_day']}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return str(path)
