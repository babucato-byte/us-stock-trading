#!/usr/bin/env python3
"""CODEX-049: the LIVE buy-entry cycle entrypoint
(`us-stock-trading-live.service`, installed but not enabled).

This is the only script in scripts/ that can reach
`execution.execution_engine.submit_buy_order()`, and therefore the only
one that can place a real order. It is deliberately the last piece of
the deployment: the unit that runs it is never enabled by
`install_oracle_services.sh`, and it refuses to run at all while the
read-only posture is in force.

`kis_live_trading.run_live_buy_entry_cycle()` itself raises before any
per-symbol work when `LIVE_ROLLOUT_ENABLED` is false, when HALT or
ENTRY_OFF is set, or when the validated/deployed commits differ -- and
even if all of those were somehow satisfied, `KISBroker.submit_order()`
still runs its own fail-closed `KIS_LIVE_ORDER_ENABLED` gate before the
network. The explicit guard below simply makes the refusal legible in
the service log instead of surfacing as a stack trace.
"""

import argparse
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import kis_live_trading as klt  # noqa: E402
from brokers import kis_rate_limiter  # noqa: E402
from brokers.kis_broker import KISBroker  # noqa: E402
from execution.order_repository import (  # noqa: E402
    FatalRepositoryConnectionError,
)
from execution.secret_redaction import install_logging_redaction  # noqa: E402

logger = logging.getLogger("live_buy_entry")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 3

EXIT_FATAL_DB = 4

#: The broker was busy, so this tick did nothing and that is correct.
#:
#: A new BUY is the lowest-priority use of the KIS budget: below exits,
#: below position management, below reconciliation. Missing an entry
#: costs an opportunity; making a position-managing tick wait costs the
#: management of a real holding, which on 2026-08-27 ended with S1's
#: watchdog disabling entries account-wide.
#:
#: Deferring is not queueing. The tick ends, and the next one re-asks --
#: by then the candidate is either still READY, in which case nothing was
#: lost, or it is not, in which case the order should not have been sent.
ENTRY_DEFERRED_KIS_BUSY = "ENTRY_DEFERRED_KIS_BUSY"

#: S1's executor has gone quiet, so this tick stands down.
#:
#: On 2026-08-27 the entry consumed enough of the shared KIS budget that
#: S1's executor missed two of its fifteen-minute ticks while holding a
#: real position, and its watchdog then disabled entries for every
#: strategy. The lock is fair now and the entry yields on contention, so
#: that should not recur -- but "should not" is an argument, and this is
#: a measurement.
#:
#: The threshold is deliberately well under the watchdog's own limit:
#: the entry gets out of the way while S1 still has room to recover, so
#: the account-wide stop is never reached in the first place. Reads the
#: same cycle log the watchdog reads, so the two cannot disagree about
#: what "quiet" means.
ENTRY_DEFERRED_S1_STALE = "ENTRY_DEFERRED_S1_STALE"

#: Minutes of S1 silence after which a new entry stands down. Half the
#: watchdog's 40, so there is a full recovery window between the entry
#: getting out of the way and the account-wide stop.
S1_SILENCE_STAND_DOWN_MINUTES = 20.0


def _s1_is_falling_behind(now=None):
    """True when S1's executor has been quiet too long to crowd."""
    try:
        from datetime import datetime, timezone

        from market_hours import us_trading_day

        # `scripts/` is not a package, so the watchdog is imported by
        # sitting next to it on the path rather than through a dotted
        # name that does not exist.
        script_dir = str(Path(__file__).resolve().parent)
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        import run_s1_position_watchdog as watchdog

        current = now or datetime.now(timezone.utc)
        if not watchdog.ticks_expected_now():
            # Outside the executor's own session rule it is not due to
            # tick at all, so silence says nothing.
            return False
        newest = watchdog.newest_tick_at(us_trading_day(current))
        if newest is None:
            # No tick recorded yet today. Early in the session that is
            # ordinary; it is not evidence of falling behind.
            return False
        silence = (current - newest).total_seconds() / 60.0
        if silence >= S1_SILENCE_STAND_DOWN_MINUTES:
            logger.warning(
                "S1 executor last ticked %.1f min ago (stand-down at %.0f, "
                "watchdog stops entries at 40)", silence,
                S1_SILENCE_STAND_DOWN_MINUTES)
            return True
        return False
    except Exception:  # noqa: BLE001 -- a missing diagnostic must not
        # decide trading either way; the watchdog remains the backstop.
        logger.warning("could not measure S1 tick age", exc_info=True)
        return False


def _log_calendar(now, declared_session):
    """One line naming every date fact, so a calendar fault is readable.

    The 2026-08-30 incident printed session=OVERNIGHT_DAYTIME and
    trading_day=2026-08-30 -- a Sunday -- and the symptom everyone saw
    was "candidates=0". Four components then failed for four
    different-looking reasons. Printing the facts together means the
    next one is one line instead of an investigation.
    """
    try:
        from config.operational_calendar import resolve_operational_trading_day

        c = resolve_operational_trading_day(now, session=declared_session)
        logger.info(
            "[SYSTEM][S6 CALENDAR] session=%s declared=%s disagreement=%s "
            "session_date=%s operational_trading_day=%s calendar_trading_day=%s "
            "orders_allowed=%s entry_supported=%s exit_supported=%s reason=%s",
            c["session"], c["declared_session"], c["session_disagreement"],
            c["session_date"], c["operational_trading_day"],
            c["calendar_trading_day"], c["orders_allowed"],
            c["entry_supported"], c["exit_supported"], c["reason"])
    except Exception:  # noqa: BLE001 - a log line never stops a tick
        logger.warning("could not describe the trading calendar", exc_info=True)


