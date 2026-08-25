#!/usr/bin/env python3
"""Entry point for the one-shot LIMITED LIVE bootstrap.

Thin on purpose. Every decision -- preconditions, candidate selection,
the transport cap, the UNKNOWN contract -- lives in `live_pilot/
bootstrap.py` where it is unit-testable without a live account. This
file opens the real broker and the real database, calls that module
once, and prints what happened.

Exit codes, because a shell wrapper branches on them:

    0  one BUY placed and verified
    1  blocked before any transport (zero orders)
    3  BUY response ambiguous -- order may be live, UNKNOWN is durable,
       reconciliation required, retry blocked
    4  an unexpected fault after the transport budget was spent

Nothing here is retried. Not the order, not the cancel, not on any exit
code.
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone  # noqa: E402

from brokers.kis_broker import KISBroker  # noqa: E402
from config.live_rollout_config import LiveRolloutConfig  # noqa: E402
from live_pilot import bootstrap  # noqa: E402
from state_store import db as state_db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _print_block(title, mapping):
    print(f"\n{title}")
    for key, value in mapping.items():
        print(f"  {key}: {value}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="One-shot LIMITED LIVE bootstrap: one real BUY of one share")
    # Which STRATEGY's candidate source is asked. Not which symbol -- the
    # symbol is still the single allow-list entry and still cannot be
    # passed in. A bootstrap that took a symbol from the command line
    # would be testing the operator rather than the pipeline.
    parser.add_argument("--strategy", default="s1",
                        choices=sorted(bootstrap.SOURCE_FACTORIES),
                        help="which strategy's candidate source to use")
    args = parser.parse_args(argv)

    print("LIMITED LIVE BOOTSTRAP -- one real BUY of one share, or nothing")
    print(f"  strategy (candidate source): {args.strategy}")
    print(f"  quantity (fixed in code): {bootstrap.BOOTSTRAP_QUANTITY}")
    print(f"  side / type (fixed in code): {bootstrap.BOOTSTRAP_SIDE} / "
          f"{bootstrap.BOOTSTRAP_ORDER_TYPE}")
    print("  transport budget: 1 BUY, 1 CANCEL, 0 retries")

    broker = KISBroker()
    conn = state_db.open_db()
    now = datetime.now(timezone.utc)
    rollout = LiveRolloutConfig.from_env()
    source = bootstrap.SOURCE_FACTORIES[args.strategy](rollout=rollout, now=now)
    try:
        try:
            result = bootstrap.run_bootstrap_buy(
                broker=broker, conn=conn, rollout=rollout, now=now, source=source)
        except bootstrap.BootstrapBlocked as exc:
            _print_block("BLOCKED -- no order was placed", {
                "reason_codes": ", ".join(exc.reason_codes) or "unspecified",
                "detail": str(exc),
            })
            print("\nRESULT: BOOTSTRAP_BLOCKED")
            return 1
        except bootstrap.BootstrapUnknownOrder as exc:
            # The engine has already written durable UNKNOWN and alerted.
            _print_block("UNKNOWN -- the order may be live at KIS", {
                "internal_order_id": exc.internal_order_id,
                "durable_state": "UNKNOWN",
                "RETRY": "BLOCKED",
                "RECONCILIATION_REQUIRED": "true",
                "NEW_ENTRY_BLOCKED": "true",
                "detail": str(exc),
            })
            print("\nRESULT: BOOTSTRAP_UNKNOWN")
            print("Terminating. Run reconciliation against KIS order history "
                  "before anything else touches this account.")
            return 3

        _print_block("Candidate (production scanner, production threshold)",
                     result.candidate.as_dict())
        _print_block("Submit", {
            "status": result.status,
            "broker_order_id": result.broker_order_id or "unavailable",
            "buy_transport_calls": result.guard.submit_calls,
        })

        verification = bootstrap.verify_buy(broker=broker, conn=conn, result=result)
        result.verification = verification
        _print_block("Verification (KIS is the authority, not the submit response)",
                     verification)

        cancel = bootstrap.cancel_if_open(
            conn=conn, result=result, verification=verification,
            order_intent=result.order_intent,
            account_id=os.environ.get("KIS_ALLOWED_ACCOUNT_NO", ""),
        )
        _print_block("Cancel", cancel)

        print("\nRESULT: BOOTSTRAP_COMPLETED")
        print(f"  BUY transports: {result.guard.submit_calls} (budget 1)")
        print(f"  CANCEL transports: {result.guard.cancel_calls} (budget 1)")
        print("  Confirm order_path / order_tr_id_live_buy (and the cancel "
              "values, if a cancel actually went out) from the OBSERVED "
              "responses only -- never from documentation.")
        return 0
    except Exception:  # noqa: BLE001
        logging.exception("bootstrap faulted")
        print("\nRESULT: BOOTSTRAP_FAULTED")
        print("Do NOT re-run. Reconcile against KIS order history first.")
        return 4
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
