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

#: The five sessions an all-session scanner covers, in clock order. Only
#: S2 is all-session today; S1 is frozen at its measured sessions and
#: S3..S6 are DISCOVERY_ONLY, so neither advertises coverage it does not
#: have. Membership is a fact about the scanner, not about this channel.
ALL_SESSIONS = ("OVERNIGHT", "DAYTIME", "PREMARKET", "REGULAR", "AFTER_HOURS")
ALL_SESSION_SCANNERS = frozenset({"accumulation"})

#: Printed for S3..S6 so a reader never has to infer whether a candidate
#: could have been ordered. DISCOVERY_ONLY means the scanner is
#: structurally unable to reach the order path -- it is the default in
#: `notify_run` precisely so an unmapped scanner is described as inert
#: rather than as live.
MODE_DISCOVERY_ONLY = "DISCOVERY_ONLY"

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


def is_all_session(scanner_name: str) -> bool:
    return str(scanner_name) in ALL_SESSION_SCANNERS


#: Set only in the KIS trading release's environment -- the scanner
#: runtime has no such key, because it deploys from a working checkout
#: rather than an immutable release. That difference is what identifies
#: the two runtimes to each other without anyone hardcoding a path.
TRADING_RUNTIME_MARKER = "DEPLOYED_COMMIT"


def scanner_notifications_owned_here(env=None) -> bool:
    """Whether THIS runtime is the one that announces scanner results.

    The two runtimes share a repository and a webhook, so the same
    `scanners/runner.py` exists in both. Only one of them actually runs
    scanners: the scanner runtime's cron invokes `run_scanners.py`, and
    the trading release's cron never does. The wiring in the release is
    therefore unreachable rather than wrong.

    "Unreachable today" is not a property anyone can see, though, and the
    day someone adds a scanner cron to the release every scan would be
    announced twice by two runtimes that each believed they owned it.
    Duplicated alerts are how a channel stops being trusted, so the rule
    is stated here and enforced rather than left to the crontab.

    Live order events are NOT gated by this -- announcing them is the
    trading runtime's job, and gating both halves on the same flag would
    silence exactly the runtime that owns the half that matters most.
    """
    mapping = _process_env() if env is None else env
    return not str(mapping.get(TRADING_RUNTIME_MARKER) or "").strip()


def _process_env():
    """`os.environ` AFTER the `.env` file has been loaded.

    The webhook lives in `.env`, and in this codebase the module that
    loads it is `slack_utils` -- it calls `load_dotenv()` at import. The
    monitor used to read `os.environ` and return early on an empty
    result, which under cron happens BEFORE anything has imported
    `slack_utils`. The key was present in the file, the code was
    deployed, and every message was dropped with no error: the check that
    was supposed to make the module safe when unconfigured also made it
    silent when it was configured.

    Importing first is what makes the read answer the right question.
    Failure is tolerated -- if `dotenv` or `requests` is missing, the
    process environment on its own is still a valid answer.
    """
    try:
        import slack_utils  # noqa: F401 - imported for its load_dotenv()
    except Exception:  # noqa: BLE001
        logger.debug("scanner monitor: could not preload the env file",
                     exc_info=True)
    return os.environ


def webhook_configured(env=None) -> bool:
    mapping = _process_env() if env is None else env
    return bool(str(mapping.get(WEBHOOK_ENV) or "").strip())


def _send(message: str, *, env=None) -> bool:
    """The only outbound call. Never raises, never blocks a caller.

    The webhook is resolved here (from a caller-supplied mapping, so tests
    never need the real one) and the transport is `slack_utils`, which is
    where every other Slack path in this codebase already goes. Imported
    lazily so a missing `requests` cannot break `import scanners.runner`.
    """
    mapping = _process_env() if env is None else env
    url = str(mapping.get(WEBHOOK_ENV) or "").strip()
    if not url:
        logger.debug("scanner monitor: %s unset, message not sent", WEBHOOK_ENV)
        return False
    try:
        from slack_utils import send_to_webhook

        return bool(send_to_webhook(url, message))
    except Exception:  # noqa: BLE001 - a Slack outage is not a trading failure
        logger.warning("scanner monitor: send failed", exc_info=True)
        return False