def _fail_stop(stage, exc):
    """Report an unrecoverable database-connection fault and let the
    caller exit non-zero. HALT was set by the repository before this
    exception was raised; nothing here clears it."""
    logger.critical(
        "FATAL: unrecoverable order-state connection fault during %s (%s) -- "
        "HALT is set and this process must restart so the OS releases the SQLite lock",
        stage, type(exc).__name__,
    )
    try:
        from operations import alerts

        alerts.send_alert(
            "*CRITICAL: trading process fail-stop*\n"
            f"- stage: {stage}\n"
            f"- cause: {type(exc).__name__}\n"
            "- HALT: set\n"
            "- action: process exiting non-zero so systemd restarts it and the SQLite "
            "write lock is released"
        )
    except Exception as alert_exc:  # noqa: BLE001 -- alerting must not mask the fault
        logger.error("could not alert on fail-stop: %s", alert_exc)

def _flag(name):
    return str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on")


def refusal_reason():
    """Returns a human-readable reason this service must not run, or
    None if the operator has genuinely enabled live entries."""
    if not _flag("KIS_LIVE_ORDER_ENABLED"):
        return "KIS_LIVE_ORDER_ENABLED is false -- live orders are not enabled"
    if not _flag("LIVE_ROLLOUT_ENABLED"):
        return "LIVE_ROLLOUT_ENABLED is false -- the live rollout is not active"
    if _flag("ENTRY_DISABLED"):
        return "ENTRY_DISABLED is true -- new entries are blocked"
    return None


#: Which strategy's candidate source the cycle asks. Not which symbol --
#: the symbols are the source's own, at its own production threshold.
#:
#: Omitting it keeps the shipped default exactly as it was: S1's source,
#: resolved from the environment. S6 has to be asked for, because turning
#: it on by default would change which strategy the live cycle trades
#: without anyone saying so.
SOURCE_FACTORIES = {
    "s1": lambda rollout, now, broker=None: None,  # None -> cycle default
    "s6": lambda rollout, now, broker=None: _s6_source(rollout, now,
                                                       broker=broker),
    "s6_buy_worker": lambda rollout, now, broker=None: _s6_buy_worker_source(rollout, now),
}


def _s6_source(rollout, now, *, broker=None):
    """S6's own published breakout rows for the session we are in.

    `s6_live.candidate_source.S6CandidateSource`, not the same-named
    class in `live_pilot.candidate_sources` -- they are different
    interfaces for different callers. This one carries `.name` (which
    `_session_permitted` matches on to route S6 through the capability
    resolver) and the pipeline methods the cycle calls; the live_pilot
    one is the bootstrap's adapter and takes `valid_for_seconds`, which
    this one neither accepts nor needs.

    No freshness argument is passed because this source does not take
    one: its staleness policy is the trading-day, session, variant and
    scan-cycle checks it already applies, and how old a PRICE may be at
    the moment an order is placed is the shared gate's question. A second
    age limit here would be a second staleness policy.
    """
    from market_hours import us_trading_day
    from config import s6_sessions
    from s6_live.candidate_source import S6CandidateSource
    from scanners.base import scan_session

    session = scan_session.session_at()
    trading_day = us_trading_day(now)

    # The fast second stage uses the existing one-minute BUY tick as its
    # single owner, in EVERY S6 session.  It evaluates only the persisted
    # <=41 active symbols for the current session and then enters the same
    # shared order path below; there is no session-specific runtime and no
    # second execution path.  A session S6 does not scan keeps the
    # published full-scan source byte for byte.
    if session in s6_sessions.SCAN_SESSIONS:
        from s6_live.fast_watch import ActiveWatchSource
        from s6_live import pretrade_validation as ptv
        from state_store import db as state_db

        return ActiveWatchSource(
            trading_day=trading_day, session=session, rollout=rollout,
            now=now, conn=state_db.open_db(),
            # Stream members are local reads. A newly discovered outsider
            # uses the bounded REST fallback only until it joins a stream.
            provider=ptv.provider_for(session, broker=broker,
                                      trading_day=trading_day),
            budget_seconds=ptv.budget_seconds())

    source = S6CandidateSource(
        trading_day=trading_day,
        session=session,
        rollout=rollout,
    )
    # An hourly candidate is a reason to WATCH, not a reason to buy.
    #
    # DT was published every fifteen minutes with a fresh generated_at
    # and bit-identical market data underneath -- price, volume, VWAP and
    # EMAs unchanged for three hours -- and the entry path had no step
    # that asked what the market was doing at the moment of the order.
    # The watch re-asks S6's own entry conditions against the current
    # intraday view and offers only the candidates that still hold.
    #
    # A pure restriction on `symbols()`: it can offer fewer names than
    # the source it wraps, never more and never different ones.
    from s6_live.precision_watch import WatchedCandidateSource
    from state_store import db as state_db

    # The session's data adapter, injected HERE rather than resolved
    # inside the watch. Without it `realtime_features.build` takes its
    # stream-only branch in PREMARKET/AFTER_HOURS/OVERNIGHT_DAYTIME, and
    # the stream carries only the ~41 symbols chosen before the session
    # opened -- so a candidate discovered this morning had no feed and
    # every data gate closed against it, whatever the strategy thought.
    #
    # Measured 2026-09-01 PREMARKET: 30 of 32 candidates sat at zero open
    # gates purely for being absent from that list. Realtime is a data
    # delivery mechanism; it does not select stocks.
    from s6_live import pretrade_validation as ptv

    return WatchedCandidateSource(
        source, conn=state_db.open_db(),
        session=session, now=now,
        provider=ptv.provider_for(session, broker=broker,
                                  trading_day=trading_day),
        budget_seconds=ptv.budget_seconds())


def _s6_buy_worker_source(rollout, now):
    """The execution worker's source: claimed BUY_INTENT rows, not a
    fresh fast-watch evaluation. See s6_live/buy_intent_source.py."""
    from market_hours import us_trading_day
    from scanners.base import scan_session
    from s6_live.buy_intent_source import IntentQueueSource

    session = scan_session.session_at()
    return IntentQueueSource(
        trading_day=us_trading_day(now), session=session,
        rollout=rollout, now=now)


