#!/usr/bin/env python3
"""Read-only KIS response verification for the Oracle host.

Answers the one question the LIVE_RESPONSE_PENDING matrix cannot answer
from source alone: does a REAL KIS response actually have the shape this
code assumes? It exists because Oracle verification found that it often
does not -- a wrong exchange code returns success with an empty price,
and consecutive reads trip a per-second cap.

This script CANNOT place, amend or cancel an order. Not "does not":
it never imports execution.execution_engine, and the broker handle it
uses is wrapped in a read-only proxy that raises on any state-mutating
method. That is a stronger guarantee than relying on
KIS_LIVE_ORDER_ENABLED=false, which is also checked.

Output is PASS/FAIL per check with a reason code. Symbols and exchanges
are shown; tokens, account numbers and raw responses are not.

Exit codes:
    0  every check passed
    1  a structural error (bad configuration, unusable environment)
    2  at least one check FAILED -- a real KIS response did not match
"""

import argparse
import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from brokers import kis_rate_limiter, kis_token_cache  # noqa: E402
from brokers.kis_broker import (  # noqa: E402
    LIVE_RESPONSE_PENDING,
    VERIFICATION_MATRIX,
    KISBroker,
    KISPriceUnavailableError,
)
from execution.secret_redaction import install_logging_redaction  # noqa: E402
from market_data.exchange_registry import (  # noqa: E402
    ExchangeResolutionError,
    build_kis_instrument,
)

logger = logging.getLogger("verify_kis")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_FAILED = 2

# Anything that could change state. The proxy below refuses all of them.
FORBIDDEN_METHODS = ("submit_order", "cancel_order", "amend_order", "revise_order")


class ReadOnlyViolation(Exception):
    """Raised if this script ever reaches a state-mutating broker method."""


class ReadOnlyBroker:
    """Exposes only the read methods. Any order/cancel attempt raises
    before a request is built, let alone sent."""

    ALLOWED = frozenset({
        "get_current_price", "get_account_snapshot", "get_positions",
        "get_open_orders", "get_fills", "get_orderable_usd", "config",
    })

    def __init__(self, broker):
        self._broker = broker

    def __getattr__(self, name):
        if name in FORBIDDEN_METHODS:
            raise ReadOnlyViolation(
                f"{name}() is not reachable from the read-only verifier"
            )
        if name not in self.ALLOWED:
            raise ReadOnlyViolation(f"{name} is not on the read-only allow-list")
        return getattr(self._broker, name)


class Results:
    def __init__(self):
        self.rows = []

    def record(self, name, ok, *, reason_code=None, detail=None):
        self.rows.append({
            "check": name, "ok": bool(ok),
            "reason_code": reason_code, "detail": detail,
        })
        status = "PASS" if ok else "FAIL"
        suffix = ""
        if reason_code:
            suffix += f" [{reason_code}]"
        if detail:
            suffix += f" {detail}"
        print(f"[{status}] {name}{suffix}")

    @property
    def failures(self):
        return [row for row in self.rows if not row["ok"]]


def mask_account(value):
    text = str(value or "")
    return text[:2] + "*" * max(len(text) - 2, 0)


def check_safety_posture(broker, results):
    config = broker.config
    live_enabled = bool(getattr(config, "live_order_enabled", False))
    results.record(
        "safety_live_order_disabled", not live_enabled,
        reason_code=None if not live_enabled else "LIVE_ORDER_ENABLED",
        detail="KIS_LIVE_ORDER_ENABLED=false" if not live_enabled else "MUST be false",
    )
    read_ok = bool(getattr(config, "account_read_enabled", False))
    results.record("safety_account_read_enabled", read_ok,
                   reason_code=None if read_ok else "ACCOUNT_READ_DISABLED")
    results.record("account_identified", bool(getattr(config, "account_no", "")),
                   detail=f"account={mask_account(getattr(config, 'account_no', ''))} "
                          f"env={getattr(config, 'kis_env', '?')}")


def check_token_cache(results):
    path = kis_token_cache.cache_file()
    existed = path.exists()
    results.record("token_cache_path", True,
                   detail=f"{path} ({'hit' if existed else 'miss'})")
    if existed:
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            results.record("token_cache_readable", False, reason_code="TOKEN_CACHE_CORRUPT")
            return
        leaked = [k for k in stored if k in ("app_key", "app_secret", "account_no")]
        results.record("token_cache_stores_no_secret", not leaked,
                       reason_code=None if not leaked else "TOKEN_CACHE_LEAK",
                       detail=f"fields={sorted(stored)}")


