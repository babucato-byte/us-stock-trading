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
from execution.secret_redaction import install_logging_redaction  # noqa: E402

logger = logging.getLogger("live_buy_entry")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 3


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


def main(argv=None):
    parser = argparse.ArgumentParser(description="KIS live buy-entry cycle")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    install_logging_redaction()

    reason = refusal_reason()
    if reason is not None:
        logger.error("refusing to run the live buy-entry cycle: %s", reason)
        return EXIT_REFUSED

    try:
        results = klt.run_live_buy_entry_cycle(broker=KISBroker())
    except klt.KISLiveTradingError as exc:
        logger.error("live buy-entry cycle refused to run: %s", exc)
        return EXIT_REFUSED
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