def _s6_write_intents(source, *, now):
    """S6's entire responsibility on the fast-watch tick: decide READY,
    hand it off, return. Does not qualify, does not call KIS beyond what
    fast-watch evaluation itself already does, does not submit.

    Returns (ready_symbols, written_count) for the caller's own logging;
    never raises -- an admission fault is logged by
    `s6_live.buy_intent.write_ready` and must not turn a fast-watch tick
    into a failed cron run.
    """
    from s6_live import buy_intent

    ready_symbols = source.symbols()
    rows = [source.candidate_row(s) for s in ready_symbols]
    rows = [r for r in rows if r]
    written = buy_intent.write_ready(
        source._trading_day, source._session, rows, now=now)
    logger.info("S6_BUY_INTENT_WRITTEN ready=%d written=%d symbols=%s",
               len(ready_symbols), written,
               ",".join(ready_symbols) or "-")
    return ready_symbols, written


def _execution_funnel(source, claimed, results, *, since):
    """The execution worker's own funnel (§18/§20): READY -> BUY_INTENT
    (already true by the time this runs -- `claimed` IS that hand-off) ->
    execution started -> qualification -> cash/risk -> submitted/
    accepted/blocked, with READY->intent->execution latency per symbol.

    Deliberately separate from `_funnel()`: that one is built around a
    fresh WATCHING/READY evaluation (`source.evaluations`), which this
    source never produces -- these candidates were already judged READY
    by an earlier fast-watch tick.
    """
    from datetime import datetime

    blocked = dict(results.get("blocked") or ())
    skipped = dict(results.get("skipped") or ())
    submitted = set(results.get("submitted") or ())
    logger.info(
        "FUNNEL_EXECUTION claimed=%d submitted=%d blocked=%d skipped=%d",
        len(claimed), len(submitted), len(blocked), len(skipped))
    # `_funnel()` (the fast-watch tick) announces blocks to stock-live-
    # trading; this is the worker's own funnel and never called that,
    # so every INSUFFICIENT_CASH/RISK_BLOCKED/SESSION_BLOCKED/etc. the
    # worker's own gates produced went completely unannounced -- logged,
    # never presented, since the day READY was decoupled from
    # submission. The routine/transient codes (execution-lock
    # contention, symbol-already-held) stay silent exactly as before --
    # that filtering lives in operations.slack_presentation.
    # SILENT_BLOCK_CODES, not here.
    _announce_blocks(results.get("blocked") or ())
    for symbol in sorted(claimed):
        meta = source.intent_metadata(symbol) if hasattr(source, "intent_metadata") else {}
        first_ready_at = meta.get("first_ready_at")
        latency_ms = None
        if first_ready_at:
            try:
                ready_at = datetime.fromisoformat(str(first_ready_at))
                latency_ms = round(
                    (since - ready_at).total_seconds() * 1000, 1)
            except Exception:  # noqa: BLE001 -- reporting only
                latency_ms = None
        if symbol in submitted:
            outcome = "SUBMITTED"
        elif symbol in blocked:
            outcome = f"BLOCKED reason={blocked[symbol]}"
        elif symbol in skipped:
            outcome = f"SKIPPED reason={skipped[symbol]}"
        else:
            outcome = "UNKNOWN"
        logger.info(
            "FUNNEL_EXECUTION_SYMBOL %s first_ready_at=%s ready_to_execution_ms=%s outcome=%s",
            symbol, first_ready_at, latency_ms, outcome)


def _funnel(source, results, *, since, expect_no_submission=False):
    """One line describing what happened to every candidate this tick.

    The counts exist because "no BUY today" has several very different
    explanations and the log could not tell them apart: nothing
    published, everything still WATCHING, everything READY but
    unaffordable, or -- the one that matters -- candidates that reached
    READY and were never acted on. That last case is an execution defect
    and it is invisible without the numbers either side of it.

    EXECUTABLE is read from the audit trail rather than recounted here.
    The Execution Engine records GATE_APPROVED before it calls the
    broker, so an approval that never became an order is already durably
    recorded; a second count kept alongside the submission loop could
    disagree with the gate, and then the number meant to expose the
    defect would be derived from the code suspected of having it.

    `expect_no_submission`: the fast-watch tick (strategy="s6", since
    2026-09-10) never submits anything itself -- READY only produces a
    BUY_INTENT, and the execution worker submits later, in a different
    process. "ready>0, submitted=0" there is the intended steady state,
    not a defect signal, so it skips the classification below (still
    used for s1 and any strategy that reaches the shared cycle inline).
    """
    scanned = watching = ready = executable = 0
    evaluations = getattr(source, "evaluations", None) or {}
    if evaluations:
        scanned = len(evaluations)
        ready = sum(1 for e in evaluations.values() if getattr(e, "ready", False))
        watching = scanned - ready
    submitted = len(results.get("submitted") or ())
    try:
        import shadow_audit

        conn = shadow_audit._open_conn()
        try:
            # SCOPED THREE WAYS, and every one of them was missing.
            #
            # The count answers "did a BUY this funnel prepared get
            # approved and then not sent". Unscoped it answered "did
            # ANY gate approve ANYTHING", which on 2026-09-02 at
            # 16:34:52 counted `s6exit-HBAN-4bd8f7bb86f3` -- an exit
            # SELL, approved by the exit runtime at 16:24:52, inside
            # this cycle's window because the cycle had been running
            # since 16:24:07. The funnel then reported that the gate
            # had approved a buy it never submitted. It had not.
            #
            #   side='buy'   an exit approval is not a buy
            #   symbol IN    another strategy's buy is not this funnel's
            #   created_at   this cycle only, as before
            #
            # Entry cycles now run for minutes without the execution
            # lock, so the window is wide and the odds of catching an
            # unrelated approval in it are no longer small.
            symbols = sorted(evaluations)
            if symbols:
                placeholders = ",".join("?" * len(symbols))
                executable = conn.execute(
                    "SELECT COUNT(*) FROM shadow_audit_events "
                    "WHERE event_type = ? AND created_at >= ? "
                    "AND side = 'buy' "
                    f"AND symbol IN ({placeholders})",
                    (shadow_audit.GATE_APPROVED, since.isoformat(), *symbols),
                ).fetchone()[0]
            else:
                executable = 0
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 -- reporting must never affect trading
        executable = -1

    logger.info(
        "FUNNEL scanned=%d watching=%d ready=%d executable=%s submitted=%d",
        scanned, watching, ready,
        "unavailable" if executable < 0 else executable, submitted)

    # §16: READY candidates that reached the gate and were approved, and
    # then produced no order, is the one combination that is a defect
    # rather than a market condition.
    #
    # This lives HERE, with the counts. It had drifted into
    # `_record_shadow_signals`, where `ready` is a per-symbol boolean
    # from the loop rather than a count -- so it read the LAST symbol's
    # readiness, and raised UnboundLocalError outright on any tick with
    # no candidates at all. Which is every tick when discovery is empty:
    # the check meant to catch a silent execution defect was itself
    # failing silently, once a minute.
    if ready > 0 and submitted == 0 and not expect_no_submission:
        label, level, detail = _classify_no_submission(results, executable)
        logger.log(
            level, "%s ready=%d executable=%d submitted=0 -- %s",
            label, ready, executable, detail)
    elif ready > 0 and expect_no_submission:
        logger.info(
            "ENTRY_READY_HANDED_OFF ready=%d -- BUY_INTENT written for the "
            "execution worker; this tick never submits", ready)
    for symbol, evaluation in sorted(evaluations.items()):
        if not getattr(evaluation, "ready", False):
            logger.info("FUNNEL_WATCHING %s state=%s blocking=%s", symbol,
                        getattr(evaluation, "state", "?"),
                        ",".join(getattr(evaluation, "blocking", ()) or ()) or "-")
    for symbol, reason in (results.get("skipped") or ()):
        logger.info("FUNNEL_SKIPPED %s reason=%s", symbol, reason)
    for symbol, reason in (results.get("blocked") or ()):
        logger.info("FUNNEL_BLOCKED %s reason=%s", symbol, reason)
    _announce_blocks(results.get("blocked") or ())
    for symbol in (results.get("submitted") or ()):
        logger.info("FUNNEL_SUBMITTED %s", symbol)

    _record_shadow_signals(source, results, since=since)
    _announce_fast_watch_health(source)
    _log_s6_transport_funnel(source, ready=ready, watching=watching)


