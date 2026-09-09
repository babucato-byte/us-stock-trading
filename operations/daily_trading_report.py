"""The daily trading report for sotck-trading-report. Read-only.

Source of truth is the position book, not the notification stream:

* S6  `s6_positions` -- one row per intended entry. A row with an
      entry price is a filled BUY; a CLOSED row with an exit price is a
      filled SELL; a CLOSED row without an entry price is a cancelled
      or never-filled BUY (BUY_FILL_TTL_EXPIRED, BUY_NEVER_FILLED).
* S1  `s1_live_trades` -- one row per trade with entry/exit fill times.

Counting from the book means a partial fill, a cancelled order, a retry
or a duplicated audit event cannot inflate the numbers: each position is
one row however many events produced it.

PnL is gross (exit - entry) * quantity in USD; fees are not in the
book. Returns are exit/entry - 1 per closed position; the average is the
simple mean across closed positions.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CANCEL_REASONS = ("BUY_FILL_TTL_EXPIRED", "BUY_NEVER_FILLED")


def _num(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _day_of(stamp) -> Optional[str]:
    if not stamp:
        return None
    return str(stamp)[:10]


def _s6_rows(conn, trading_day: str) -> List[Dict[str, Any]]:
    keys = ["position_id", "symbol", "status", "quantity", "entry_price", "exit_price",
            "exit_reason", "entry_session", "exit_session", "created_at", "closed_at",
            "range_minutes"]
    optional = [c for c in ("scanner_variant",) if c in _s6_row_fields(conn)]
    try:
        rows = conn.execute(
            "SELECT " + ", ".join(keys + optional) + " "
            "FROM s6_positions WHERE substr(created_at, 1, 10) = ? "
            "OR substr(closed_at, 1, 10) = ? OR status != 'CLOSED'",
            (trading_day, trading_day)).fetchall()
    except Exception:  # noqa: BLE001
        logger.warning("s6_positions unavailable", exc_info=True)
        return []
    return [dict(zip(keys + optional, row)) for row in rows]


def _s1_rows(conn, trading_day: str) -> List[Dict[str, Any]]:
    keys = ("trade_id", "trading_day", "entry_filled_at", "exit_filled_at",
            "entry_price", "exit_price", "qty", "gross_pnl", "exit_reason")
    try:
        rows = conn.execute(
            "SELECT trade_id, trading_day, entry_filled_at, exit_filled_at, entry_price, "
            "exit_price, qty, gross_pnl, exit_reason FROM s1_live_trades "
            "WHERE trading_day = ? OR substr(exit_filled_at, 1, 10) = ?",
            (trading_day, trading_day)).fetchall()
    except Exception:  # noqa: BLE001
        logger.warning("s1_live_trades unavailable", exc_info=True)
        return []
    return [dict(zip(keys, row)) for row in rows]


def _closed_trade(symbol, quantity, entry, exit_price, reason, strategy, session=None):
    qty = int(quantity or 0)
    pnl = (exit_price - entry) * qty
    ret = (exit_price / entry - 1.0) * 100.0 if entry else None
    return {"symbol": symbol, "strategy": strategy, "quantity": qty,
            "entry_price": entry, "exit_price": exit_price, "pnl": pnl,
            "return_pct": ret, "reason": reason,
            "session": str(session).upper() if session else None}


def _s6_row_fields(conn) -> List[str]:
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(s6_positions)")]
    except Exception:  # noqa: BLE001
        return []


def _s6_premarket_section(conn, trading_day: str, closed: List[Dict[str, Any]],
                          s6_rows: List[Dict[str, Any]], *, env=None) -> Dict[str, Any]:
    """The compact S6 PREMARKET block: the day's premarket book, how many
    entries were ORB5, how many candidates the entry-quality gate stopped
    (from the shadow signal log), and what the ORB15 shadow saw (from the
    range-shadow log). Shadow rows are never counted as fills. Every
    figure is measured or absent."""
    premarket = [r for r in s6_rows if str(r.get("entry_session") or "").upper() == "PREMARKET"
                 and _day_of(r.get("created_at")) == trading_day]
    filled = [r for r in premarket if _num(r.get("entry_price")) is not None]
    closed_pm = [t for t in closed if t.get("strategy") == "S6"
                 and t.get("session") == "PREMARKET"]
    wins = [t for t in closed_pm if t["pnl"] > 0]
    losses = [t for t in closed_pm if t["pnl"] < 0]
    returns = [t["return_pct"] for t in closed_pm if t["return_pct"] is not None]
    orb5 = [r for r in filled if r.get("range_minutes") == 5]
    variants: Dict[str, int] = {}
    for row in filled:
        key = str(row.get("scanner_variant") or f"S6_ORB{row.get('range_minutes')}")
        variants[key] = variants.get(key, 0) + 1
    section: Dict[str, Any] = {
        "buys": len(filled), "sells": len(closed_pm),
        "wins": len(wins), "losses": len(losses),
        "realized_pnl": sum(t["pnl"] for t in closed_pm) if closed_pm else 0.0,
        "avg_return": (sum(returns) / len(returns)) if returns else None,
        "orb5_entries": len(orb5), "entries_by_variant": variants,
        "quality_blocks": None, "quality_block_codes": {},
        "orb15_shadow_opportunities": None, "orb15_shadow_ready_symbols": [],
        "outcomes": None,
    }
    try:
        from s6_live import shadow_signal_log as ssl

        rows = ssl.read(trading_day, env=env) if hasattr(ssl, "read") else []
        blocked = {}
        for row in rows or []:
            if row.get("first_blocked_by") == "ENTRY_QUALITY" and row.get("entry_quality_reason"):
                blocked.setdefault((row.get("symbol"), row.get("entry_quality_reason")), 1)
        if rows:
            section["quality_blocks"] = len(blocked)
            codes: Dict[str, int] = {}
            for (_symbol, code) in blocked:
                codes[code] = codes.get(code, 0) + 1
            section["quality_block_codes"] = codes
    except Exception:  # noqa: BLE001
        logger.debug("shadow signal log unavailable", exc_info=True)
    try:
        from s6_live import range_shadow

        rows = range_shadow.read(trading_day, env=env)
        if rows:
            ready = range_shadow.first_ready(rows)
            section["orb15_shadow_opportunities"] = len(ready)
            section["orb15_shadow_ready_symbols"] = sorted(ready)
    except Exception:  # noqa: BLE001
        logger.debug("range shadow log unavailable", exc_info=True)
    try:
        from s6_live import entry_outcomes

        summary = entry_outcomes.summarise(entry_outcomes.read(trading_day, env=env))
        if summary:
            section["outcomes"] = summary
    except Exception:  # noqa: BLE001
        logger.debug("entry outcomes unavailable", exc_info=True)
    return section


def build(conn, trading_day: str, *, now=None, env=None) -> Dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    s6 = _s6_rows(conn, trading_day)
    s1 = _s1_rows(conn, trading_day)

    buy_fills = 0
    cancelled = 0
    closed: List[Dict[str, Any]] = []
    open_positions = 0
    for row in s6:
        entry = _num(row.get("entry_price"))
        created_today = _day_of(row.get("created_at")) == trading_day
        closed_today = _day_of(row.get("closed_at")) == trading_day
        if row.get("status") != "CLOSED":
            open_positions += 1
        if created_today and entry is not None:
            buy_fills += 1
        if created_today and entry is None and row.get("status") == "CLOSED":
            cancelled += 1
        exit_price = _num(row.get("exit_price"))
        if closed_today and entry is not None and exit_price is not None:
            closed.append(_closed_trade(row["symbol"], row.get("quantity"), entry,
                                        exit_price, row.get("exit_reason"), "S6",
                                        session=row.get("entry_session")))

    s1_buys = 0
    for row in s1:
        if row.get("entry_filled_at") and str(row.get("trading_day")) == trading_day:
            s1_buys += 1
        if row.get("exit_filled_at") and _day_of(row.get("exit_filled_at")) == trading_day:
            entry, exit_price = _num(row.get("entry_price")), _num(row.get("exit_price"))
            if entry is not None and exit_price is not None:
                closed.append(_closed_trade(row.get("trade_id"), row.get("qty"), entry,
                                            exit_price, row.get("exit_reason"), "S1"))
        elif not row.get("exit_filled_at"):
            open_positions += 1 if row.get("entry_filled_at") else 0

    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] < 0]
    returns = [t["return_pct"] for t in closed if t["return_pct"] is not None]
    by_strategy: Dict[str, Dict[str, Any]] = {}
    for trade in closed:
        bucket = by_strategy.setdefault(trade["strategy"], {"closed": 0, "wins": 0,
                                                            "losses": 0, "pnl": 0.0})
        bucket["closed"] += 1
        bucket["pnl"] += trade["pnl"]
        if trade["pnl"] > 0:
            bucket["wins"] += 1
        elif trade["pnl"] < 0:
            bucket["losses"] += 1
    if buy_fills:
        by_strategy.setdefault("S6", {"closed": 0, "wins": 0, "losses": 0, "pnl": 0.0})
        by_strategy["S6"]["buys"] = buy_fills
    if s1_buys:
        by_strategy.setdefault("S1", {"closed": 0, "wins": 0, "losses": 0, "pnl": 0.0})
        by_strategy["S1"]["buys"] = s1_buys

    return {
        "trading_day": trading_day,
        "generated_at": current.isoformat(),
        "buy_fills": buy_fills + s1_buys,
        "sell_fills": len(closed),
        "cancelled_entries": cancelled,
        "open_positions": open_positions,
        "wins": len(wins), "losses": len(losses),
        "win_rate": (len(wins) / len(closed) * 100.0) if closed else None,
        "realized_pnl": sum(t["pnl"] for t in closed) if closed else 0.0,
        "avg_return": (sum(returns) / len(returns)) if returns else None,
        "best": max(closed, key=lambda t: t["pnl"]) if closed else None,
        "worst": min(closed, key=lambda t: t["pnl"]) if closed else None,
        "by_strategy": by_strategy,
        "closed": closed,
        "s6_premarket": _s6_premarket_section(conn, trading_day, closed, s6, env=env),
    }


def _money(value) -> str:
    return "-" if value is None else f"${value:,.2f}"


def _pct(value) -> str:
    return "-" if value is None else f"{value:+.2f}%"


def format_message(report: Dict[str, Any]) -> str:
    from operations import slack_presentation as sp

    lines = ["[일일 거래 리포트]", str(report["trading_day"]), "",
             f"매수 체결: {report['buy_fills']}건",
             f"매도 체결: {report['sell_fills']}건",
             f"미체결 취소: {report['cancelled_entries']}건",
             f"보유 포지션: {report['open_positions']}개", "",
             f"승/패: {report['wins']}/{report['losses']}",
             f"승률: {_pct(report['win_rate']) if report['win_rate'] is not None else '-'}".replace("+", ""),
             f"실현손익: {_money(report['realized_pnl'])}",
             f"평균 수익률: {_pct(report['avg_return'])}"]
    best, worst = report.get("best"), report.get("worst")
    if best:
        lines.append(f"최고 거래: {best['symbol']} {_pct(best['return_pct'])} ({_money(best['pnl'])})")
    if worst:
        lines.append(f"최저 거래: {worst['symbol']} {_pct(worst['return_pct'])} ({_money(worst['pnl'])})")
    if report.get("by_strategy"):
        lines += ["", "전략별:"]
        for name, bucket in sorted(report["by_strategy"].items()):
            lines.append(f"  {name}: 매수 {bucket.get('buys', 0)} · 청산 {bucket['closed']} · "
                         f"승 {bucket['wins']} / 패 {bucket['losses']} · 손익 {_money(bucket['pnl'])}")
    pm = report.get("s6_premarket") or {}
    if pm and (pm.get("buys") or pm.get("sells") or pm.get("quality_blocks")
               or pm.get("orb15_shadow_opportunities")):
        lines += ["", "S6 프리장",
                  f"- 매수: {pm.get('buys', 0)}건",
                  f"- 매도: {pm.get('sells', 0)}건",
                  f"- 승/패: {pm.get('wins', 0)}/{pm.get('losses', 0)}",
                  f"- 실현손익: {_money(pm.get('realized_pnl'))}",
                  f"- 평균 수익률: {_pct(pm.get('avg_return'))}",
                  f"- ORB5 진입: {pm.get('orb5_entries', 0)}건"]
        if pm.get("quality_blocks") is not None:
            codes = pm.get("quality_block_codes") or {}
            detail = ", ".join(f"{sp.reason_label(c)[0]} {n}" for c, n in sorted(codes.items()))
            lines.append(f"- 진입 품질 차단: {pm['quality_blocks']}건" + (f" ({detail})" if detail else ""))
        else:
            lines.append("- 진입 품질 차단: 데이터 없음")
        if pm.get("orb15_shadow_opportunities") is not None:
            lines.append(f"- ORB15 관찰 기회: {pm['orb15_shadow_opportunities']}건")
        outcomes = pm.get("outcomes") or {}
        for variant, stats in sorted(outcomes.items()):
            lines.append(
                f"- {variant}: {stats.get('count', 0)}건 · MFE15 {_pct(stats.get('avg_mfe_15m'))} "
                f"· MAE15 {_pct(stats.get('avg_mae_15m'))} · MFE60 {_pct(stats.get('avg_mfe_60m'))} "
                f"· MAE60 {_pct(stats.get('avg_mae_60m'))}")
    if report.get("closed"):
        lines += ["", "청산 내역:"]
        for trade in report["closed"]:
            label, code = sp.reason_label(trade.get("reason"))
            lines.append(f"  {trade['symbol']} {trade['quantity']}주 "
                         f"{_money(trade['entry_price'])} → {_money(trade['exit_price'])} "
                         f"{_pct(trade['return_pct'])} · {label} ({code})")
    return "\n".join(lines)
