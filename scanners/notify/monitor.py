"""One channel that shows what every scanner is doing -- #stock-scanner.

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

from scanners.notify import labels
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

def _scan_sessions():
    """The session vocabulary, taken from the module that defines it.

    This list used to be written out here, and it named OVERNIGHT and
    DAYTIME separately -- two names `scan_session.normalize()` rejects,
    because the venue treats that window as one bucket. The message
    therefore advertised coverage of sessions no scan could ever be
    labelled with. One vocabulary, defined once, or the channel and the
    code disagree about what a session is.
    """
    try:
        from scanners.base import scan_session

        return tuple(scan_session.SESSIONS)
    except Exception:  # noqa: BLE001
        return ()


#: The sessions an all-session scanner covers, in clock order. Only S2 is
#: all-session today; S1 is frozen at its measured sessions and S3..S6 are
#: DISCOVERY_ONLY, so neither advertises coverage it does not have.
#: Membership is a fact about the scanner, not about this channel.
ALL_SESSIONS = _scan_sessions()
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


def _data_source_lines(session):
    """What is actually feeding this session's features.

    Printed because the previous silence was read as absence. Premarket
    and after-hours carried no volume line, and an operator reasonably
    concluded there was no volume -- which was true of the daily provider
    and never true of the market. Now that the trade stream supplies it,
    saying so is the only way the message stops implying the old answer.
    """
    try:
        from s6_live import realtime_features as rf
        from scanners.base import scan_session

        resolved = scan_session.normalize(session)
        if resolved is None or resolved not in rf.KIS_AUTHORITATIVE_SESSIONS:
            return []
        return [f"{labels.field('Data source')}: {rf.KIS_STREAM_SOURCE}",
                f"{labels.field('Volume')}: AVAILABLE"]
    except Exception:  # noqa: BLE001 - a missing provenance line must not
        # cost the report the counts it exists to carry.
        return []


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
                status, top=None, live_status=None, generated_at=None,
                variant=None, live_candidates=None) -> str:
    """A scan result, including the zero-candidate case.

    Zero candidates and a failed scanner are different facts and are
    printed differently: the first is a market observation reported as
    `Candidates: 0` with `Scanner: SUCCESS`, the second carries the
    failure in the `Scanner:` line itself. Collapsing them is how a broken
    scanner hides behind a quiet day, so the count never doubles as the
    health signal.
    """
    lines = [f"[{labels.scanner(scanner_name, variant=variant)}]", ""]
    if generated_at:
        lines.append(f"{labels.field('Generated at')}: "
                     f"{labels.dual_time(generated_at)}")
    lines += [
        f"{labels.field('Status')}: {labels.status(status)}",
        f"{labels.field('Session')}: {labels.session(session)}",
        f"{labels.field('Trading day')}: {trading_day}",
        f"{labels.field('Scanned')}: "
        f"{f'{scanned:,}' if isinstance(scanned, int) else '-'}",
        # 0 and "-" mean different things and always will: 0 is a
        # completed scan that found nothing, "-" is a scan that never
        # completed. See the empty-result line below.
        f"{labels.field('Candidates')}: "
        f"{candidates if candidates is not None else '-'}",
    ]
    # The two counts are separated because they answered the same
    # question differently on 2026-08-21: S6 found one candidate and it
    # was an ETP, so "후보 수: 1" read as an opportunity while the number
    # an operator could act on was zero.
    lines += _data_source_lines(session)
    if live_candidates is not None:
        lines.append(f"실거래 가능 후보: {live_candidates}")
        if candidates and not live_candidates:
            # Says which filter removed them. "연구용만" read as a
            # statement about the scan's purpose -- research rather than
            # trading -- when the scan is live and it was the instrument
            # type that disqualified every candidate.
            lines.append("  (후보는 있으나 전부 COMMON_STOCK 아님 — 실거래 대상 없음)")
    lines.append("")
    ranked = list(top or [])[:TOP_N]
    if ranked:
        for index, item in enumerate(ranked, start=1):
            lines.append(f"{index}위 {item.get('symbol', '?'):<6} "
                         f"{labels.field('Score')} {_fmt(item.get('score'))}")
        lines.append("")
    elif candidates == 0:
        # Said in words as well as in the count. "후보 수: 0" with no
        # sentence reads like a truncated message; the sentence is what
        # makes a quiet day unmistakably a RESULT rather than an absence.
        lines.append("결과: 조건을 충족한 종목이 없습니다.")
        lines.append("")
    if live_status:
        lines.append(f"{labels.field('Mode')}: {labels.status(live_status)}")
    if is_all_session(scanner_name):
        covered = " · ".join(labels.session(name, with_code=False)
                             for name in ALL_SESSIONS)
        lines.append(f"전 세션 스캔: {covered}")
    # Whether THIS session can place a live order, printed rather than
    # left to be inferred from the fact that a scan happened. A premarket
    # candidate list looks identical to a regular-hours one, and the
    # difference -- that no verified order route exists for it -- is
    # exactly what a reader would otherwise assume away.
    status_line = _session_execution_status(session)
    if status_line:
        lines.append(f"{labels.field('Session execution')}: "
                     f"{labels.status(status_line)}")

    if ranked:
        best = ranked[0]
        eligible_note = best.get("security_type")
        details = [(labels.field("Volume Multiple"), best.get("volume_multiple")),
                   ("거래량 확장", best.get("volume_expansion")),
                   (labels.field("Price"), best.get("price")),
                   (labels.field("VWAP"), best.get("vwap")),
                   ("EMA9", best.get("ema9")), ("EMA21", best.get("ema21")),
                   ("Range 상단", best.get("range_high")),
                   ("Range 하단", best.get("range_low"))]
        shown = [f"  {label}: {_fmt(value)}" for label, value in details
                 if value is not None]
        if shown:
            # Named "실거래 가능 상위 후보" only when the list it came
            # from was the eligible one -- otherwise an operator reads a
            # research symbol as something they could buy.
            # Three states, not two. A caller that passed NO live count
            # (S1, S2) keeps the original heading; only a caller that
            # measured eligibility gets the qualified wording, and only
            # a measured ZERO is labelled research.
            if live_candidates is None:
                heading = "상위 후보"
            elif live_candidates:
                heading = "실거래 가능 상위 후보"
            else:
                heading = "상위 후보 (연구용)"
            label = f"{heading}: {best.get('symbol', '?')}"
            if eligible_note:
                label += f" [{eligible_note}]"
            lines += ["", label] + shown
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
                variant=None, live_candidates=None, env=None) -> bool:
    try:
        message = format_scan(
            scanner_name=scanner_name, session=session, trading_day=trading_day,
            scanned=scanned, candidates=candidates, status=status, top=top,
            live_status=live_status, generated_at=generated_at,
            variant=variant, live_candidates=live_candidates)
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
    lines = [f"[{labels.tag(TAG_LIVE_BUY)} · {_strategy_number(strategy)}]", "",
             f"전략: {labels.strategy(strategy)}",
             f"{labels.field('Symbol')}: {symbol}"]
    if rank is not None:
        lines.append(f"{labels.field('Rank')}: {rank}")
    lines += [
        f"{labels.field('Session')}: {labels.session(session)}",
        f"{labels.field('Quantity')}: {qty}주",
        f"주문가: {_fmt(limit_price, 4)}",
        f"{labels.field('Order ID')}: {order_id}",
        f"{labels.field('Status')}: {labels.status(status)}",
    ]
    return "\n".join(lines)


def format_fill(*, strategy, symbol, qty, average_fill_price, position_id,
                side=None, position_state=None) -> str:
    """A fill, tagged by the ACTUAL side.

    Not by the event name: one fill event carries both directions, and
    labelling a sell "매수 체결" would make the channel lie about a real
    order. An unknown side degrades to the neutral "체결".
    """
    lines = [f"[{labels.fill_tag(side)} · {_strategy_number(strategy)}]", "",
             f"전략: {labels.strategy(strategy)}",
             f"{labels.field('Symbol')}: {symbol}",
             f"{labels.field('Quantity')}: {qty}주",
             f"{labels.field('Avg Fill')}: {_fmt(average_fill_price, 4)}"]
    if position_state:
        lines.append(f"{labels.field('Position')}: {position_state}")
    lines.append(f"포지션 ID: {position_id}")
    return "\n".join(lines)


def format_sell(*, strategy, symbol, reason, qty, average_entry, average_sell,
                realized_pnl=None, holding_time=None) -> str:
    lines = [f"[{labels.tag(TAG_LIVE_SELL)} · {_strategy_number(strategy)}]", "",
             f"전략: {labels.strategy(strategy)}",
             f"{labels.field('Symbol')}: {symbol}",
             f"매도 사유: {labels.exit_reason(reason)}",
             f"{labels.field('Quantity')}: {qty}주",
             f"평균 매수가: {_fmt(average_entry, 4)}",
             f"평균 매도가: {_fmt(average_sell, 4)}"]
    # PnL is printed only when it is known. A fee-inclusive realised
    # number is not available until settlement, and printing a
    # gross figure labelled "Realized PnL" would be a claim the ledger
    # cannot support.
    lines.append(f"{labels.field('Realized PnL')}: "
                 f"{_fmt(realized_pnl) if realized_pnl is not None else labels.status('PENDING_SETTLEMENT')}")
    if holding_time:
        lines.append(f"{labels.field('Holding Time')}: {holding_time}")
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

def _strategy_number(strategy) -> str:
    """S1 / S2 for the header, from either a scanner name or a strategy id.

    The number is kept in the header because it is how the operator
    refers to the strategies everywhere else; the Korean name goes on
    its own line rather than replacing it.
    """
    text = str(strategy or "")
    if text.startswith("S1") or text == "hma_early_trend":
        return "S1"
    if text.startswith("S2") or text == "accumulation":
        return "S2"
    return text


def notify_tagged(tag: str, body: str, *, env=None) -> bool:
    try:
        return _send(f"[{labels.tag(tag)}]\n\n{body}", env=env)
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
        return labels.status(INSUFFICIENT_SAMPLE)
    best = (max if highest else min)(usable, key=lambda r: r[key])
    return f"{labels.scanner(best.get('label', '?'))} ({_fmt(best[key])})"


def format_daily_summary(*, trading_day, rows: Iterable[Dict[str, Any]],
                         minimum_trades_for_winner: int = 1) -> str:
    rows = list(rows)
    lines = [f"[{labels.tag(TAG_DAILY_SUMMARY)}]", "",
             f"{labels.field('Trading day')}: {trading_day}", ""]
    for row in rows:
        lines.append(labels.scanner(row.get("label", "?")))
        lines.append(f"  {labels.field('Candidates')}: "
                     f"{row.get('candidates', '-')}")
        for label, key in (("실거래 기회", "live_opportunities"),
                           ("거래 수", "trades"),
                           ("평균 보유시간", "avg_holding"),
                           ("최고 MFE", "best_mfe"),
                           ("평균 MFE", "avg_mfe"),
                           ("평균 MAE", "avg_mae"),
                           ("실현손익", "pnl"),
                           ("실거래 결과", "live_result")):
            if row.get(key) is not None:
                lines.append(f"  {label}: {_fmt(row[key])}")
        lines.append("")
    lines += [
        "오늘 가장 많은 기회: " + _leader(rows, "candidates"),
        "최고 MFE: " + _leader(rows, "best_mfe"),
        "최저 MAE: " + _leader(rows, "avg_mae", highest=False),
        "거래 최다: " + _leader(rows, "trades",
                            minimum_trades=minimum_trades_for_winner),
        # Last, and phrased as a verdict rather than a winner, because
        # with too little behind it the honest answer is that there is
        # no answer -- not a name printed with a caveat beside it.
        "성과 판정: " + _leader(rows, "avg_mfe",
                            minimum_trades=minimum_trades_for_winner),
    ]
    return "\n".join(lines)


def _variant_for(scanner_name, session):
    """The S6 variant this scanner/session pair belongs to, or None.

    Looked up per call rather than computed once, so the outcomes loop
    and the construction-failure loop do not depend on each other's
    locals -- an earlier version bound it only in the first loop and a
    run with no outcomes raised UnboundLocalError inside the very
    handler that is supposed to keep a monitor from failing a scan.
    """
    if str(scanner_name) != "orb":
        return None
    try:
        from config import s6_sessions

        return s6_sessions.variant_for(session) or None
    except Exception:  # noqa: BLE001
        return None


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
            # Classified before ranking so the top block is drawn from
            # the symbols an operator could actually buy. The full count
            # still reports every observed candidate.
            rows = []
            for sig in ranked:
                metrics = getattr(sig, "metrics", None) or {}
                rows.append({
                    "symbol": getattr(sig, "symbol", "?"),
                    "score": getattr(sig, "scanner_score", None),
                    "price": getattr(sig, "signal_price", None),
                    "volume_multiple": metrics.get("volume_multiple"),
                    "volume_expansion": metrics.get("volume_expansion"),
                    "vwap": metrics.get("vwap"),
                    "ema9": metrics.get("session_ema9"),
                    "ema21": metrics.get("session_ema21"),
                    "range_high": metrics.get("opening_range_high"),
                    "range_low": metrics.get("opening_range_low"),
                })
            try:
                from scanners.publish import eligibility

                enriched = eligibility.enrich(rows)
                live_rows = eligibility.top_live(enriched, limit=TOP_N)
                live_count = len(eligibility.split(enriched)[1])
            except Exception:  # noqa: BLE001 - a classification outage
                # must narrow what is shown, never widen it, and must
                # never fail the scan it is describing.
                logger.warning("could not classify candidates", exc_info=True)
                enriched, live_rows, live_count = rows, [], 0

            top = live_rows or []
            for sig in ranked[:0]:
                metrics = getattr(sig, "metrics", None) or {}
                top.append({
                    "symbol": getattr(sig, "symbol", "?"),
                    "score": getattr(sig, "scanner_score", None),
                    "price": getattr(sig, "signal_price", None),
                    "volume_multiple": metrics.get("volume_multiple"),
                    "volume_expansion": metrics.get("volume_expansion"),
                    "vwap": metrics.get("vwap"),
                    "ema9": metrics.get("session_ema9"),
                    "ema21": metrics.get("session_ema21"),
                    "range_high": metrics.get("opening_range_high"),
                    "range_low": metrics.get("opening_range_low"),
                })

            status = "FAILED" if getattr(outcome, "failed", False) else "SUCCESS"
            if getattr(outcome, "failure_reason", None):
                status = f"FAILED: {outcome.failure_reason}"
            if notify_scan(scanner_name=name, session=str(session).upper(),
                           trading_day=trading_day,
                           scanned=getattr(outcome, "symbols_seen", None),
                           candidates=len(signals), status=status, top=top,
                           live_candidates=live_count if signals else None,
                           live_status=modes.get(name, MODE_DISCOVERY_ONLY),
                           variant=_variant_for(name, session), env=env):
                sent += 1

        # A scanner that could not be BUILT never reaches `outcomes`, so
        # the loop above cannot see it and the channel said nothing at
        # all about it. That is the worst version of the failure this
        # module exists to make visible: a broken scanner does not report
        # zero candidates, it reports nothing, and the reader has to
        # notice an ABSENCE among five other messages to catch it.
        for name, reason in (getattr(report, "construction_failures", None)
                             or {}).items():
            if notify_scan(scanner_name=name, session=str(session).upper(),
                           trading_day=trading_day, scanned=None,
                           candidates=None,
                           status=f"FAILED_NOT_BUILT: {reason}",
                           live_status=modes.get(name, MODE_DISCOVERY_ONLY),
                           variant=_variant_for(name, session), env=env):
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