def _log_s6_transport_funnel(source, *, ready, watching) -> None:
    """One extra line, S6-only: the transport/admission breakdown the
    physical-vs-logical cap split needs to be provable from logs alone
    (logical watch size, WebSocket- vs REST-backed, deferred). Universe
    and scanner-PASS counts are the SCANNER's own numbers, not this
    runner's, and are read from its log directly rather than guessed
    here.
    """
    if getattr(source, "name", None) is None or not hasattr(source, "describe") \
            or getattr(source, "_scope", "missing") == "missing":
        return
    try:
        info = source.describe()
        tiers = info.get("tier_counts") or {}
        logger.info(
            "FUNNEL_S6_TRANSPORT logical_watch=%s websocket_backed=%s rest_backed=%s "
            "fast_evaluated=%s deferred=%s watching=%d ready=%d "
            "hot=%s warm=%s cold=%s load_ms=%s eval_loop_ms=%s",
            info.get("watchlist_size"), info.get("websocket_backed"),
            info.get("rest_backed"), info.get("fast_evaluated"),
            info.get("deferred"), watching, ready,
            tiers.get("HOT"), tiers.get("WARM"), tiers.get("COLD"),
            info.get("load_ms"), info.get("eval_loop_ms"))
    except Exception:  # noqa: BLE001 -- reporting must never affect trading
        logger.warning("could not log S6 transport funnel", exc_info=True)


def _announce_fast_watch_health(source) -> None:
    """Route only S6 runtime faults to system-health, after trading ends."""
    # Identity, not session: only the active-watch source reports this.
    if getattr(source, "name", None) is None or not hasattr(source, "describe") \
            or getattr(source, "_scope", "missing") == "missing":
        return
    try:
        info = source.describe()
        faults = []
        status = info.get("watchlist_status")
        if status != "ACTIVE":
            faults.append(("ACTIVE_WATCH_FAILURE", f"watchlist_status={status}"))
        collector = info.get("collector_state")
        if collector in ("COLLECTOR_STALE", "DISCONNECTED", "FAILED",
                         "SUBSCRIPTION_PARTIAL", "UNKNOWN"):
            faults.append(("ACTIVE_WATCH_STALE", f"collector_state={collector}"))
        origin_missing = []
        for symbol, evaluation in (getattr(source, "evaluations", None) or {}).items():
            reason = str(getattr(evaluation, "reason", "") or "")
            error = str(getattr(getattr(evaluation, "features", None), "error", "") or "")
            if "OFFICIAL_ORIGIN_NOT_COVERED" in reason + error:
                origin_missing.append(symbol)
        if origin_missing:
            faults.append(("DATA_ORIGIN_UNAVAILABLE",
                           f"count={len(origin_missing)} symbols={','.join(origin_missing[:8])}"))
        if not faults:
            return
        script_dir = str(Path(__file__).resolve().parent)
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        import notify_system_health
        for code, detail in faults:
            notify_system_health.main([code, detail])
    except Exception:  # noqa: BLE001 -- health reporting cannot affect orders
        logger.warning("fast-watch health report failed", exc_info=True)


#: A tick that produced no order, and what kind of nothing it was.
#:
#: All three were one ERROR line. That made the loudest signal in the
#: entry path fire on days when the system was working exactly as
#: designed -- an exit holding execution access, a candidate dropped by
#: revalidation, an account with no cash -- and a warning that cries on
#: ordinary Tuesdays stops being read by Thursday.
EXPECTED_CONTENTION = "ENTRY_YIELDED_EXPECTED_CONTENTION"
EXPECTED_DEFERRAL = "ENTRY_DEFERRED_EXPECTED"
REAL_EXECUTION_DEFECT = "EXECUTION_DEFECT_SUSPECTED"

