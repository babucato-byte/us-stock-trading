#!/usr/bin/env python3
"""Terminally release a SUBMITTING row whose order provably never
reached the broker.

Why this needs a command at all
-------------------------------
The engine advances an order to SUBMITTING immediately BEFORE the
transport call, so that a crash mid-order is recoverable. That is the
right ordering -- but it means a refusal raised by the broker's own
pre-network guard leaves a row saying "may be in flight" about an order
that was never sent. That row then holds a pending-position slot and the
day's entry slot against nothing.

The engine now classifies that case itself (REASON_PRE_TRANSPORT_CONFIG),
so new occurrences self-release. This command exists for rows written
before that fix, and for any future case where the classification could
not be made in-process.

The dangerous mistake this refuses to make
------------------------------------------
A SUBMITTING row usually means the OPPOSITE: the transport was attempted
and we do not know the outcome. Releasing one of those as REJECTED would
declare a possibly-live order dead, free the entry slot, and invite a
second order for the same signal. So this command does not take anyone's
word for it -- not the caller's, not a log's:

  * the row must still be SUBMITTING with a NULL broker_order_id;
  * KIS itself must show no open order for the symbol;
  * KIS itself must show no position in the symbol;
  * KIS fill history for the trading day must contain no fill.

Any of those failing, or any of those reads failing, and it refuses.
"Unreadable" is not "absent" -- a read that errors is a refusal, because
the whole point is to distinguish definitely-never-sent from unknown.

It never writes SQL directly: the transition goes through
order_repository.advance(), so the state machine and the append-only
event history apply exactly as they would for any other transition.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brokers.kis_broker import KISBroker  # noqa: E402
from execution import order_repository  # noqa: E402
from execution.execution_engine import REASON_PRE_TRANSPORT_CONFIG  # noqa: E402
from state_store import db as state_db  # noqa: E402

RELEASABLE_STATE = "SUBMITTING"


def _evidence(broker, symbol, trading_date):
    """Everything KIS knows about this symbol right now. Any read that
    raises makes the whole check fail closed."""
    findings = {}

    open_orders = broker.get_open_orders() or []
    findings["open_orders"] = [
        o for o in open_orders if (o.get("pdno") or o.get("PDNO")) == symbol
    ]

    positions = broker.get_positions() or []
    findings["positions"] = [
        {"symbol": p.symbol, "quantity": p.quantity}
        for p in positions if p.symbol == symbol and p.quantity
    ]

    fills = broker.get_fills(start_date=trading_date, end_date=trading_date) or []
    findings["fills"] = [
        f for f in fills if (f.get("pdno") or f.get("PDNO")) == symbol
    ]
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("internal_order_id")
    parser.add_argument("--confirm", action="store_true",
                        help="actually perform the release; without it this is a dry run")
    args = parser.parse_args()

    conn = state_db.open_db()
    try:
        record = order_repository.load(conn, args.internal_order_id)
        if record is None:
            print(f"REFUSED: no durable record for {args.internal_order_id}")
            return 1

        row = conn.execute(
            "SELECT symbol, side, status, broker_order_id, trading_date "
            "FROM kis_order_idempotency WHERE internal_order_id = ?",
            (args.internal_order_id,),
        ).fetchone()
        if row is None:
            print(f"REFUSED: no idempotency row for {args.internal_order_id}")
            return 1

        symbol = row["symbol"]
        print(f"  internal_order_id : {args.internal_order_id}")
        print(f"  symbol / side     : {symbol} / {row['side']}")
        print(f"  status            : {row['status']}")
        print(f"  broker_order_id   : {row['broker_order_id']!r}")
        print(f"  trading_date      : {row['trading_date']}")

        if row["status"] != RELEASABLE_STATE:
            print(f"REFUSED: status is {row['status']}, not {RELEASABLE_STATE}")
            return 1
        if row["broker_order_id"]:
            print("REFUSED: a broker_order_id is present, so the order DID reach KIS")
            return 1

        print("\n  Asking KIS directly (a read that fails is a refusal):")
        try:
            findings = _evidence(KISBroker(), symbol, row["trading_date"])
        except Exception as exc:  # noqa: BLE001
            print(f"REFUSED: could not establish KIS state ({type(exc).__name__}: {exc})")
            print("         unreadable is not the same as absent")
            return 1

        for name, items in findings.items():
            print(f"    {name:12s}: {len(items)}")
        if any(findings.values()):
            print("\nREFUSED: KIS shows a trace of this symbol. This row is NOT a "
                  "definitively-unsent order; leave it for reconciliation.")
            return 1

        print("\n  KIS shows no open order, no position and no fill.")
        print("  -> the transport was definitively not attempted")

        if not args.confirm:
            print("\nDRY RUN: re-run with --confirm to release it")
            return 0

        released = order_repository.advance(
            conn, record, "REJECTED",
            event_type="TRANSPORT_NOT_ATTEMPTED",
            event_payload={
                "reason": REASON_PRE_TRANSPORT_CONFIG,
                "transport_attempted": False,
                "evidence": "kis open_orders=0 positions=0 fills=0",
            },
        )
        print(f"\nRELEASED: {args.internal_order_id} -> {released.state}")
        print("  broker_order_id stays NULL, so entry_limits treats this as an "
              "attempt that never reached the broker and frees both slots.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
