"""One channel that shows what every scanner is doing -- #scanner-monitor.

Why this is not `scanners/notify/slack.py`
------------------------------------------
That module is the ALERT channel, and it is deliberately failure-only and
deliberately carries no ticker: "a channel that occasionally prints
tickers becomes a channel people trade from". That discipline is what
keeps its signal-to-noise worth waking someone for, and it is left
untouched here.

This is a different job. A monitor exists to be read routinely -- what
ran, what it found, what was bought and sold -- and that requires exactly
the symbols and scores the alert channel refuses to print. So it is a
separate module on a separate webhook, and neither one's policy leaks
into the other. The alert channel keeps alerting; this one narrates.

Fail-closed and silent when unconfigured
----------------------------------------
Without `SCANNER_MONITOR_SLACK_WEBHOOK_URL` nothing is sent and nothing
raises. That makes the code deployable before the channel exists: every
call site is live, the formatting is exercised by tests, and the day the
webhook is set the messages start flowing with no further change.

Nothing here can break a scan or an order. Every entry point catches
everything: a Slack outage is not a trading failure, and a monitor that
can abort the thing it monitors is worse than no monitor.
"""

import logging
import os
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

WEBHOOK_ENV = "SCANNER_MONITOR_SLACK_WEBHOOK_URL"

#: One tag per source, so a reader filters by eye. The scanner tags map
#: from the scanner_name the runner already uses -- a scanner absent from
#: this table is still reported, under its own name, rather than dropped.
SCANNER_TAGS = {
    "hma_early_trend": "S1 HMA",
    "accumulation": "S2 VOLUME",
    "breakout_ready": "S3 BREAKOUT",
    "premarket_momentum": "S4 PREMARKET",
    "gap_pullback": "S5 GAP",
    "orb": "S6 ORB",
}

TAG_LIVE_BUY = "LIVE BUY"
TAG_LIVE_FILL = "LIVE FILL"
TAG_LIVE_SELL = "LIVE SELL"
TAG_RISK = "RISK"
TAG_WATCHDOG = "WATCHDOG"
TAG_RECONCILIATION = "RECONCILIATION"
TAG_DAILY_SUMMARY = "DAILY SUMMARY"

#: How many ranked candidates a scan message lists. The rest are counted,
#: not printed: a monitor that pastes 300 tickers is not read.
TOP_N = 3

#: Said rather than guessed when a comparison has too little behind it.
INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"


def scanner_tag(scanner_name: str) -> str:
    return SCANNER_TAGS.get(str(scanner_name), str(scanner_name).upper())


def webhook_configured(env=None) -> bool:
    mapping = os.environ if env is None else env
    return bool(str(mapping.get(WEBHOOK_ENV) or "").strip())


def _send(message: str, *, env=None) -> bool:
    """The only outbound call. Never raises, never blocks a caller."""
    mapping = os.environ if env is None else env
    url = str(mapping.get(WEBHOOK_ENV) or "").strip()
    if not url:
        logger.debug("scanner monitor: %s unset, message not sent", WEBHOOK_ENV)
        return False
    try:
        import requests

        response = requests.post(url, json={"text": message}, timeout=10)
        return 200 <= response.status_code < 300
    except Exception:  # noqa: BLE001 - a Slack outage is not a trading failure
        logger.warning("scanner monitor: send failed", exc_info=True)
        return False


def _fmt(value, digits=2, dash="-"):
    if value is None:
        return dash
    if isinstance(value, str):
        return value
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


# --- scanner runs --------------------------------------------------------

def format_scan(*, scanner_name, session, trading_day, scanned, candidates,
                status, top=None, live_status=None, generated_at=None) -> str:
    """A scan result, including the zero-candidate case.

    Zero candidates and a failed scanner are different facts and are
    printed differently: the first is a market observation, the second is
    an operational one, and collapsing them is how a broken scanner hides
    behind a quiet day.
    """
    tag = scanner_tag(scanner_name)
    lines = [f"[SCANNER {tag} · {session}]", ""]
    if generated_at:
        lines.append(f"Time: {generated_at}")
    lines += [
        f"Trading day: {trading_day}",
        f"Scanned: {scanned if scanned is not None else '-'}",
        f"Candidates: {candidates if candidates is not None else '-'}",
        "",
    ]
    ranked = list(top or [])[:TOP_N]
    if ranked:
        for index, item in enumerate(ranked, start=1):
            lines.append(f"#{index} {item.get('symbol', '?'):<6} "
                         f"score {_fmt(item.get('score'))}")
        lines.append("")
    lines.append(f"Status: {status}")
    if live_status:
        lines.append(f"Live: {live_status}")

    if ranked:
        best = ranked[0]
        details = [("volume multiple", best.get("volume_multiple")),
                   ("price", best.get("price")),
                   ("VWAP", best.get("vwap")),
                   ("session", session)]
        shown = [f"  {label}: {_fmt(value)}" for label, value in details
                 if value is not None]
        if shown:
            lines += ["", f"Top candidate: {best.get('symbol', '?')}"] + shown
    return "\n".join(lines)


def notify_scan(*, scanner_name, session, trading_day, scanned, candidates,
                status, top=None, live_status=None, generated_at=None,
                env=None) -> bool:
    try:
        return _send(format_scan(
            scanner_name=scanner_name, session=session, trading_day=trading_day,
            scanned=scanned, candidates=candidates, status=status, top=top,
            live_status=live_status, generated_at=generated_at), env=env)
    except Exception:  # noqa: BLE001
        logger.warning("scanner monitor: scan message failed", exc_info=True)
        return False