#: The entry gave way to something that outranks it. No broker mutation
#: happened, nothing is stuck, and the next minute re-asks.
_CONTENTION_MARKERS = (
    "execution access is held by another cycle",
)

#: The entry decided against itself on current evidence. Also expected,
#: also self-recovering, but worth separating from contention: one says
#: the system was busy, the other says the candidate stopped qualifying.
_DEFERRAL_MARKERS = (
    "while this entry was being prepared",   # every revalidation drop
    "insufficient KIS orderable cash",
    "not in live_rollout.allowed_symbols",
    "signal", "expired",
)

#: The order REACHED the broker and the broker answered. That answer is
#: reported by its own path (BROKER_REJECTED / BROKER_UNKNOWN) and it
#: accounts for a gate approval that produced no submission -- so it is
#: neither an unexplained defect nor a quiet deferral.
_BROKER_ANSWERED_MARKERS = (
    "KIS rejected the order",
    "KIS did not confirm the order",
)


def _classify_no_submission(results, executable):
    """Why did a tick with READY candidates send nothing?

    Returns (label, log level, detail). Observability only: nothing here
    gates, blocks, retries or cancels anything, and a misclassification
    costs a log line, never a trade.

    The question that matters is narrow: was a BUY *approved by the gate*
    and then not sent? Everything else is the system declining to trade,
    which it is supposed to be able to do without raising an alarm.
    """
    blocked = [str(reason or "") for _s, reason in (results.get("blocked") or ())]
    answered = sum(1 for r in blocked
                   if any(m in r for m in _BROKER_ANSWERED_MARKERS))

    # An approval the broker answered is accounted for. What is left is
    # an approval with no order and no answer anywhere -- the silent
    # failure this check was written for.
    unexplained = max(0, int(executable) - int(answered))
    if unexplained > 0:
        return (REAL_EXECUTION_DEFECT, logging.ERROR,
                f"the gate approved {unexplained} order(s) that were never "
                f"submitted and that the broker never answered")

    if any(any(m in r for m in _CONTENTION_MARKERS) for r in blocked):
        return (EXPECTED_CONTENTION, logging.INFO,
                "an exit or another execution cycle held execution access; "
                "no broker mutation occurred and the next tick re-asks")

    if blocked and all(
            any(m in r for m in _DEFERRAL_MARKERS + _BROKER_ANSWERED_MARKERS)
            for r in blocked):
        return (EXPECTED_DEFERRAL, logging.INFO,
                "every ready candidate was declined on current evidence; "
                "no broker mutation occurred and the next tick re-asks")

    # Ready candidates, no approvals, and reasons this function does not
    # recognise. Not proof of a defect -- but not something to file as
    # expected either, so it stays visible at WARNING.
    return (EXPECTED_DEFERRAL, logging.WARNING,
            "no order was submitted and the reasons are not all recognised "
            f"as expected: {sorted(set(blocked))[:5]}")


def _announce_blocks(blocked) -> None:
    """One 매수 차단 message per (symbol, reason code, trading day).

    Presentation only. The block itself was decided and logged above;
    this reads `results["blocked"]` and never writes anything the entry
    path reads. Transient codes (the execution lock, an already-held
    symbol) and outcomes the engine already announced (a broker rejection
    or an UNKNOWN response) are filtered inside `notify()`. A tracking
    failure after a successful buy is not a block at all and goes to the
    alert channel instead.
    """
    if not blocked:
        return
    try:
        from operations import live_notifications as ln
        from operations import slack_presentation as sp
        from state_store import db as state_db
    except Exception:  # noqa: BLE001
        logger.warning("block notifications unavailable", exc_info=True)
        return
    conn = None
    try:
        conn = state_db.open_db()
    except Exception:  # noqa: BLE001 - dedupe is best-effort
        conn = None
    try:
        for symbol, reason in blocked:
            code = sp.block_code_for(reason)
            if code == "POSITION_TRACKING_FAILED":
                ln.notify(ln.DB_FAILURE, {"symbol": symbol, "reason": code,
                                          "detail": str(reason)[:200]},
                          dedupe_conn=conn, dedupe_subject=symbol,
                          dedupe_version=code)
                continue
            ln.notify(ln.ORDER_BLOCKED,
                      ln.order_blocked_fields(symbol=symbol, reason_code=code,
                                              detail=str(reason)[:200],
                                              strategy_id="S6_ORB_BREAKOUT_V1"),
                      dedupe_conn=conn)
    except Exception:  # noqa: BLE001 - reporting must never affect trading
        logger.warning("block notifications failed", exc_info=True)
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:  # noqa: BLE001
            pass


def _announce_quality_blocks(source, *, since) -> None:
    """One 매수 차단 message per (symbol, S6 reason code, day) for the
    candidates the entry-quality gate stopped this tick.

    Presentation only. The watch already decided and logged; this reads
    `source.evaluations` and never writes anything the entry path reads.
    Deduplicated through the notification ledger; a Slack failure cannot
    reach trading (notify() never raises).
    """
    evaluations = getattr(source, "evaluations", None) or {}
    hits = []
    for symbol, evaluation in sorted(evaluations.items()):
        blocking = list(getattr(evaluation, "blocking", ()) or ())
        detail = dict(getattr(evaluation, "detail", {}) or {})
        code = detail.get("entry_quality_reason")
        if not code or not blocking or blocking[0] != "ENTRY_QUALITY":
            continue
        hits.append((symbol, code, detail, evaluation))
    if not hits:
        return
    from operations import live_notifications as ln
    from state_store import db as state_db

    conn = None
    try:
        conn = state_db.open_db()
    except Exception:  # noqa: BLE001
        conn = None
    try:
        for symbol, code, detail, evaluation in hits:
            fields = ln.order_blocked_fields(
                symbol=symbol, reason_code=code,
                strategy_id="S6_ORB_BREAKOUT_V1",
                session=getattr(evaluation, "session", None))
            compact = dict(detail.get("entry_quality") or {})
            fields["orb_minutes"] = detail.get("range_minutes")
            for key in ("breakout_age_minutes", "minutes_since_session_high",
                        "rvol_5m", "rvol_15m"):
                if compact.get(key) is not None:
                    fields[key] = compact[key]
            try:
                ln.notify(ln.ORDER_BLOCKED, fields, dedupe_conn=conn)
            except Exception:  # noqa: BLE001 -- notify() never raises; belt and braces
                logger.warning("entry-quality block notice for %s failed", symbol,
                               exc_info=True)
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:  # noqa: BLE001
            pass


