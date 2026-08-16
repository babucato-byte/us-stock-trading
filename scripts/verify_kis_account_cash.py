#!/usr/bin/env python3
"""Does KIS report an account-level USD cash or equity figure? (PHASE 4C)

Why this exists
---------------
`brokers/kis_broker.py` implements six endpoints and none of them carries
an account cash balance -- the matrix records
`balance_cash_fields_absent` as LIVE_RESPONSE_CONFIRMED, from a real
probe: TTTS3012R's `output2` returned nine purchase/valuation/P&L fields
and no deposit at all. Without cash there is no equity, and without
equity the daily-loss and drawdown limits cannot be measured, so
`s1_live/risk_state.py` blocks every new entry.

The official KIS repository (koreainvestment/open-trading-api) lists
three account-inquiry endpoints this wrapper does NOT implement:

    CTRP6504R  inquire-present-balance     체결기준현재잔고
    CTRP6010R  inquire-paymt-stdr-balance  결제기준잔고
    TTTC2101R  foreign-margin              해외증거금 통화별조회

The endpoints and TR ids are confirmed by that repository. The RESPONSE
FIELD NAMES are not: the official sample code frames whatever comes back
without declaring a schema, and the API portal is a JavaScript shell. So
the fields are established the only way ORACLE-CASH-01 was -- by asking
the real account and writing down what it answered.

Read-only, structurally
-----------------------
It never imports the execution engine or the KIS adapter, and the broker
handle is wrapped in a proxy whose allow-list holds reads only, so an
order call raises before a request is built. The three endpoints probed
here are GET queries.

What it prints
--------------
FIELD NAMES and value SHAPES. Never a token, never a full account
number, never a raw body, and never a balance: a numeric field is
reported as `positive` / `zero` / `negative` with its magnitude in
digits, which is enough to tell "this field carries real money" from
"this field is absent or empty" without putting the balance in a log.

Exit codes
    0  at least one usable account-level cash or equity field was found
    1  a structural error (configuration, environment)
    2  probed successfully and found no usable field
"""

import argparse
import json
import logging
import math
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger("verify_kis_account_cash")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_FOUND = 2

FORBIDDEN_METHODS = frozenset({
    "submit_order", "cancel_order", "submit_buy_order", "submit_sell_order",
    "amend_order", "replace_order", "place_order",
})

#: Endpoints probed. Path and tr_id are from the official
#: koreainvestment/open-trading-api repository; the fields are what this
#: script is here to discover.
PROBES = (
    {
        "name": "inquire_present_balance",
        "path": "/uapi/overseas-stock/v1/trading/inquire-present-balance",
        "tr_id": {"live": "CTRP6504R", "paper": "VTRP6504R"},
        "params": {"WCRC_FRCR_DVSN_CD": "02", "NATN_CD": "840",
                   "TR_MKET_CD": "00", "INQR_DVSN_CD": "00"},
        "describe": "체결기준현재잔고 (foreign currency, USA)",
    },
    {
        "name": "foreign_margin",
        "path": "/uapi/overseas-stock/v1/trading/foreign-margin",
        "tr_id": {"live": "TTTC2101R", "paper": "TTTC2101R"},
        "params": {},
        "describe": "해외증거금 통화별조회",
    },
    {
        "name": "inquire_paymt_stdr_balance",
        "path": "/uapi/overseas-stock/v1/trading/inquire-paymt-stdr-balance",
        "tr_id": {"live": "CTRP6010R", "paper": "CTRP6010R"},
        "params": {"BASS_DT": "", "WCRC_FRCR_DVSN_CD": "02", "INQR_DVSN_CD": "00"},
        "describe": "결제기준잔고",
    },
)

#: Substrings that mark a field as cash-like or equity-like. Used only to
#: HIGHLIGHT candidates in the output -- every field name is printed
#: regardless, so nothing is hidden by a pattern that failed to match.
CASH_HINTS = ("dncl", "psbl", "cash", "deposit", "d2", "wdrw")
EQUITY_HINTS = ("tot_asst", "evlu_amt", "evlu_pfls", "asst", "nass", "tot_evlu")