def _session_execution_status(session):
    """REFERENCE_VERIFIED / SCAN_ONLY for one of the four clock sessions.

    Returns None for anything else -- a profile name, "RUN", a legacy
    report with no session -- because claiming a verification status for a
    label that is not a session would be inventing one.
    """
    try:
        from scanners.base import scan_session

        if scan_session.normalize(session) is None:
            return None
        return scan_session.execution_status(session)
    except Exception:  # noqa: BLE001
        return None


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
    printed differently: the first is a market observation reported as
    `Candidates: 0` with `Scanner: SUCCESS`, the second carries the
    failure in the `Scanner:` line itself. Collapsing them is how a broken
    scanner hides behind a quiet day, so the count never doubles as the
    health signal.
    """
    tag = scanner_tag(scanner_name)
    lines = [f"[{tag}]", ""]
    if generated_at:
        lines.append(f"Time: {generated_at}")
    lines += [
        f"Session: {session}",
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
    lines.append(f"Scanner: {status}")
    if live_status:
        lines.append(f"Mode: {live_status}")
    if is_all_session(scanner_name):
        lines.append(f"All-session coverage: {' · '.join(ALL_SESSIONS)}")
    # Whether THIS session can place a live order, printed rather than
    # left to be inferred from the fact that a scan happened. A premarket
    # candidate list looks identical to a regular-hours one, and the
    # difference -- that no verified order route exists for it -- is
    # exactly what a reader would otherwise assume away.
    status_line = _session_execution_status(session)
    if status_line:
        lines.append(f"Session execution: {status_line}")

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


#: Scan messages are de-duplicated on their own CONTENT, not on
#: (scanner, session, day). The duplicate this prevents is the real one:
#: cronie has no CRON_TZ, so every ET-guarded entry fires twice from two
#: UTC hours, and a retried or manually re-run scan repeats the same
#: finding. Keying on content means an unchanged result is sent once
#: while a genuinely different result in the same session still gets
#: through -- a day/session key would swallow the second one, which is
#: the message an operator most wants.
#:
#: Lifecycle messages (BUY/FILL/SELL) are deliberately NOT de-duplicated.
#: Two identical-looking fills can be two real fills, and suppressing an
#: order event to save a line is the wrong trade.
_SCAN_SENT: Dict[str, set] = {}


def _scan_is_duplicate(digest: str, trading_day) -> bool:
    """In-process suppression. Best-effort and deliberately not persisted.

    A scan run is a single process, so an in-memory set covers the dual
    firing and the retry within it. Persisting across processes would
    mean a crashed run's state could silence the re-run that replaces
    it, which is worse than an occasional repeat.
    """
    return digest in _SCAN_SENT.get(str(trading_day or "unknown"), set())


def _mark_scan_sent(digest: str, trading_day) -> None:
    """Recorded only after the send SUCCEEDS.

    Marking before would let one Slack outage suppress the retry, turning
    a transient failure into a permanently missing message.

    Only the current trading day is retained. A cron run is one process so
    this is normally a handful of entries, but the watchdog and executor
    are long-lived enough that an unbounded dict would be a slow leak, and
    yesterday's digests can no longer suppress anything useful.
    """
    day = str(trading_day or "unknown")
    if day not in _SCAN_SENT:
        _SCAN_SENT.clear()
    _SCAN_SENT.setdefault(day, set()).add(digest)


def reset_scan_dedup() -> None:
    """Forget what has been sent. For tests, and for a re-run that means it."""
    _SCAN_SENT.clear()


def notify_scan(*, scanner_name, session, trading_day, scanned, candidates,
                status, top=None, live_status=None, generated_at=None,
                env=None) -> bool:
    try:
        message = format_scan(
            scanner_name=scanner_name, session=session, trading_day=trading_day,
            scanned=scanned, candidates=candidates, status=status, top=top,
            live_status=live_status, generated_at=generated_at)
        if _scan_is_duplicate(message, trading_day):
            logger.info("scanner monitor: identical scan message suppressed")
            return False
        if not _send(message, env=env):
            return False
        _mark_scan_sent(message, trading_day)
        return True
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


def notify_run(report, *, env=None) -> int:
    """One monitor message per scanner in `report`. Returns how many sent.

    Unlike the alert channel this reports EVERY run, including the quiet
    ones: "S2 scanned 5,960 and found nothing" is the answer to "why did
    it not trade today", and it is only an answer if it was said.

    A scanner that FAILED is reported with its own status, so a broken
    scanner cannot hide behind a zero-candidate day.
    """
    sent = 0
    try:
        if not scanner_notifications_owned_here(env):
            logger.info("scanner monitor: scan results belong to the scanner "
                        "runtime; this is the trading release, not sending")
            return 0
        from config import scanner_live_mode

        modes = getattr(scanner_live_mode, "SCANNER_LIVE_MODE", {}) or {}
        trading_day = getattr(report, "trading_day", None)
        # The run's real clock session, not its profile name. A profile is
        # a scanner GROUP ("daily", "open"); reporting it under "Session:"
        # meant the line said DAILY where the reader needed REGULAR, and
        # made two runs of the same profile in different sessions
        # indistinguishable. Falls back to the profile only when a caller
        # predates the session field, so an old report still says
        # something rather than nothing.
        session = (getattr(report, "session", None)
                   or getattr(report, "profile", None) or "RUN")
        for outcome in getattr(report, "outcomes", None) or []:
            name = getattr(outcome, "scanner_name", "?")
            signals = list(getattr(outcome, "signals", None) or [])
            ranked = sorted(
                signals,
                key=lambda sig: (-(getattr(sig, "scanner_score", None) or 0.0),
                                 getattr(sig, "symbol", "")))
            top = [{
                "symbol": getattr(sig, "symbol", "?"),
                "score": getattr(sig, "scanner_score", None),
                "price": getattr(sig, "signal_price", None),
                "volume_multiple": (getattr(sig, "metrics", None) or {}).get(
                    "volume_multiple"),
                "vwap": (getattr(sig, "metrics", None) or {}).get("vwap"),
            } for sig in ranked[:TOP_N]]
            status = "FAILED" if getattr(outcome, "failed", False) else "SUCCESS"
            if getattr(outcome, "failure_reason", None):
                status = f"FAILED: {outcome.failure_reason}"
            if notify_scan(scanner_name=name, session=str(session).upper(),
                           trading_day=trading_day,
                           scanned=getattr(outcome, "symbols_seen", None),
                           candidates=len(signals), status=status, top=top,
                           live_status=modes.get(name, MODE_DISCOVERY_ONLY),
                           env=env):
                sent += 1
    except Exception:  # noqa: BLE001 - a monitor must never fail a scan
        logger.warning("scanner monitor: run report failed", exc_info=True)
    return sent


def notify_daily_summary(*, trading_day, rows, minimum_trades_for_winner=1,
                         env=None) -> bool:
    try:
        return _send(format_daily_summary(
            trading_day=trading_day, rows=rows,
            minimum_trades_for_winner=minimum_trades_for_winner), env=env)
    except Exception:  # noqa: BLE001
        logger.warning("scanner monitor: daily summary failed", exc_info=True)
        return False