#: How long ONE entry tick may run before its own non-critical
#: post-processing starts skipping itself, so it cannot push the tick
#: past the 60-second cron interval and cause the next one to be
#: OVERLAP_SKIPPED. Below the cron interval on purpose -- there is
#: cleanup (return, log flush) after this check too.
_TICK_HARD_BUDGET_SECONDS = 50.0


def _shadow_budget_remaining(since, *, now=None):
    """Seconds left for optional shadow work in this entry tick."""
    from datetime import datetime, timezone

    elapsed = ((now or datetime.now(timezone.utc)) - since).total_seconds()
    return _TICK_HARD_BUDGET_SECONDS - elapsed


def _shadow_deferred_budget(*, processed, total, since):
    """Make an audit deferral visible without changing trading state."""
    elapsed = _TICK_HARD_BUDGET_SECONDS - _shadow_budget_remaining(since)
    logger.info("SHADOW_DEFERRED_BUDGET elapsed=%.1fs limit=%.0fs processed=%d deferred=%d",
                elapsed, _TICK_HARD_BUDGET_SECONDS, processed,
                max(0, total - processed))


def _record_shadow_signals(source, results, *, since):
    """Persist what happened to every candidate this tick.

    Written here, after the cycle, for the same reason the funnel is:
    everything is known and nothing left can be affected by it. A
    candidate refused at a gate otherwise leaves no trace at all, which
    makes "is this gate blocking good trades" a question nobody can
    answer.
    """
    from datetime import datetime, timezone

    # This is research/audit work after the order path.  It never earns an
    # extra second of the entry lock: return before imports or persistence
    # when the tick has reached its deadline.
    evaluations = getattr(source, "evaluations", None) or {}
    total = len(evaluations)
    if _shadow_budget_remaining(since) <= 0:
        _shadow_deferred_budget(processed=0, total=total, since=since)
        return

    try:
        from config import s6_sessions
        from market_hours import us_trading_day
        from s6_live import shadow_signal_log as ssl

        session = getattr(source, "_session", None) or getattr(
            source, "session", None)
        day = us_trading_day(since)
        blocked = {str(sym): reason
                   for sym, reason in (results.get("blocked") or ())}
        skipped = {str(sym): reason
                   for sym, reason in (results.get("skipped") or ())}
        submitted = {str(s) for s in (results.get("submitted") or ())}

        processed = 0
        for symbol, evaluation in sorted(evaluations.items()):
            if _shadow_budget_remaining(since) <= 0:
                _shadow_deferred_budget(processed=processed, total=total, since=since)
                return
            ready = bool(getattr(evaluation, "ready", False))
            if symbol in submitted:
                outcome, first = ssl.OUTCOME_SUBMITTED, None
            elif symbol in blocked or symbol in skipped:
                outcome = ssl.OUTCOME_BLOCKED
                first = blocked.get(symbol) or skipped.get(symbol)
            elif ready:
                outcome, first = ssl.OUTCOME_EXECUTABLE, None
            else:
                outcome = ssl.OUTCOME_NOT_READY
                blocking = list(getattr(evaluation, "blocking", ()) or ())
                first = blocking[0] if blocking else None

            feats = getattr(evaluation, "features", None)
            detail = dict(getattr(evaluation, "detail", {}) or {})
            record = ssl.build_record(
                symbol=symbol, session=session, outcome=outcome,
                strategy_id=s6_sessions.STRATEGY_ID,
                features=feats,
                candidate=(source.candidate_row(symbol)
                           if hasattr(source, "candidate_row") else None),
                first_blocked_by=first,
                watch_blocking=getattr(evaluation, "blocking", ()),
                now=since,
                scanner_variant=detail.get("scanner_variant"),
                range_minutes=detail.get("range_minutes"),
                evaluated_at=getattr(evaluation, "evaluated_at", None),
                entry_quality=getattr(feats, "entry_quality", None),
                entry_quality_reason=detail.get("entry_quality_reason"),
                watch_state=getattr(evaluation, "state", None),
                watch_detail={k: v for k, v in detail.items()
                              if k in ("entry_quality_gate", "age_seconds",
                                       "extension_pct", "volume_expansion")})
            ssl.append(record, trading_day=day)
            processed += 1
            # append can block on filesystem pressure; do not start the
            # next symbol after it consumes the remaining entry deadline.
            if _shadow_budget_remaining(since) <= 0:
                _shadow_deferred_budget(processed=processed, total=total, since=since)
                return
    except Exception:  # noqa: BLE001 -- an observation that fails must
        # not alter a cycle that has already finished trading.
        logger.warning("could not record shadow signals", exc_info=True)

    # HARD TICK BUDGET: everything from here down is presentation or
    # research, never the trading decision itself (that already happened,
    # durably, in the shadow_signal_log.append() calls above and in
    # klt.run_live_buy_entry_cycle()). A logical watchlist large enough to
    # need it can push this tick's OWN cost past the 60-second cron
    # interval, and a tick that overruns causes the NEXT cron trigger to
    # be OVERLAP_SKIPPED -- which is worse for latency than skipping
    # optional work THIS tick, because it silently doubles the effective
    # cadence for every candidate, not just this one. Measured
    # 2026-09-09: a 73.7s tick against a 60s cron interval. Slack must
    # never be why a candidate misses its tick, so it is the first thing
    # dropped, and ORB15 shadow (comparison research, explicitly never an
    # order -- see s6_live/range_shadow.py) is the second.
    elapsed = (datetime.now(timezone.utc) - since).total_seconds()
    if elapsed > _TICK_HARD_BUDGET_SECONDS:
        logger.warning(
            "TICK_BUDGET_EXCEEDED elapsed=%.1fs limit=%.0fs -- skipping "
            "entry-quality Slack announcements and ORB15 shadow recording "
            "this tick; the required shadow_signal_log audit rows above "
            "were still written",
            elapsed, _TICK_HARD_BUDGET_SECONDS)
    else:
        try:
            _announce_quality_blocks(source, since=since)
        except Exception:  # noqa: BLE001 -- presentation only
            logger.warning("could not announce entry-quality blocks", exc_info=True)

        try:
            from market_hours import us_trading_day
            from s6_live import range_shadow

            # A live py-spy trace on 2026-09-09 caught this loop still
            # running ~4s/symbol well past the tick's own budget, because
            # it had no deadline of its own -- the same "runs unbounded
            # once already inside the outer gate" shape as the closed-bar
            # shadow's per-symbol loop, fixed the same way.
            range_shadow.record_cycle(
                source, trading_day=us_trading_day(since), now=since,
                deadline=lambda: _shadow_budget_remaining(since) <= 0)
        except Exception:  # noqa: BLE001 -- research, and the cycle is over
            logger.warning("could not record the ORB15 range shadow", exc_info=True)

    # In its OWN try. It used to sit inside the block above, after the
    # shadow-signal import -- so when that import was wrong, this never
    # ran either, and two independent observations were lost to one
    # bug. Neither of them can take the other down now.
    #
    # Also its own HARD TICK BUDGET check, re-measured here rather than
    # reusing `elapsed` above: this is the same "presentation or
    # research, never the trading decision" category as the block above
    # it, but it was not actually covered by that check (each symbol
    # runs a full entry-quality baseline computation -- one or more
    # JSON snapshot loads apiece -- and a live py-spy trace on
    # 2026-09-09 caught a production tick still inside this exact call
    # chain, six-plus minutes in, for a REGULAR-session watchlist).
    # Uncapped per-symbol research work here is exactly what turns a
    # correctly budgeted 30s live-evaluation pass into a multi-minute
    # tick, so it gets the same treatment as Slack and the ORB15
    # shadow: skip once the tick has already overrun.
    elapsed = (datetime.now(timezone.utc) - since).total_seconds()
    if elapsed > _TICK_HARD_BUDGET_SECONDS:
        logger.warning(
            "TICK_BUDGET_EXCEEDED elapsed=%.1fs limit=%.0fs -- skipping "
            "the closed-bar shadow comparison this tick",
            elapsed, _TICK_HARD_BUDGET_SECONDS)
    else:
        try:
            from market_hours import us_trading_day
            from scanners.base import scan_session

            _record_closed_bar_shadow(
                source, sorted(getattr(source, "evaluations", None) or {}),
                session=scan_session.session_at(),
                day=us_trading_day(since), since=since)
        except Exception:  # noqa: BLE001
            logger.warning("could not record the closed-bar comparison",
                           exc_info=True)