class ReadOnlyViolation(Exception):
    """A mutating broker method was reached. The proxy exists so this
    cannot happen silently."""


class ReadOnlyBroker:
    """Reads only. `_get` is allowed because these three endpoints have no
    wrapper method yet -- that is the whole point of the probe -- and it
    is a GET helper that cannot construct an order."""

    ALLOWED = frozenset({
        "get_current_price", "get_account_snapshot", "get_positions",
        "get_open_orders", "get_orderable_usd", "config", "_get", "_env_key",
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


def shape(value) -> str:
    """A value's shape, never its content.

    A balance is money. What this needs to establish is whether a field
    exists and carries a real number, which `positive/12345 -> 5 digits`
    answers without printing the amount.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return f"bool({value})"
    if isinstance(value, (list, tuple)):
        return f"list[{len(value)}]"
    if isinstance(value, dict):
        return f"dict[{len(value)}]"
    text = str(value).strip()
    if text == "":
        return "empty-string"
    try:
        number = float(text)
    except ValueError:
        return f"text[{len(text)}]"
    if not math.isfinite(number):
        return "non-finite"
    digits = len(str(abs(int(number))))
    sign = "positive" if number > 0 else ("zero" if number == 0 else "negative")
    return f"numeric:{sign}:{digits}-digit"


def classify(field: str) -> str:
    lowered = field.lower()
    if any(hint in lowered for hint in CASH_HINTS):
        return "CASH?"
    if any(hint in lowered for hint in EQUITY_HINTS):
        return "EQUITY?"
    return ""


def describe_block(label, block, report):
    """Print every field name and shape in one output block."""
    if block is None:
        print(f"    {label}: absent")
        return
    rows = block if isinstance(block, list) else [block]
    if not rows:
        print(f"    {label}: empty")
        return
    print(f"    {label}: {len(rows)} row(s); fields of row 0:")
    first = rows[0]
    if not isinstance(first, dict):
        print(f"      (not an object: {type(first).__name__})")
        return
    for field in sorted(first):
        tag = classify(field)
        marker = f" <-- {tag}" if tag else ""
        rendered = shape(first[field])
        print(f"      {field:28} {rendered}{marker}")
        if tag and rendered.startswith("numeric"):
            report.setdefault(tag, []).append(f"{label}.{field} = {rendered}")


def probe(broker, spec, report) -> None:
    env_key = broker._env_key()
    tr_id = spec["tr_id"].get(env_key) or spec["tr_id"].get("live")
    params = dict(spec["params"])
    params.update({"CANO": broker.config.account_no,
                   "ACNT_PRDT_CD": broker.config.account_product_cd,
                   "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""})
    print(f"\n  [{spec['name']}] {spec['describe']}")
    print(f"    path={spec['path']}  tr_id={tr_id}")
    try:
        body = broker._get(spec["path"], tr_id, params)
    except Exception as exc:  # noqa: BLE001 - a probe reports, it does not crash
        print(f"    REQUEST FAILED: {type(exc).__name__}")
        report.setdefault("failed", []).append(spec["name"])
        return
    if not isinstance(body, dict):
        print(f"    unexpected body type {type(body).__name__}")
        return
    print(f"    rt_cd={body.get('rt_cd')!r} msg_cd={body.get('msg_cd')!r}")
    if str(body.get("rt_cd")) != "0":
        # msg1 is an API message, not account data, but truncate anyway.
        print(f"    msg1={str(body.get('msg1', ''))[:80]!r}")
        report.setdefault("refused", []).append(spec["name"])
        return
    for key in ("output", "output1", "output2", "output3"):
        if key in body:
            describe_block(key, body.get(key), report)


def probe_orderable(broker, symbols, report) -> None:
    """PHASE 4B's open questions about `get_orderable_usd`.

    Reports RELATIVE differences only -- same/different and a percentage
    -- never the account's actual orderable amount.
    """
    from domain.instrument import build_instrument

    print("\n  [orderable_cash] symbol and price dependence")
    observations = {}
    for symbol in symbols:
        try:
            instrument = build_instrument(symbol, exchange="NASD",
                                          fractionable=False, leveraged=False,
                                          inverse=False, otc=False)
        except Exception as exc:  # noqa: BLE001
            print(f"    {symbol}: instrument build failed ({type(exc).__name__})")
            continue
        try:
            price = broker.get_current_price(instrument)
        except Exception as exc:  # noqa: BLE001
            print(f"    {symbol}: price read failed ({type(exc).__name__})")
            continue
        for label, probe_price in (("current", price),
                                   ("buffered+1%", round(price * 1.01, 2)),
                                   ("half", round(price * 0.5, 2))):
            try:
                amount = broker.get_orderable_usd(instrument, probe_price)
            except Exception as exc:  # noqa: BLE001
                print(f"    {symbol} @{label}: FAILED ({type(exc).__name__})")
                continue
            observations[(symbol, label)] = float(amount)
            print(f"    {symbol} @{label}: orderable {shape(amount)}")

    values = list(observations.values())
    if len(values) >= 2:
        low, high = min(values), max(values)
        spread = (high - low) / high * 100.0 if high else 0.0
        print(f"    spread across {len(values)} reads: {spread:.4f}%")
        report["orderable_spread_pct"] = round(spread, 6)
        report["orderable_identical"] = bool(spread == 0.0)
        by_symbol = {}
        for (symbol, label), value in observations.items():
            by_symbol.setdefault(symbol, set()).add(round(value, 6))
        report["orderable_varies_within_symbol"] = {
            symbol: len(seen) > 1 for symbol, seen in by_symbol.items()}
        distinct_symbols = {round(v, 6) for (s, l), v in observations.items() if l == "current"}
        report["orderable_varies_across_symbols"] = len(distinct_symbols) > 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", default="AAPL,MSFT",
                        help="symbols for the orderable-cash probe")
    parser.add_argument("--skip-orderable", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    from brokers.kis_broker import KISBroker
    from execution.secret_redaction import install_logging_redaction

    install_logging_redaction()

    try:
        raw = KISBroker()
        raw.config.validate_read_allowed()
    except Exception as exc:  # noqa: BLE001
        print(f"error: KIS read access is not usable: {type(exc).__name__}: "
              f"{str(exc)[:120]}")
        return EXIT_ERROR

    broker = ReadOnlyBroker(raw)
    account = str(raw.config.account_no or "")
    print("KIS ACCOUNT CASH / EQUITY PROBE  (read-only)")
    print(f"  env={raw._env_key()}  account=****{account[-4:] if len(account) >= 4 else '????'}")
    print(f"  live orders enabled = {os.environ.get('KIS_LIVE_ORDER_ENABLED', 'unset')}")

    report = {}
    for spec in PROBES:
        probe(broker, spec, report)

    if not args.skip_orderable:
        probe_orderable(broker, [s.strip().upper() for s in args.symbols.split(",") if s.strip()],
                        report)

    print("\n  --- summary ---")
    cash = report.get("CASH?") or []
    equity = report.get("EQUITY?") or []
    for label, rows in (("cash-like numeric fields", cash),
                        ("equity-like numeric fields", equity)):
        print(f"    {label}: {len(rows)}")
        for row in rows:
            print(f"      {row}")
    print(f"    endpoints that failed: {report.get('failed') or 'none'}")
    print(f"    endpoints that refused (rt_cd != 0): {report.get('refused') or 'none'}")
    print(f"\n  broker methods called: {sorted(set(broker.calls))}")
    print("  orders submitted: 0")

    if args.json:
        print("\nJSON " + json.dumps(report, sort_keys=True, default=str))

    return EXIT_OK if (cash or equity) else EXIT_NOT_FOUND


if __name__ == "__main__":
    sys.exit(main())
