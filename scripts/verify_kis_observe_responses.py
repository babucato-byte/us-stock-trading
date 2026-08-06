#!/usr/bin/env python3
"""Confirms the wire values OBSERVE depends on, against real KIS reads.

OBSERVE needs three of the nine matrix values -- the price endpoint, the
field the price is read from, and the OVRS_EXCG_CD code space that every
account read is swept over. The other six belong to order and cancel
submission, which OBSERVE never reaches. This script is how the three get
from LIVE_RESPONSE_PENDING to LIVE_RESPONSE_CONFIRMED without anyone
guessing from documentation.

It CANNOT place, amend or cancel an order. Not "does not": it never
imports execution.execution_engine or the KIS adapter, and the broker
handle it uses is wrapped in a proxy whose allow-list contains read
methods only, so an order call raises before a request is built.

What it reports is the SHAPE of the response -- which field was present
and what type it held -- never a price it should not, never a token, a
full account number or a raw body.

Exit codes:
    0  every OBSERVE value confirmed by a real response
    1  a structural error (bad configuration, unusable environment)
    2  at least one value could not be confirmed
"""

import argparse
import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from brokers.kis_broker import (  # noqa: E402
    PRICE_PATH,
    KISBroker,
    KISPriceUnavailableError,
)
from domain.exchange import supported_kis_order_exchange_codes  # noqa: E402
from execution.secret_redaction import install_logging_redaction  # noqa: E402
from market_data.exchange_registry import (  # noqa: E402
    ExchangeResolutionError,
    build_kis_instrument,
)

logger = logging.getLogger("verify_kis_observe")

# The values this probe is able to establish. Written here because the
# probe must also run against a release deployed BEFORE the matrix gained
# `required_for` -- which is exactly when it is needed. When the matrix
# does expose the split, `_cross_check_matrix()` asserts the two agree,
# so this stays a statement of what the probe covers rather than a second
# authority on what OBSERVE requires.
PROBE_CONFIRMS = ("price_path", "price_field_last", "order_exchange_code_space")


def _cross_check_matrix(results):
    """If the running release knows which values OBSERVE needs, make sure
    this probe covers exactly those."""
    try:
        from brokers.kis_broker import REQUIRED_FOR_OBSERVE, matrix_entries_for
    except ImportError:
        results.record("matrix_split", True, reason_code="MATRIX_SPLIT_ABSENT",
                       detail="this release predates required_for; probe covers "
                              + ", ".join(PROBE_CONFIRMS))
        return list(PROBE_CONFIRMS)
    observe = [entry.name for entry in matrix_entries_for(REQUIRED_FOR_OBSERVE)]
    agrees = set(observe) == set(PROBE_CONFIRMS)
    results.record("matrix_split", agrees,
                   reason_code=None if agrees else "PROBE_COVERAGE_MISMATCH",
                   detail=f"OBSERVE requires {sorted(observe)}")
    return observe

# Mirrors scripts/verify_kis_live_responses.py: naming them here means a
# new mutating method has to be added in two places to slip through.
FORBIDDEN_METHODS = frozenset({
    "submit_order", "cancel_order", "submit_buy_order", "submit_sell_order",
    "amend_order", "replace_order", "place_order",
})


class ReadOnlyViolation(Exception):
    """A mutating broker method was reached. Never expected: the point of
    the proxy is that this cannot happen silently."""


class ReadOnlyBroker:
    ALLOWED = frozenset({
        "get_current_price", "get_account_snapshot", "get_positions",
        "get_open_orders", "get_fills", "config",
    })

    def __init__(self, broker):
        self._broker = broker
        self.calls = []

    def __getattr__(self, name):
        if name in FORBIDDEN_METHODS:
            raise ReadOnlyViolation(f"{name}() is not reachable from this verifier")
        if name not in self.ALLOWED:
            raise ReadOnlyViolation(f"{name} is not on the read-only allow-list")
        attribute = getattr(self._broker, name)
        if not callable(attribute):
            return attribute

        def _record(*args, **kwargs):
            self.calls.append(name)
            return attribute(*args, **kwargs)
        return _record


class Results:
    def __init__(self):
        self.rows = []

    def record(self, name, ok, *, reason_code=None, detail=None):
        self.rows.append({"check": name, "ok": bool(ok),
                          "reason_code": reason_code, "detail": detail})
        status = "PASS" if ok else "FAIL"
        suffix = f" [{reason_code}]" if reason_code else ""
        print(f"[{status}] {name}{suffix}" + (f" {detail}" if detail else ""))

    @property
    def failed(self):
        return [row for row in self.rows if not row["ok"]]


