"""Turning a watchlist payload into something a person reads.

Three renderings, all from the same payload:

    format_markdown  the file next to the JSON, full detail
    format_console   what the CLI prints
    format_slack     TOP 5 by default, TOP 10 at most

Every rendering carries the same banner. The banner is not decoration:
this list looks exactly like a buy list, is produced by the same
scanners that will one day feed one, and lands in a channel next to
order notifications. The one thing that distinguishes it is that it says
so, on every surface, every time.
"""

from typing import Any, Dict, List, Optional

from watchlist import config

BANNER = "[Manual Watchlist] 수동 검토용 / 자동주문 아님"
FOOTER = "주문 경로 영향 없음 · Candidate Decision: disabled · S1~S6 DISCOVERY_ONLY"


def _num(value, digits: int = 2) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def _scanner_tags(entry: Dict[str, Any]) -> str:
    """`S1+S3` -- short enough for a phone-width Slack line.

    Sorted by scanner NUMBER, not by scanner name. `daily_scanners` is
    stored alphabetically, which would render as "S2+S3+S1" and read as
    though the order meant something.
    """
    order = {name: index for index, name
             in enumerate(config.DAILY_SOURCE_SCANNERS, start=1)}
    numbers = sorted(order[name] for name in (entry.get("daily_scanners") or [])
                     if name in order)
    tags = [f"S{number}" for number in numbers]
    if entry.get("premarket_confirmed"):
        tags.append("S4✓")
    return "+".join(tags) if tags else "-"


def _stage_title(payload: Dict[str, Any]) -> str:
    if payload.get("stage") == config.STAGE_TOMORROW:
        return f"Tomorrow Watchlist — {payload.get('trading_day')} 용"
    return f"Today Watchlist — {payload.get('trading_day')}"


def format_slack(payload: Dict[str, Any], *, top_n: Optional[int] = None) -> str:
    """TOP N only, clamped to the documented ceiling."""
    limit = config.slack_top_n(top_n)
    entries: List[Dict[str, Any]] = list(payload.get("entries") or [])[:limit]

    lines = [
        f"*{BANNER}*",
        _stage_title(payload),
        f"버전: {payload.get('manual_watch_version')}   "
        f"검토 대상: {payload.get('symbols_considered', 0)}종목   "
        f"표시: 상위 {len(entries)}",
    ]
    if payload.get("premarket_confirmations") is not None:
        lines.append(f"프리마켓 확인(S4): {payload.get('premarket_confirmations')}종목")

    if not entries:
        lines.append("")
        lines.append(payload.get("empty_reason") or "오늘 워치리스트에 오른 종목이 없습니다.")
        lines.append("")
        lines.append(FOOTER)
        return "\n".join(lines)

    lines.append("")
    for entry in entries:
        flag = " ⚠️과열" if entry.get("overextended") else ""
        lines.append(
            f"{entry.get('rank')}. *{entry.get('symbol')}*  "
            f"score {_num(entry.get('manual_watch_score'), 1)}  "
            f"[{_scanner_tags(entry)}]  "
            f"scan {_num(entry.get('max_scanner_score'), 0)}{flag}")
        detail = []
        if entry.get("latest_signal_price") is not None:
            detail.append(f"신호가 {_num(entry.get('latest_signal_price'))}")
        if entry.get("extension_hma200_pct") is not None:
            detail.append(f"HMA200 {_num(entry.get('extension_hma200_pct'), 1)}%")
        if entry.get("overextended_reasons"):
            detail.append("과열: " + ", ".join(entry["overextended_reasons"]))
        if detail:
            lines.append("     " + " · ".join(detail))

    lines.append("")
    lines.append(FOOTER)
    return "\n".join(lines)


def format_markdown(payload: Dict[str, Any], *, top_n: Optional[int] = None) -> str:
    """The file rendering. Carries more than Slack does, on purpose."""
    limit = top_n if top_n is not None else config.FILE_TOP_N
    entries = list(payload.get("entries") or [])[:limit]

    lines = [
        f"# {_stage_title(payload)}",
        "",
        f"> {BANNER}",
        "",
        f"- 생성: {payload.get('generated_at')}",
        f"- 버전: {payload.get('manual_watch_version')}",
        f"- 원본 세션: {payload.get('source_session_day') or '-'}",
        f"- 검토 대상: {payload.get('symbols_considered', 0)}종목"
        + (f" (상위 {len(entries)} 표시)" if entries else ""),
    ]
    if payload.get("truncated_from"):
        lines.append(f"- 저장 시 절삭: {payload['truncated_from']}종목 중 "
                     f"{len(payload.get('entries') or [])}종목 보관")
    lines.append("")

    if not entries:
        lines.append(payload.get("empty_reason") or "해당 없음.")
        lines.append("")
        lines.append(FOOTER)
        return "\n".join(lines)

    lines.append("| # | 심볼 | manual_watch_score | scanner | max scan | 신호가 | HMA200 ext | 과열 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for entry in entries:
        lines.append(
            f"| {entry.get('rank')} | {entry.get('symbol')} "
            f"| {_num(entry.get('manual_watch_score'), 1)} "
            f"| {_scanner_tags(entry)} "
            f"| {_num(entry.get('max_scanner_score'), 0)} "
            f"| {_num(entry.get('latest_signal_price'))} "
            f"| {_num(entry.get('extension_hma200_pct'), 1)}% "
            f"| {'예' if entry.get('overextended') else '-'} |")

    lines.append("")
    lines.append("## 근거")
    lines.append("")
    for entry in entries:
        lines.append(f"### {entry.get('rank')}. {entry.get('symbol')}")
        components = entry.get("components") or {}
        lines.append("- 점수 구성: " + ", ".join(
            f"{key}={_num(value, 1)}" for key, value in sorted(components.items())))
        if entry.get("overextended_reasons"):
            lines.append("- 과열 표시: " + ", ".join(entry["overextended_reasons"]))
        if entry.get("intraday_observed"):
            lines.append("- 장중 관측(S5/S6): " + ", ".join(entry["intraday_observed"]))
        for reason in entry.get("reasons") or []:
            lines.append(f"- {reason}")
        lines.append("")

    lines.append(FOOTER)
    return "\n".join(lines)


def format_console(payload: Dict[str, Any], *, top_n: Optional[int] = None) -> str:
    limit = top_n if top_n is not None else config.FILE_TOP_N
    entries = list(payload.get("entries") or [])[:limit]
    lines = [
        BANNER,
        _stage_title(payload),
        f"version={payload.get('manual_watch_version')} "
        f"considered={payload.get('symbols_considered', 0)}",
        "",
    ]
    if not entries:
        lines.append(payload.get("empty_reason") or "(empty)")
        return "\n".join(lines)
    header = f"{'#':>3} {'symbol':10} {'score':>8} {'scanners':12} {'scan':>6} {'ext200':>8} over"
    lines.append(header)
    lines.append("-" * len(header))
    for entry in entries:
        lines.append(
            f"{entry.get('rank'):>3} {str(entry.get('symbol'))[:10]:10} "
            f"{_num(entry.get('manual_watch_score'), 1):>8} "
            f"{_scanner_tags(entry):12} "
            f"{_num(entry.get('max_scanner_score'), 0):>6} "
            f"{_num(entry.get('extension_hma200_pct'), 1):>8} "
            f"{'YES' if entry.get('overextended') else '-'}")
    return "\n".join(lines)
