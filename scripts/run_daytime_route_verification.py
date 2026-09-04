#!/usr/bin/env python3
"""Entry point for the one-shot DAYTIME route verification.

Thin on purpose, exactly like `run_limited_live_bootstrap.py`. Every
decision -- the window, the allow-list, the price, the branches, the
disarm -- lives in `live_pilot/route_verification_runner.py` where it is
unit-testable without a live account. This file opens the real broker and
the real database, calls that function once, prints what happened, and
disarms.

What this CLI CANNOT express
----------------------------
There is no flag for side, quantity, order type, session, price or
symbol, and none for force, retry or bypass. The shape is fixed in code
(BUY / 1 / LIMIT / OVERNIGHT_DAYTIME) and the symbol comes from the
configured one-symbol allow-list. A CLI that could name the symbol would
be testing the operator rather than the wire.

It also never calls `broker.submit_order`. The order goes through
`execution_engine`, which is the sole real-order boundary, and
`tests/test_execution_boundary.py` enforces that this file is not an
exception to it.

Exit codes, because a shell wrapper branches on them:

    0  the route was verified and the account is flat
    1  blocked before any transport (zero orders)
    3  the BUY response was ambiguous -- UNKNOWN is durable, reconciliation
       required, retry blocked
    4  the BUY filled and could not be flattened -- REAL EXPOSURE remains
    5  the run finished but disarming failed -- the one-shot may still be
       armed and a human must clear it

Nothing is retried. Not the order, not the cancel, not the flatten, and
not on any exit code.
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone  # noqa: E402

from brokers.kis_broker import KISAmbiguousResponseError, KISBroker  # noqa: E402
from config.live_rollout_config import LiveRolloutConfig  # noqa: E402
from execution.secret_redaction import install_logging_redaction  # noqa: E402
from live_pilot import route_verification_runner as runner  # noqa: E402
from state_store import db as state_db  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

#: Where the shared environment lives. The disarm writes here, atomically,
#: the same way a deploy switches the release.
DEFAULT_ENV_PATH = (
    "/home/ubuntu/releases/us-stock-trading/shared/env/kis-readonly.env")


def _print(title, mapping):
    print(f"\n{title}")
    for key, value in mapping.items():
        print(f"  {key}: {value}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="One-shot DAYTIME route verification: one real BUY of "
                    "one share, priced to rest, then cancelled")
    # The ONLY argument, and it names a file rather than an order.
    parser.add_argument("--env-path", default=os.environ.get(
        "SCANNER_SHARED_ENV", DEFAULT_ENV_PATH),
        help="the shared env file to disarm on completion")
    args = parser.parse_args(argv)
    install_logging_redaction()

    print("DAYTIME ROUTE VERIFICATION -- one real BUY of one share, or nothing")
    print("  side / qty / type (fixed in code): "
          f"{runner.capability_mod.VERIFICATION_SIDE} / "
          f"{runner.capability_mod.VERIFICATION_QUANTITY} / "
          f"{runner.capability_mod.VERIFICATION_ORDER_TYPE}")
    print(f"  session (fixed in code): "
          f"{runner.capability_mod.VERIFICATION_SESSION}")
    print("  transport budget: 1 BUY, 1 CANCEL, 1 FLATTEN, 0 retries")

    broker = KISBroker()
    conn = state_db.open_db()
    rollout = LiveRolloutConfig.from_env()
    account_id = broker.get_account_snapshot().account_id
    exit_code = 0

    try:
        report = runner.run_route_verification(
            broker=broker, conn=conn,
            allowed_symbols=rollout.allowed_symbols or (),
            account_id=account_id, now=datetime.now(timezone.utc))
        _print("Result", report)
        print(f"\nRESULT: {report.get('conclusion')}")
    except runner.RouteVerificationBlocked as exc:
        _print("BLOCKED -- no order was placed", {
            "reason_codes": ", ".join(exc.reason_codes) or "unspecified",
            "detail": str(exc)})
        print("\nRESULT: ROUTE_VERIFICATION_BLOCKED")
        exit_code = 1
    except KISAmbiguousResponseError as exc:
        _print("UNKNOWN -- the BUY may be live at KIS", {
            "durable_state": "UNKNOWN", "RETRY": "BLOCKED",
            "RECONCILIATION_REQUIRED": "true", "detail": str(exc)})
        print("\nRESULT: ROUTE_VERIFICATION_UNKNOWN")
        print("Run reconciliation against KIS order history before anything "
              "else touches this account.")
        exit_code = 3
    except runner.RouteVerificationExposed as exc:
        _print("EXPOSURE -- the BUY filled and was not flattened", {
            "remaining_quantity": exc.remaining_qty,
            "adopted_position_id": exc.position_id or "NOT ADOPTED",
            "detail": str(exc)})
        print("\nRESULT: ROUTE_VERIFICATION_EXPOSED")
        exit_code = 4
    finally:
        # Disarm on EVERY terminal path, including the exceptional ones.
        # An armed one-shot left behind is a second order waiting for the
        # next person who runs this.
        try:
            outcome = runner.disarm(args.env_path)
            _print("Disarmed", outcome)
        except Exception as exc:  # noqa: BLE001
            print(f"\nDISARM FAILED: {type(exc).__name__}: {exc}")
            print("The verification flags may STILL BE ARMED. Clear "
                  f"{runner.capability_mod.FLAG_ENABLED}, "
                  f"{runner.capability_mod.FLAG_ACK} and "
                  f"{runner.ALLOWLIST_KEY} by hand before anything else runs.")
            exit_code = max(exit_code, 5)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