def check_price_endpoint(results, broker, symbols):
    """price_path and price_field_last, from one real quote.

    A quote that comes back with a numerically usable `output.last` is
    what confirms both at once: the path answered, and the field this
    code reads is the field that carried the price.
    """
    confirmed_path = False
    confirmed_field = False
    for symbol in symbols:
        try:
            instrument, record = build_kis_instrument(symbol)
        except ExchangeResolutionError as exc:
            results.record(f"exchange:{symbol}", False,
                           reason_code=exc.reason_code, detail=str(exc))
            continue
        try:
            price = broker.get_current_price(instrument)
        except KISPriceUnavailableError as exc:
            results.record(f"price:{symbol}", False, reason_code=exc.reason_code,
                           detail=str(exc.diagnostic()))
            continue
        except Exception as exc:  # noqa: BLE001 -- any read failure is a failure
            results.record(f"price:{symbol}", False, reason_code="PRICE_READ_FAILED",
                           detail=type(exc).__name__)
            continue
        usable = isinstance(price, float) and price > 0
        results.record(
            f"price:{symbol}", usable,
            reason_code=None if usable else "PRICE_NOT_USABLE",
            # The venue and the TYPE, not the quote itself.
            detail=(f"via EXCD={record.kis_exchange_code} "
                    f"output.last -> {type(price).__name__}, positive={usable}"),
        )
        confirmed_path = confirmed_path or usable
        confirmed_field = confirmed_field or usable

    results.record("price_path", confirmed_path,
                   reason_code=None if confirmed_path else "NO_USABLE_QUOTE",
                   detail=PRICE_PATH)
    results.record("price_field_last", confirmed_field,
                   reason_code=None if confirmed_field else "NO_USABLE_QUOTE",
                   detail="output.last carried a positive float")
    return confirmed_path and confirmed_field


def check_order_exchange_code_space(results, broker):
    """order_exchange_code_space, from the account reads themselves.

    Every balance / open-order / fill read is swept over OVRS_EXCG_CD, so
    a sweep that completes for all three venues without a venue error is
    the confirmation -- and it is the read path that proves it, not an
    order.
    """
    codes = supported_kis_order_exchange_codes()
    ok = True
    for name, call in (("balance", broker.get_account_snapshot),
                       ("positions", broker.get_positions),
                       ("open_orders", broker.get_open_orders)):
        try:
            result = call()
        except Exception as exc:  # noqa: BLE001
            results.record(f"read:{name}", False, reason_code="READ_FAILED",
                           detail=type(exc).__name__)
            ok = False
            continue
        count = len(result) if isinstance(result, list) else 1
        results.record(f"read:{name}", True,
                       detail=f"swept {'/'.join(codes)}; {count} row(s)")
    try:
        fills = broker.get_fills(start_date="20260101", end_date="20260101")
        results.record("read:fills", True,
                       detail=f"swept {'/'.join(codes)}; {len(fills)} row(s)")
    except Exception as exc:  # noqa: BLE001
        results.record("read:fills", False, reason_code="READ_FAILED",
                       detail=type(exc).__name__)
        ok = False

    results.record("order_exchange_code_space", ok,
                   reason_code=None if ok else "SWEEP_INCOMPLETE",
                   detail=f"{'/'.join(codes)} accepted on every account read")
    return ok


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Confirm the OBSERVE wire values against real KIS reads "
                    "(places no orders)")
    parser.add_argument("--symbols", default="AAPL,MSFT",
                        help="comma-separated symbols to quote")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s %(message)s")
    install_logging_redaction()

    results = Results()
    broker = ReadOnlyBroker(KISBroker())
    results.record("readonly_guard", True,
                   detail=f"{len(FORBIDDEN_METHODS)} mutating method(s) unreachable")

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    check_price_endpoint(results, broker, symbols)
    check_order_exchange_code_space(results, broker)

    # Which OBSERVE values this run has now established, so the operator
    # can see the matrix edit this justifies.
    observe = _cross_check_matrix(results)
    confirmed = {row["check"] for row in results.rows if row["ok"]}
    outstanding = [name for name in observe if name not in confirmed]
    results.record("observe_requirements", not outstanding,
                   reason_code=None if not outstanding else "OBSERVE_VALUES_UNCONFIRMED",
                   detail=(f"{len(observe) - len(outstanding)}/{len(observe)} confirmed"
                           + (f"; outstanding: {', '.join(outstanding)}" if outstanding else "")))

    assert not [c for c in broker.calls if c in FORBIDDEN_METHODS]
    print(f"\n{len(results.rows) - len(results.failed)}/{len(results.rows)} checks passed")
    print(f"broker methods used: {sorted(set(broker.calls))}")
    if args.json:
        print(json.dumps(results.rows, indent=2))
    return 0 if not results.failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