def _record_closed_bar_shadow(source, symbols, *, session, day, since):
    """The same features read off closed bars only, recorded beside the
    live reading.

    Every live feature is computed over ALL bars, and the last of those
    is the minute in progress -- its close is whatever the latest print
    was, and its volume a fraction of what the minute will finish with.
    A breakout read off a partial bar can un-break before the minute
    ends. Whether that actually happens here has never been measured.

    Production is untouched: this records what the other reading WOULD
    have said, so a later argument for closed bars can be made from
    evidence rather than from that plausible story.

    Also budgeted, PER SYMBOL, not just once before the loop: a live
    py-spy trace on 2026-09-09 caught a single symbol's `compare` +
    `compare_readiness` pair (each its own entry-quality baseline
    computation) running for minutes on its own in a REGULAR-session
    tick, so a check only before the whole call would not have stopped
    an overrun starting on the very first symbol. The caller's own
    check (just above) still skips this entirely once already over
    budget; this one covers the loop once it is under way.
    """
    try:
        from datetime import datetime, timezone

        from s6_live import closed_bar_shadow, kis_bar_features

        if not session:
            return
        store = kis_bar_features.load_store(session, day)
        if store is None:
            return
        for symbol in symbols:
            elapsed = (datetime.now(timezone.utc) - since).total_seconds()
            if elapsed > _TICK_HARD_BUDGET_SECONDS:
                logger.warning(
                    "TICK_BUDGET_EXCEEDED elapsed=%.1fs limit=%.0fs -- "
                    "stopping the closed-bar shadow comparison mid-loop "
                    "at %s; %d of %d symbols were not reached",
                    elapsed, _TICK_HARD_BUDGET_SECONDS, symbol,
                    len(symbols) - symbols.index(symbol), len(symbols))
                return
            comparison = closed_bar_shadow.compare(
                symbol, store=store, session=session, now=since)
            if comparison is not None:
                closed_bar_shadow.append(comparison, trading_day=day)
            # Whether the difference reaches the DECISION, which the
            # feature deltas alone cannot say: a large gap in a field no
            # gate consults changes nothing, and a small one that
            # crosses a threshold changes everything.
            verdict = closed_bar_shadow.compare_readiness(
                symbol, store=store, session=session, now=since)
            if verdict is not None:
                closed_bar_shadow.append(verdict, trading_day=day)
    except Exception:  # noqa: BLE001 - research, and the cycle is over
        logger.warning("could not record the closed-bar comparison",
                       exc_info=True)


