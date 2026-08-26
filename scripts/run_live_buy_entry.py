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

    return S6CandidateSource(
        trading_day=us_trading_day(now),
        session=scan_session.session_at(),
        rollout=rollout,
    )


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
    return klt.run_live_buy_entry_cycle(
        broker=broker or KISBroker(), candidate_source=source)


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

    try:
        results = run_once(strategy=args.strategy)
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
