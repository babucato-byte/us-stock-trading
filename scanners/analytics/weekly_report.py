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
from scanners.base import result_store, run_context

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


def collect_run_health(start_day: str, end_day: str) -> Dict[str, Any]:
    """How the week's RUNS went, from the manifests rather than the signals.

    A week with no failed run and a week whose runs all failed both
    produce a report full of zeroes if you only read the signal files.
    The manifests are the only place the difference is recorded, so the
    Slack summary reads them: "0 signals" next to "4 failed runs" is a
    different message from "0 signals" next to "12 clean runs".
    """
    days, statuses, failed, partial, skipped, breakers = [], {}, [], [], 0, 0
    for day in result_store.available_trading_days():
        if not (start_day <= day <= end_day):
            continue
        days.append(day)
    # Manifests exist for days with no signals too, so walk the range by
    # date rather than by which days happen to have a signal file.
    for day in _days_in_range(start_day, end_day):
        for manifest in result_store.read_run_manifests(day):
            status = str(manifest.get("run_status") or "UNKNOWN")
            statuses[status] = statuses.get(status, 0) + 1
            label = f"{day} {manifest.get('profile') or 'adhoc'}"
            if status in run_context.FAILURE_STATUSES:
                failed.append(f"{label}: {status}")
            elif status == run_context.PARTIAL:
                partial.append(label)
            elif status == run_context.SKIPPED_MARKET_CLOSED:
                skipped += 1
            if manifest.get("circuit_breaker_triggered"):
                breakers += 1
    return {
        "runs": sum(statuses.values()),
        "statuses": statuses,
        "failed": failed,
        "partial": partial,
        "skipped_market_closed": skipped,
        "circuit_breaker_runs": breakers,
    }


def _days_in_range(start_day: str, end_day: str) -> List[str]:
    try:
        start = date.fromisoformat(start_day)
        end = date.fromisoformat(end_day)
    except (TypeError, ValueError):
        return []
    out, cursor = [], start
    while cursor <= end:
        out.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return out


def format_slack(report: Dict[str, Any], *, run_health: Optional[Dict[str, Any]] = None) -> str:
    """The weekly summary as a Slack message.

    Deliberately NOT `format_report()` with a wrapper. Two differences
    are requirements, not styling:

    * No `best_candidate`/`worst_candidate`. Those name a symbol, and
      track B-2 keeps symbols out of Slack -- a weekly message that
      printed the week's best ticker is a message people trade from,
      which is exactly what month 1 is not for.
    * Run health is included. The file report is read by someone who
      already knows why they opened it; the Slack message is read by
      someone who was not looking, so "4 runs failed" has to be in the
      message rather than one directory away.

    The file report keeps everything, including the candidates. This is
    an additional rendering, not a replacement.
    """
    start, end = report.get("start_day"), report.get("end_day")
    lines = [
        "*📡 Scanner 주간 요약* (Month 1 · 관측 전용)",
        f"기간: {start} ~ {end}",
        f"신호 총계: {report.get('total_signals', 0)}   "
        f"신호 발생 거래일: {len(report.get('trading_days') or [])}   "
        f"hit horizon: {report.get('hit_horizon')}",
    ]

    health = run_health or {}
    if health:
        bits = [f"실행 {health.get('runs', 0)}회"]
        if health.get("failed"):
            bits.append(f"실패 {len(health['failed'])}")
        if health.get("partial"):
            bits.append(f"부분성공 {len(health['partial'])}")
        if health.get("circuit_breaker_runs"):
            bits.append(f"circuit breaker {health['circuit_breaker_runs']}")
        if health.get("skipped_market_closed"):
            bits.append(f"휴장 {health['skipped_market_closed']}")
        lines.append("실행 상태: " + " · ".join(bits))
        for entry in (health.get("failed") or [])[:5]:
            lines.append(f"  ⚠️ {entry}")

    scanners = report.get("scanners") or []
    if not scanners:
        lines.append("")
        lines.append("이번 주 기록된 신호가 없습니다.")
    else:
        lines.append("")
        lines.append("```")
        header = (f"{'scanner':20}{'ver':>6} {'prov':>9} {'n':>5} {'hit%':>7} "
                  f"{'1d':>7} {'3d':>7} {'5d':>7} {'MFE':>7} {'MAE':>7} "
                  f"{'M/M':>6} {'5d성숙':>7}")
        lines.append(header)
        lines.append("-" * len(header))
        for item in scanners:
            maturity = (item.get("maturity") or {}).get("return_5d") or {}
            lines.append(
                f"{str(item.get('scanner_name'))[:20]:20}"
                f"{_short_version(item.get('scanner_version')):>6} "
                f"{str(item.get('market_data_provider') or '-')[:9]:>9} "
                f"{item.get('signal_count', 0):5} {_num(item.get('hit_rate')):>7} "
                f"{_num(item.get('avg_return_1d')):>7} {_num(item.get('avg_return_3d')):>7} "
                f"{_num(item.get('avg_return_5d')):>7} {_num(item.get('avg_mfe')):>7} "
                f"{_num(item.get('avg_mae')):>7} {_num(item.get('mfe_mae_ratio')):>6} "
                f"{_maturity_pct(maturity):>7}")
        lines.append("```")

    for finding in report.get("experiment_splits") or []:
        lines.append(f"⚠️ {format_split_warning(finding)}")

    lines.append("")
    lines.append("종목명 미포함 · 주문 경로 영향 없음 · Candidate Decision: disabled")
    return "\n".join(lines)


def _short_version(version) -> str:
    """`hma_early_trend_v1.0` -> `v1.0`. The scanner name is already in
    the row; repeating it inside the version column wastes the width a
    phone-sized Slack view has."""
    text = str(version or "-")
    marker = text.rfind("_v")
    return text[marker + 1:] if marker != -1 else text[-6:]


def _maturity_pct(maturity: Dict[str, Any]) -> str:
    count = maturity.get("n")
    pct = maturity.get("pct_of_signals")
    if count is None:
        return "-"
    return f"{count}/{_num(pct, 0)}%" if pct is not None else str(count)