def run_once(broker=None, *, strategy="s1"):
    """The work this entrypoint does, factored out so it can be driven
    (and faulted) directly -- same shape as every other service script.

    Only the SOURCE varies with `strategy`. Every gate below it --
    allow-list, price re-validation, orderable cash, duplicate order,
    entry limits, kill switch, reconciliation, the Execution Engine --
    is shared and exists exactly once, which is what keeps a second
    strategy from getting a second, less-exercised execution path.

    `strategy="s6"` is the one exception, and it never reaches that
    shared cycle at all: it is fast-watch's own tick, and its entire
    job is deciding READY and writing a BUY_INTENT (see
    `_s6_write_intents`) -- not qualifying, not calling KIS beyond what
    fast-watch evaluation itself already does, not submitting. Measured
    2026-09-09: the shared cycle's qualify->KIS->submit sequence costs
    ~44-45s PER READY CANDIDATE, which is why it used to blow through
    the 60s cron interval and OVERLAP_SKIP the next tick. `strategy=
    "s6_buy_worker"` is the other half: a separate process, its own cron/lock,
    that claims the queue `_s6_write_intents` filled and calls the
    SAME unchanged shared cycle those candidates would have gone
    through inline before.
    """
    from datetime import datetime, timezone

    from config.live_rollout_config import LiveRolloutConfig

    now = datetime.now(timezone.utc)
    factory = SOURCE_FACTORIES[strategy]
    # The cycle's own broker, so pre-trade validation reuses this
    # authenticated client rather than standing up a second one.
    resolved_broker = broker or KISBroker()
    source = factory(LiveRolloutConfig.from_env(), now, resolved_broker)

    try:
        from scanners.base import scan_session

        session = scan_session.session_at()
    except Exception:  # noqa: BLE001 -- context, not a precondition
        session = "unavailable"
    logger.info(
        "TICK started_at=%s strategy=%s session=%s deployed=%s runtime_root=%s",
        now.isoformat(), strategy, session,
        os.environ.get("DEPLOYED_COMMIT", "<unset>"),
        os.environ.get("TRADING_PROJECT_ROOT", "<unset>"))
    _log_calendar(now, session)

    if strategy == "s6":
        ready_symbols, _written = _s6_write_intents(source, now=now)
        try:
            _funnel(source, {"submitted": [], "blocked": [], "skipped": []},
                   since=now, expect_no_submission=True)
        except Exception:  # noqa: BLE001 -- a reporting fault must not
            # change what the tick already did, nor mask its result.
            logger.warning("funnel report failed", exc_info=True)
        return {"submitted": [], "blocked": [], "skipped": [],
               "ready_written": ready_symbols}

    results = klt.run_live_buy_entry_cycle(
        broker=resolved_broker, candidate_source=source)
    try:
        if strategy == "s6_buy_worker":
            _execution_funnel(source, source.claimed_symbols(), results, since=now)
        else:
            _funnel(source, results, since=now)
    except Exception:  # noqa: BLE001 -- a reporting fault must not
        # change what the cycle already did, nor mask its result.
        logger.warning("funnel report failed", exc_info=True)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="KIS live buy-entry cycle")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--strategy", default="s1",
                        choices=sorted(SOURCE_FACTORIES),
                        help="which strategy's candidate source to use")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    install_logging_redaction()

    reason = refusal_reason()
    if reason is not None:
        logger.error("refusing to run the live buy-entry cycle: %s", reason)
        return EXIT_REFUSED

    # Fast-watch's own tick (strategy="s6") no longer makes the heavy,
    # KIS-budget-competing calls this stand-down exists to protect S1
    # from -- those moved to the execution worker (strategy="s6_buy_worker")
    # when READY was decoupled from submission (2026-09-10). Standing
    # fast-watch down here would only cost READY-detection cadence for
    # no protective effect, so only the strategies that still reach the
    # shared qualify->KIS->submit cycle check it.
    if args.strategy != "s6" and _s1_is_falling_behind():
        logger.info(
            "%s: S1's executor is behind and holds the account's open "
            "position; a new entry stands down rather than compete with it",
            ENTRY_DEFERRED_S1_STALE)
        return EXIT_OK

    try:
        results = run_once(strategy=args.strategy)
    except kis_rate_limiter.KISRateLimitStateUnavailable as exc:
        # Only the contention case yields here. A genuinely broken or
        # missing state file is a different fault and must still surface
        # as an error rather than be filed as "the broker was busy".
        if getattr(exc, "reason_code", None) != kis_rate_limiter.REASON_LOCK_FAILED:
            logger.exception("KIS rate-limit state unavailable: %s", exc)
            return EXIT_ERROR
        logger.info(
            "%s: another owner holds the KIS rate-limit lock; this tick is "
            "dropped, not queued, and the next one re-evaluates",
            ENTRY_DEFERRED_KIS_BUSY)
        return EXIT_OK
    except klt.KISLiveTradingError as exc:
        logger.error("live buy-entry cycle refused to run: %s", exc)
        return EXIT_REFUSED
    except FatalRepositoryConnectionError as exc:
        # CODEX-058: the order-state connection could neither be rolled
        # back nor closed, so this process may still hold a SQLite write
        # lock that blocks every other writer. HALT is already set by the
        # repository; exiting non-zero is what actually releases the lock
        # (the OS reclaims the descriptor) and lets systemd's
        # Restart=on-failure bring the service back cleanly.
        _fail_stop("live buy-entry cycle", exc)
        return EXIT_FATAL_DB
    except Exception as exc:  # noqa: BLE001 -- service entrypoint
        logger.exception("live buy-entry cycle failed: %s", exc)
        if args.strategy == "s6_buy_worker":
            # The execution worker is the one process that can reach the
            # broker for S6; an unhandled crash here means claimed
            # BUY_INTENTs sit unprocessed until the next minute silently
            # retries -- exactly the "execution worker failure" case
            # operators need to see on stock-live-alerts, not just in a
            # log line nobody is watching between ticks.
            try:
                from operations import alerts
                alerts.send_alert(
                    "*S6 execution worker failed*\n"
                    f"- cause: {type(exc).__name__}\n"
                    "- effect: claimed BUY_INTENTs were not processed this tick; "
                    "the next minute retries automatically"
                )
            except Exception:  # noqa: BLE001 -- alerting must not mask the fault
                logger.error("could not alert on execution worker failure", exc_info=True)
        return EXIT_ERROR

    logger.info(
        "live buy-entry cycle: submitted=%s blocked=%d skipped=%d",
        results["submitted"], len(results["blocked"]), len(results["skipped"]),
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
