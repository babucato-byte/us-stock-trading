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

#: An exit is in flight, so this tick does not start.
#:
#: The entry and the exit runtime share `s6_exec.lock`, so whichever
#: arrives first holds it -- and an entry cycle that has taken it delays
#: the exit behind it for as long as the cycle runs. An entry is an
#: opportunity; an exit is a position already at risk, and a strategy
#: whose exit condition has fired is not one that should be opening
#: anything else first.
#:
#: Checked from the local position store: a SQLite read, no broker call,
#: so asking costs nothing from the budget it is protecting.
ENTRY_DEFERRED_EXIT_PENDING = "ENTRY_DEFERRED_EXIT_PENDING"

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


def _exit_in_flight(now=None):
    """True when an exit is actually able to run and compete for the lock.

    The original rule deferred entries whenever ANY position had an exit
    submitted or pending, because entry and exit share `s6_exec.lock` and
    an entry cycle holding it delays the exit behind it.

    That reasoning holds only while the exit CAN run. RIG latched
    EXIT_PENDING on Friday at 19:52 and its route was unavailable all
    weekend, so this returned True continuously for three days and
    deferred every entry -- protecting an exit cycle that was never going
    to start. Nothing was being contended for.

    So the two cases are separated:

      an order already at the broker (`exit_submitted`)
          always defers. Something is live and a second order must not
          race it, whatever the session says.

      a latched exit that cannot be submitted right now
          does not defer. It is not competing for the lock, and blocking
          on it stops trading for a reason that no longer exists.

    Everything else -- slot caps, ownership, reconciliation, the gate --
    is unchanged and still applies to any entry that proceeds.
    """
    try:
        from s6_live import position_store
        from state_store import db as state_db

        pending = False
        with state_db.open_db() as conn:
            for _pid, row in position_store.load_live(conn):
                if row.get("exit_submitted"):
                    return True
                if row.get("pending_exit_reason"):
                    pending = True
        if not pending:
            return False
        return _exit_can_run_now(now)
    except Exception:  # noqa: BLE001 -- an unreadable store is not a
        # reason to refuse the entry; the gate and the runtime have their
        # own, stronger refusals, and failing the tick over a diagnostic
        # would stop trading for the wrong reason.
        logger.warning("could not check for exits in flight", exc_info=True)
        return False



def _exit_can_run_now(now=None):
    """Can a latched exit actually be submitted at this moment?

    Asked of `session_capability`, the same authority the order path
    uses, so the entry cycle and the exit runtime cannot disagree about
    whether a sell is possible.

    Fails CLOSED: if capability cannot be determined, the answer is that
    an exit may well be about to run, and deferring the entry is the
    cheaper mistake.
    """
    try:
        from config import session_capability

        capability = session_capability.capability_at(now)
        return bool(capability.exit_supported)
    except Exception:  # noqa: BLE001
        logger.warning("could not determine exit capability; deferring the "
                       "entry as the safer direction", exc_info=True)
        return True



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
    from s6_live.candidate_source import S6CandidateSource
    from scanners.base import scan_session

    source = S6CandidateSource(
        trading_day=us_trading_day(now),
        session=scan_session.session_at(),
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

    session = scan_session.session_at()
    return WatchedCandidateSource(
        source, conn=state_db.open_db(),
        session=session, now=now,
        provider=ptv.provider_for(session, broker=broker,
                                  trading_day=us_trading_day(now)),
        budget_seconds=ptv.budget_seconds())


def _funnel(source, results, *, since):
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
    if ready > 0 and submitted == 0:
        label, level, detail = _classify_no_submission(results, executable)
        logger.log(
            level, "%s ready=%d executable=%d submitted=0 -- %s",
            label, ready, executable, detail)
    for symbol, evaluation in sorted(evaluations.items()):
        if not getattr(evaluation, "ready", False):
            logger.info("FUNNEL_WATCHING %s state=%s blocking=%s", symbol,
                        getattr(evaluation, "state", "?"),
                        ",".join(getattr(evaluation, "blocking", ()) or ()) or "-")
    for symbol, reason in (results.get("skipped") or ()):
        logger.info("FUNNEL_SKIPPED %s reason=%s", symbol, reason)
    for symbol, reason in (results.get("blocked") or ()):
        logger.info("FUNNEL_BLOCKED %s reason=%s", symbol, reason)
    for symbol in (results.get("submitted") or ()):
        logger.info("FUNNEL_SUBMITTED %s", symbol)

    _record_shadow_signals(source, results, since=since)


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


def _record_shadow_signals(source, results, *, since):
    """Persist what happened to every candidate this tick.

    Written here, after the cycle, for the same reason the funnel is:
    everything is known and nothing left can be affected by it. A
    candidate refused at a gate otherwise leaves no trace at all, which
    makes "is this gate blocking good trades" a question nobody can
    answer.
    """
    try:
        from config import s6_sessions
        from market_hours import us_trading_day
        from s6_live import shadow_signal_log as ssl

        session = getattr(source, "_session", None) or getattr(
            source, "session", None)
        day = us_trading_day(since)
        evaluations = getattr(source, "evaluations", None) or {}
        blocked = {str(sym): reason
                   for sym, reason in (results.get("blocked") or ())}
        skipped = {str(sym): reason
                   for sym, reason in (results.get("skipped") or ())}
        submitted = {str(s) for s in (results.get("submitted") or ())}

        for symbol, evaluation in sorted(evaluations.items()):
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

            record = ssl.build_record(
                symbol=symbol, session=session, outcome=outcome,
                strategy_id=s6_sessions.STRATEGY_ID,
                features=getattr(evaluation, "features", None),
                candidate=(source.candidate_row(symbol)
                           if hasattr(source, "candidate_row") else None),
                first_blocked_by=first,
                watch_blocking=getattr(evaluation, "blocking", ()),
                now=since)
            ssl.append(record, trading_day=day)
    except Exception:  # noqa: BLE001 -- an observation that fails must
        # not alter a cycle that has already finished trading.
        logger.warning("could not record shadow signals", exc_info=True)

    # In its OWN try. It used to sit inside the block above, after the
    # shadow-signal import -- so when that import was wrong, this never
    # ran either, and two independent observations were lost to one
    # bug. Neither of them can take the other down now.
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
    """
    try:
        from s6_live import closed_bar_shadow, kis_bar_features

        if not session:
            return
        store = kis_bar_features.load_store(session, day)
        if store is None:
            return
        for symbol in symbols:
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

    results = klt.run_live_buy_entry_cycle(
        broker=resolved_broker, candidate_source=source)
    try:
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

    if _s1_is_falling_behind():
        logger.info(
            "%s: S1's executor is behind and holds the account's open "
            "position; a new entry stands down rather than compete with it",
            ENTRY_DEFERRED_S1_STALE)
        return EXIT_OK

    if _exit_in_flight():
        logger.info(
            "%s: an S6 exit is in flight; a position already at risk outranks "
            "a new one, and this tick is dropped rather than queued",
            ENTRY_DEFERRED_EXIT_PENDING)
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
        return EXIT_ERROR

    logger.info(
        "live buy-entry cycle: submitted=%s blocked=%d skipped=%d",
        results["submitted"], len(results["blocked"]), len(results["skipped"]),
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
