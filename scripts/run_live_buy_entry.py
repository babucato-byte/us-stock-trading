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


def _exit_in_flight():
    """True when any S6 position has an exit submitted or pending."""
    try:
        from s6_live import position_store
        from state_store import db as state_db

        with state_db.open_db() as conn:
            for _pid, row in position_store.load_live(conn):
                if row.get("exit_submitted") or row.get("pending_exit_reason"):
                    return True
        return False
    except Exception:  # noqa: BLE001 -- an unreadable store is not a
        # reason to refuse the entry; the gate and the runtime have their
        # own, stronger refusals, and failing the tick over a diagnostic
        # would stop trading for the wrong reason.
        logger.warning("could not check for exits in flight", exc_info=True)
        return False


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
    "s1": lambda rollout, now: None,  # None -> the cycle's own default
    "s6": lambda rollout, now: _s6_source(rollout, now),
}


def _s6_source(rollout, now):
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

    return WatchedCandidateSource(
        source, conn=state_db.open_db(),
        session=scan_session.session_at(), now=now)


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
            executable = conn.execute(
                "SELECT COUNT(*) FROM shadow_audit_events "
                "WHERE event_type = ? AND created_at >= ?",
                (shadow_audit.GATE_APPROVED, since.isoformat()),
            ).fetchone()[0]
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 -- reporting must never affect trading
        executable = -1

    logger.info(
        "FUNNEL scanned=%d watching=%d ready=%d executable=%s submitted=%d",
        scanned, watching, ready,
        "unavailable" if executable < 0 else executable, submitted)
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


def _record_shadow_signals(source, results, *, since):
    """Persist what happened to every candidate this tick.

    Written here, after the cycle, for the same reason the funnel is:
    everything is known and nothing left can be affected by it. A
    candidate refused at a gate otherwise leaves no trace at all, which
    makes "is this gate blocking good trades" a question nobody can
    answer.
    """
    try:
        from market_hours import us_trading_day
        from s6_live import shadow_signal_log as ssl
        from s6_live import s6_sessions

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

    # §16: READY candidates that reached the gate and were approved, and
    # then produced no order, is the one combination that is a defect
    # rather than a market condition.
    if ready > 0 and executable > 0 and submitted == 0:
        logger.error(
            "EXECUTION_DEFECT_SUSPECTED ready=%d executable=%d submitted=0 -- "
            "the gate approved an order that was never submitted", ready, executable)


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
    source = factory(LiveRolloutConfig.from_env(), now)

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

    results = klt.run_live_buy_entry_cycle(
        broker=broker or KISBroker(), candidate_source=source)
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