def check_symbol(broker, symbol, results):
    try:
        instrument, record = build_kis_instrument(symbol)
    except ExchangeResolutionError as exc:
        results.record(f"exchange:{symbol}", False, reason_code=exc.reason_code,
                       detail=str(exc))
        return
    results.record(
        f"exchange:{symbol}", True,
        detail=f"canonical={record.exchange.value} "
               f"kis_code={record.kis_exchange_code} source={record.source}",
    )
    try:
        price = broker.get_current_price(instrument)
    except KISPriceUnavailableError as exc:
        results.record(f"price:{symbol}", False, reason_code=exc.reason_code,
                       detail=str(exc.diagnostic()))
        return
    except Exception as exc:  # noqa: BLE001 -- diagnostics must not abort
        results.record(f"price:{symbol}", False, reason_code="PRICE_READ_FAILED",
                       detail=type(exc).__name__)
        return
    results.record(f"price:{symbol}", True,
                   detail=f"last={price} via EXCD={record.kis_exchange_code}")


def check_account_reads(broker, results):
    try:
        snapshot = broker.get_account_snapshot()
    except Exception as exc:  # noqa: BLE001
        results.record("balance", False, reason_code=getattr(exc, "reason_code", "BALANCE_FAILED"),
                       detail=type(exc).__name__)
    else:
        results.record("balance", True,
                       detail=f"orderable_usd={snapshot.usd_orderable_cash} "
                              f"account={mask_account(snapshot.account_id)}")
    for name, call in (
        ("positions", broker.get_positions),
        ("open_orders", broker.get_open_orders),
    ):
        try:
            rows = call()
        except Exception as exc:  # noqa: BLE001
            results.record(name, False,
                           reason_code=getattr(exc, "reason_code", f"{name.upper()}_FAILED"),
                           detail=type(exc).__name__)
            continue
        # An EMPTY result is a valid answer, not an error and not UNKNOWN.
        results.record(name, isinstance(rows, list),
                       detail=f"count={len(rows)} (empty is valid)")


def check_fills(broker, results, *, start_date, end_date):
    try:
        fills = broker.get_fills(start_date=start_date, end_date=end_date)
    except Exception as exc:  # noqa: BLE001
        results.record("fills", False, reason_code=getattr(exc, "reason_code", "FILLS_FAILED"),
                       detail=type(exc).__name__)
        return
    results.record("fills", isinstance(fills, list),
                   detail=f"count={len(fills)} window={start_date}..{end_date}")


def report_pending(results):
    pending = [v for v in VERIFICATION_MATRIX if v.live_status == LIVE_RESPONSE_PENDING]
    results.record("live_response_pending_inventory", True,
                   detail=f"{len(pending)} value(s) still unconfirmed by a real response")
    for item in pending:
        print(f"    - {item.name} = {item.value}")
    return pending


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only KIS response verification (places no orders)")
    parser.add_argument("--symbols", default="AAPL",
                        help="comma-separated symbols to price-check")
    parser.add_argument("--days", type=int, default=7, help="fill-history window")
    parser.add_argument("--log-level", default="WARNING")
    parser.add_argument("--json", action="store_true", help="emit the results as JSON")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    install_logging_redaction()

    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    results = Results()

    try:
        broker = ReadOnlyBroker(KISBroker())
    except Exception as exc:  # noqa: BLE001 -- configuration problem
        logger.error("cannot construct a KIS client: %s", type(exc).__name__)
        return EXIT_ERROR

    # Prove the read-only wrapper actually refuses, rather than assuming.
    for method in FORBIDDEN_METHODS:
        try:
            getattr(broker, method)
        except ReadOnlyViolation:
            continue
        results.record(f"readonly_guard:{method}", False, reason_code="ORDER_PATH_REACHABLE")
    results.record("readonly_guard", True,
                   detail=f"{len(FORBIDDEN_METHODS)} state-mutating methods unreachable")

    check_safety_posture(broker, results)
    check_token_cache(results)

    interval = kis_rate_limiter.min_interval_for(kis_rate_limiter.CATEGORY_READ)
    results.record("read_pacing_configured", interval > 0,
                   reason_code=None if interval > 0 else "NO_READ_PACING",
                   detail=f"min interval {interval}s between KIS reads")

    for symbol in [s.strip().upper() for s in args.symbols.split(",") if s.strip()]:
        check_symbol(broker, symbol, results)

    check_account_reads(broker, results)
    check_fills(broker, results,
                start_date=(now - timedelta(days=args.days)).strftime("%Y%m%d"),
                end_date=now.strftime("%Y%m%d"))
    report_pending(results)

    if args.json:
        print(json.dumps(results.rows, indent=2, default=str))

    failures = results.failures
    print(f"\n{len(results.rows) - len(failures)}/{len(results.rows)} checks passed")
    if failures:
        print("FAILED checks:")
        for row in failures:
            print(f"  - {row['check']} [{row['reason_code']}]")
        return EXIT_FAILED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