# --- live order lifecycle -------------------------------------------------

def format_buy(*, strategy, symbol, session, qty, limit_price, order_id,
               status="ACCEPTED", rank=None) -> str:
    lines = [f"[{TAG_LIVE_BUY} · {strategy}]", "",
             f"Symbol: {symbol}"]
    if rank is not None:
        lines.append(f"Rank: {rank}")
    lines += [
        f"Session: {session}",
        f"Qty: {qty}",
        f"Limit: {_fmt(limit_price, 4)}",
        f"Order ID: {order_id}",
        f"Status: {status}",
    ]
    return "\n".join(lines)


def format_fill(*, strategy, symbol, qty, average_fill_price, position_id) -> str:
    return "\n".join([
        f"[{TAG_LIVE_FILL} · {strategy}]", "",
        f"Symbol: {symbol}",
        f"Qty: {qty}",
        f"Avg fill: {_fmt(average_fill_price, 4)}",
        f"Position ID: {position_id}",
    ])


def format_sell(*, strategy, symbol, reason, qty, average_entry, average_sell,
                realized_pnl=None, holding_time=None) -> str:
    lines = [f"[{TAG_LIVE_SELL} · {strategy}]", "",
             f"Symbol: {symbol}",
             f"Reason: {reason}",
             f"Qty: {qty}",
             f"Avg entry: {_fmt(average_entry, 4)}",
             f"Avg sell: {_fmt(average_sell, 4)}"]
    # PnL is printed only when it is known. A fee-inclusive realised
    # number is not available until settlement, and printing a
    # gross figure labelled "Realized PnL" would be a claim the ledger
    # cannot support.
    lines.append(f"Realized PnL: {_fmt(realized_pnl) if realized_pnl is not None else 'PENDING_SETTLEMENT'}")
    if holding_time:
        lines.append(f"Holding time: {holding_time}")
    return "\n".join(lines)


def notify_buy(**kwargs) -> bool:
    env = kwargs.pop("env", None)
    try:
        return _send(format_buy(**kwargs), env=env)
    except Exception:  # noqa: BLE001
        logger.warning("scanner monitor: buy message failed", exc_info=True)
        return False


def notify_fill(**kwargs) -> bool:
    env = kwargs.pop("env", None)
    try:
        return _send(format_fill(**kwargs), env=env)
    except Exception:  # noqa: BLE001
        logger.warning("scanner monitor: fill message failed", exc_info=True)
        return False


def notify_sell(**kwargs) -> bool:
    env = kwargs.pop("env", None)
    try:
        return _send(format_sell(**kwargs), env=env)
    except Exception:  # noqa: BLE001
        logger.warning("scanner monitor: sell message failed", exc_info=True)
        return False


# --- operational tags -----------------------------------------------------

def notify_tagged(tag: str, body: str, *, env=None) -> bool:
    try:
        return _send(f"[{tag}]\n\n{body}", env=env)
    except Exception:  # noqa: BLE001
        logger.warning("scanner monitor: tagged message failed", exc_info=True)
        return False


# --- daily summary --------------------------------------------------------

def _leader(rows: List[Dict[str, Any]], key, *, minimum_trades=0, highest=True):
    """The best row by `key`, or INSUFFICIENT_SAMPLE.

    A winner is only declared when something was actually measured. With
    four trading days behind the dataset, naming "today's best scanner"
    from one candidate would be arithmetic dressed as a finding.
    """
    usable = [r for r in rows
              if r.get(key) is not None and (r.get("trades") or 0) >= minimum_trades]
    if not usable:
        return INSUFFICIENT_SAMPLE
    best = (max if highest else min)(usable, key=lambda r: r[key])
    return f"{best.get('label', '?')} ({_fmt(best[key])})"


def format_daily_summary(*, trading_day, rows: Iterable[Dict[str, Any]],
                         minimum_trades_for_winner: int = 1) -> str:
    rows = list(rows)
    lines = [f"[{TAG_DAILY_SUMMARY}]", "", f"Trading day: {trading_day}", ""]
    for row in rows:
        lines.append(f"{row.get('label', '?')}")
        lines.append(f"  candidates: {row.get('candidates', '-')}")
        for label, key in (("live opportunities", "live_opportunities"),
                           ("trades", "trades"),
                           ("avg holding", "avg_holding"),
                           ("best MFE", "best_mfe"),
                           ("avg MFE", "avg_mfe"),
                           ("avg MAE", "avg_mae"),
                           ("PnL", "pnl"),
                           ("live result", "live_result")):
            if row.get(key) is not None:
                lines.append(f"  {label}: {_fmt(row[key])}")
        lines.append("")
    lines += [
        "Best scanner today: " + _leader(rows, "avg_mfe",
                                         minimum_trades=minimum_trades_for_winner),
        "Most opportunities: " + _leader(rows, "candidates"),
        "Best MFE: " + _leader(rows, "best_mfe"),
        "Lowest MAE: " + _leader(rows, "avg_mae", highest=False),
        "Highest turnover: " + _leader(rows, "trades",
                                       minimum_trades=minimum_trades_for_winner),
    ]
    return "\n".join(lines)


def notify_daily_summary(*, trading_day, rows, minimum_trades_for_winner=1,
                         env=None) -> bool:
    try:
        return _send(format_daily_summary(
            trading_day=trading_day, rows=rows,
            minimum_trades_for_winner=minimum_trades_for_winner), env=env)
    except Exception:  # noqa: BLE001
        logger.warning("scanner monitor: daily summary failed", exc_info=True)
        return False
