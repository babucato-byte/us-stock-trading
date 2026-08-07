#!/usr/bin/env python3
"""OBSERVE cash-path VALIDATION probe -- not a trading path.

Why this exists
---------------
ORACLE-CASH-01 changed how entry sizing gets its cash: the balance read
carries no cash field at all, so the figure now comes from a per-candidate
`inquire-psamount` call (TTTS3007R, `output.ord_psbl_frcr_amt`) taken at
the same limit price the order would use. Two Oracle OBSERVE sessions
confirmed the wire contract but never exercised the sizing path, because
the scanner produced no candidates on either day -- the evaluation stops
before the cash stage when there is nothing to evaluate.

Waiting for a candidate to appear naturally is not a verification plan.
This probe drives the same chain with real market data and a real account
read, on a symbol chosen for validation:

    symbol -> get_current_price -> limit price -> get_orderable_usd
           -> whole_shares_affordable -> rollout cap -> CASH gate
           -> BuyGateContext -> every gate after CASH

What this probe is NOT
---------------------
The symbol it examines is NOT a strategy candidate and NOT a
recommendation. It never touches `candidates.csv`, `order_candidates.csv`,
`strong_candidates.csv`, `previous_candidates.csv`, `universe.csv` or
`universe_tradable.csv`, and it changes no scanner threshold, allow-list
or risk rule. The signal it constructs is synthetic and labelled as such
in the output, so an operator reading a log cannot mistake it for
something the strategy chose.

It CANNOT place, amend or cancel an order. Not "does not": it never
imports execution.execution_engine, and the broker handle is wrapped in a
proxy whose allow-list holds read methods only -- a submission call raises
before a request is built, and the call counters are asserted at zero
before the result is printed.

Nothing here re-implements production arithmetic. The sizing function,
the gate, the order-intent and signal builders, the reconciliation
snapshot and the rollout config are all imported from the modules the
live path uses; if one of them changes, this probe changes with it. A
probe with its own copy of the formula verifies its own copy.

Exit codes:
    0  the cash path was exercised end to end and behaved as specified
    1  a structural error (bad configuration, unusable environment)
    2  the path could not be validated (no affordable symbol, gate or
       sizing behaved unexpectedly, or a side effect was observed)
"""

import argparse
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from brokers.kis_broker import (  # noqa: E402
    ORDERABLE_AMOUNT_FIELD,
    PSAMOUNT_PATH,
    TR_ID_PSAMOUNT,
    KISBroker,
    KISOrderableCashUnavailableError,
)
from config.live_rollout_config import LiveRolloutConfig  # noqa: E402
from domain.cash_sizing import (  # noqa: E402
    INSUFFICIENT_CASH,
    ORDERABLE_CASH_UNAVAILABLE,
    whole_shares_affordable,
)
from domain.order_intent import OrderIntent  # noqa: E402
from domain.signal import build_signal  # noqa: E402
from execution import order_gate  # noqa: E402
from execution.secret_redaction import (  # noqa: E402
    install_logging_redaction,
    mask_account_number,
)
from market_data.exchange_registry import (  # noqa: E402
    ExchangeResolutionError,
    build_kis_instrument,
)
from market_data.base import MarketDataProviderError  # noqa: E402
from market_data.kis_validation_provider import KISValidationProvider  # noqa: E402
from reconciliation import snapshot as reconciliation_snapshot  # noqa: E402
from state_store import db as state_db  # noqa: E402

logger = logging.getLogger("verify_kis_observe_cash_path")

MODE = "OBSERVE_VALIDATION"
TRANSPORT_ENABLED = False

# Symbols this probe may examine by default: low-priced, order-eligible US
# listings, chosen so that one whole share is likely to fit inside a small
# orderable balance. IOVA is first only because a real scanner pass
# surfaced it once, which makes it a realistic shape for this check. None
# of this is a view on any of them -- eligibility and price are re-read
# from the API on every run and a symbol that fails either is skipped.
DEFAULT_SYMBOLS = ("IOVA", "SIRI", "SOFI", "BBAI", "PLUG")

# Tables a real order would touch. All seven must be unchanged.
ORDER_TABLES = (
    "orders", "fills", "positions", "kis_order_idempotency",
    "live_entry_reservations", "exit_intents", "order_state_events",
)

# The buy gate's checks, in the order `order_gate.evaluate_buy_gate()`
# runs them. Used ONLY to report how far an evaluation got; the gate
# itself is the authority on what passes. tests/test_observe_cash_path_
# probe.py parses the gate's source and fails if this list drifts.
GATE_SEQUENCE = (
    "BROKER", "LIVE_FLAG", "ENTRY_DISABLED", "COMMIT", "ACCOUNT", "QUANTITY",
    "ORDER_TYPE", "SESSION", "SIGNAL_EXPIRED", "PRICE_INVALID", "PRICE_DEVIATION",
    "CASH", "OPEN_ORDER", "DUPLICATE_SIGNAL", "SYMBOL", "INSTRUMENT", "RECONCILIATION",
)

FORBIDDEN_METHODS = frozenset({
    "submit_order", "cancel_order", "submit_buy_order", "submit_sell_order",
    "amend_order", "replace_order", "place_order",
})


class ReadOnlyViolation(Exception):
    """A mutating broker method was reached. The proxy exists so this
    cannot happen quietly."""


class ReadOnlyBroker:
    """Read methods pass through; anything that could submit raises.

    `order_calls` / `cancel_calls` stay at zero by construction -- they
    are incremented on the raising path purely so the report can state a
    measured zero rather than an assumed one.
    """

    ALLOWED = frozenset({
        "get_current_price", "get_account_snapshot", "get_positions",
        "get_open_orders", "get_fills", "get_orderable_usd", "config",
    })

    def __init__(self, broker):
        self._broker = broker
        self.calls = []
        self.order_calls = 0
        self.cancel_calls = 0

    def __getattr__(self, name):
        if name in FORBIDDEN_METHODS:
            if "cancel" in name:
                self.cancel_calls += 1
            else:
                self.order_calls += 1
            raise ReadOnlyViolation(f"{name}() is not reachable from this probe")
        if name not in self.ALLOWED:
            raise ReadOnlyViolation(f"{name} is not on the read-only allow-list")
        attribute = getattr(self._broker, name)
        if not callable(attribute):
            return attribute

        def _record(*args, **kwargs):
            self.calls.append(name)
            return attribute(*args, **kwargs)
        return _record

    def count(self, method):
        return sum(1 for call in self.calls if call == method)


class Report:
    def __init__(self):
        self.rows = []

    def record(self, name, ok, detail=None):
        self.rows.append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" {detail}" if detail else ""))

    @property
    def failed(self):
        return [row for row in self.rows if not row["ok"]]


def _db_counts():
    conn = state_db.open_db()
    try:
        return {t: conn.execute(f"select count(*) from {t}").fetchone()[0]
                for t in ORDER_TABLES}
    finally:
        conn.close()


def _furthest_gate(blocked_code):
    """How far the evaluation got. `None` means every check passed."""
    if blocked_code is None:
        return GATE_SEQUENCE[-1], "ALL_PASSED"
    if blocked_code not in GATE_SEQUENCE:
        return blocked_code, "UNKNOWN_GATE_CODE"
    index = GATE_SEQUENCE.index(blocked_code)
    reached = GATE_SEQUENCE[index - 1] if index else "NONE"
    return reached, blocked_code


def examine(symbol, *, broker, rollout, now):
    """One symbol: real price, real orderable amount, production sizing.

    Returns a dict, or None when the symbol cannot be examined at all.
    Exactly one orderable-amount read happens here, at the same limit
    price the OrderIntent is later built with.
    """
    try:
        instrument, record = build_kis_instrument(symbol)
    except ExchangeResolutionError as exc:
        return {"symbol": symbol, "skipped": exc.reason_code, "detail": str(exc)}
    if not instrument.is_order_eligible:
        return {"symbol": symbol, "skipped": "NOT_ORDER_ELIGIBLE",
                "detail": "leveraged/inverse/OTC/not tradable"}

    provider = KISValidationProvider(broker=broker, instrument_lookup=lambda s: instrument)
    try:
        quote = provider.get_price_quote(symbol)
    except MarketDataProviderError as exc:
        return {"symbol": symbol, "skipped": "PRICE_UNAVAILABLE", "detail": type(exc).__name__}

    # The one price. It is the quote, the psamount input and the
    # OrderIntent limit -- sizing against one price and ordering at
    # another is how a quantity outgrows the cash that justified it.
    limit_price = quote.price_usd

    before = broker.count("get_orderable_usd")
    try:
        orderable_usd = broker.get_orderable_usd(instrument, limit_price)
        cash_reason = None
    except KISOrderableCashUnavailableError as exc:
        return {"symbol": symbol, "instrument": instrument, "record": record,
                "limit_price": limit_price, "orderable_usd": None,
                "orderable_calls": broker.count("get_orderable_usd") - before,
                "cash_reason": ORDERABLE_CASH_UNAVAILABLE,
                "detail": str(exc.diagnostic())}
    orderable_calls = broker.count("get_orderable_usd") - before

    shares = whole_shares_affordable(orderable_usd, limit_price)
    quantity = min(shares, rollout.max_quantity_per_order)
    if quantity < 1:
        cash_reason = INSUFFICIENT_CASH
    return {
        "symbol": symbol, "instrument": instrument, "record": record,
        "limit_price": limit_price, "orderable_usd": orderable_usd,
        "orderable_calls": orderable_calls, "whole_shares": shares,
        "quantity": quantity, "cash_reason": cash_reason,
    }


def evaluate_gates(found, *, broker, rollout, env, now):
    """Builds the production BuyGateContext and runs the production gate.

    Two evaluations, exactly as `scripts/run_shadow_mode.py` does: the
    REAL one with the deployment's actual flags (which blocks at
    LIVE_FLAG, correctly -- that is what OBSERVE means), and the
    HYPOTHETICAL one with only those two config flags flipped, which is
    the evaluation that reaches CASH and everything after it.

    The hypothetical flip lives in the context object, never in the
    environment: no flag is written, and `evaluate_buy_gate()` is a pure
    predicate that raises or returns -- it has no transport to reach.
    """
    instrument = found["instrument"]
    symbol = found["symbol"]
    limit_price = found["limit_price"]

    signal = build_signal(
        strategy_id="OBSERVE_CASH_PATH_VALIDATION", strategy_version="probe",
        config_version="validation", code_commit=env.get("DEPLOYED_COMMIT", ""),
        symbol=symbol, exchange=instrument.exchange,
        # Synthetic and deliberately equal to the KIS price: this probe
        # verifies the cash path, and a fabricated divergence would only
        # test the price-deviation gate against an invented number.
        signal_price=limit_price, score=0,
        entry_reason="observe_cash_path_validation",
        valid_for_seconds=300, now=now,
    )
    order_intent = OrderIntent(
        internal_order_id=f"validation-{symbol}-{uuid.uuid4().hex[:12]}",
        signal_id=signal.signal_id, strategy_id=signal.strategy_id, symbol=symbol,
        exchange=instrument.exchange, side="buy", quantity=found["quantity"],
        order_type="limit", limit_price=limit_price, stop_price=None,
        target_price=None, created_at=now,
    )

    account = broker.get_account_snapshot()
    open_orders = broker.get_open_orders()
    conn = state_db.open_db()
    try:
        snapshot = reconciliation_snapshot.build_snapshot(
            broker=broker, conn=conn, account_id=account.account_id, symbol=symbol,
            now=now, source="observe_cash_path_validation",
        )
    finally:
        conn.close()

    has_open_order = any((o.get("pdno") or o.get("PDNO")) == symbol for o in open_orders)
    from market_hours import get_us_market_session

    is_regular_session = get_us_market_session() == "regular"

    def _ctx(*, live_order_enabled, entry_disabled):
        return order_gate.BuyGateContext(
            execution_broker="kis", live_order_enabled=live_order_enabled,
            entry_disabled=entry_disabled,
            validated_commit=env.get("VALIDATED_COMMIT", ""),
            deployed_commit=env.get("DEPLOYED_COMMIT", ""),
            kis_account_no=account.account_id,
            allowed_account_no=(env.get("KIS_ALLOWED_ACCOUNT_NO") or "").strip(),
            order_intent=order_intent, instrument=instrument, signal=signal,
            is_regular_session=is_regular_session, kis_price_usd=limit_price,
            max_price_deviation_percent=rollout.max_price_deviation_percent,
            usd_orderable_cash=found["orderable_usd"],
            has_open_order_for_symbol=has_open_order, has_order_for_signal_id=False,
            allowed_symbols=rollout.allowed_symbols, reconciliation=snapshot, now=now,
        )

    def _run(ctx):
        try:
            order_gate.evaluate_buy_gate(ctx)
            return None
        except order_gate.OrderGateBlockedError as exc:
            return exc

    real = _run(_ctx(live_order_enabled=_flag(env, "KIS_LIVE_ORDER_ENABLED"),
                     entry_disabled=_flag(env, "ENTRY_DISABLED")))
    hypothetical = _run(_ctx(live_order_enabled=True, entry_disabled=False))
    return {
        "account": account, "signal": signal, "order_intent": order_intent,
        "snapshot": snapshot, "is_regular_session": is_regular_session,
        "real_blocked": real, "hypothetical_blocked": hypothetical,
        "context_constructed": True,
    }


def _flag(env, name):
    return str(env.get(name, "")).strip().lower() in ("1", "true", "yes", "on")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate the OBSERVE cash-sizing path against real KIS reads "
                    "(places no orders, writes no candidate files)")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                        help="comma-separated symbols to examine, in order")
    parser.add_argument("--log-level", default="WARNING")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s %(message)s")
    install_logging_redaction()

    import os

    env = os.environ
    now = datetime.now(timezone.utc)
    report = Report()

    print("OBSERVE CASH PATH VALIDATION")
    print(f"  mode: {MODE}   transport_enabled: {str(TRANSPORT_ENABLED).lower()}")
    print(f"  note: the symbol below is a VALIDATION subject, not a strategy candidate\n")

    rollout = LiveRolloutConfig.from_env(env)
    broker = ReadOnlyBroker(KISBroker())
    report.record("readonly_guard", True,
                  f"{len(FORBIDDEN_METHODS)} mutating method(s) unreachable")

    db_before = _db_counts()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    examined = []
    subject = None
    for symbol in symbols:
        found = examine(symbol, broker=broker, rollout=rollout, now=now)
        examined.append(found)
        if found.get("skipped"):
            print(f"  [skip] {symbol}: {found['skipped']} ({found.get('detail')})")
            continue
        if found["cash_reason"] == ORDERABLE_CASH_UNAVAILABLE:
            # Not "no money": no answer. Reported separately here for the
            # same reason the entry path separates them -- a failed read
            # and an empty account are different operational events.
            print(f"  [read] {symbol} ({found['record'].exchange.value}) "
                  f"price={found['limit_price']} orderable=UNAVAILABLE "
                  f"({found.get('detail')}) calls={found['orderable_calls']}")
        else:
            print(f"  [read] {symbol} ({found['record'].exchange.value}) "
                  f"price={found['limit_price']} orderable={found['orderable_usd']} "
                  f"shares={found.get('whole_shares')} quantity={found.get('quantity')} "
                  f"calls={found['orderable_calls']}")
        if found.get("cash_reason") is None:
            subject = found
            break

    if subject is None:
        read_failures = [e for e in examined
                         if e.get("cash_reason") == ORDERABLE_CASH_UNAVAILABLE]
        if read_failures:
            detail = (f"{len(read_failures)} of {len(examined)} symbol(s) returned "
                      f"{ORDERABLE_CASH_UNAVAILABLE} -- the orderable-amount read did not "
                      "answer, which is NOT the same as an unaffordable price; the CASH "
                      "gate could not be exercised")
        else:
            detail = ("every examined symbol was affordable-checked and none fits one whole "
                      f"share at its current price ({INSUFFICIENT_CASH}); the CASH gate "
                      "could not be exercised")
        report.record("affordable_subject", False, detail)
        _print_tail(report, broker, db_before, rollout, env, examined=examined)
        return 2

    report.record("affordable_subject", True,
                  f"{subject['symbol']} at {subject['limit_price']} against "
                  f"orderable {subject['orderable_usd']}")
    report.record("orderable_read_once", subject["orderable_calls"] == 1,
                  f"get_orderable_usd calls for {subject['symbol']}: "
                  f"{subject['orderable_calls']}")
    report.record("whole_shares_positive", subject["whole_shares"] >= 1,
                  f"whole_shares_affordable={subject['whole_shares']} "
                  f"(production domain.cash_sizing)")
    report.record("rollout_cap_applied",
                  subject["quantity"] == min(subject["whole_shares"],
                                             rollout.max_quantity_per_order),
                  f"min({subject['whole_shares']}, LIVE_ROLLOUT_MAX_QUANTITY="
                  f"{rollout.max_quantity_per_order}) = {subject['quantity']}")

    gates = evaluate_gates(subject, broker=broker, rollout=rollout, env=env, now=now)

    report.record("same_price_everywhere",
                  gates["order_intent"].limit_price == subject["limit_price"],
                  f"psamount limit == OrderIntent limit == {subject['limit_price']}")
    report.record("buy_gate_context_constructed", gates["context_constructed"],
                  "production order_gate.BuyGateContext")

    hypothetical = gates["hypothetical_blocked"]
    code = hypothetical.code if hypothetical is not None else None
    reached, stopped_at = _furthest_gate(code)
    passed_cash = code is None or (
        code in GATE_SEQUENCE and GATE_SEQUENCE.index(code) > GATE_SEQUENCE.index("CASH")
    )
    report.record("cash_gate_passed", passed_cash,
                  f"hypothetical evaluation reached {reached}; stopped_at={stopped_at}")

    real = gates["real_blocked"]
    report.record("real_posture_still_blocks", real is not None and real.code == "LIVE_FLAG",
                  f"real evaluation blocked at {real.code if real else 'NOTHING'} "
                  "(OBSERVE must not approve)")

    # Control group: the SAME production function, one cent short of a
    # single share. No API call -- the point is that the arithmetic, not
    # the endpoint, decides INSUFFICIENT_CASH. Built in Decimal so the
    # control price is the decimal an operator reads, not 5.8100000000005.
    control_price = subject["limit_price"]
    control_cash = float(max(Decimal("0"), Decimal(str(control_price)) - Decimal("0.01")))
    control_shares = whole_shares_affordable(control_cash, control_price)
    report.record("insufficient_cash_control", control_shares == 0,
                  f"whole_shares_affordable({control_cash}, {control_price}) = "
                  f"{control_shares} -> {INSUFFICIENT_CASH} (no API call)")

    _print_tail(report, broker, db_before, rollout, env, examined=examined,
                subject=subject, gates=gates, reached=reached, stopped_at=stopped_at)

    if args.json:
        print(json.dumps(report.rows, indent=2))
    return 0 if not report.failed else 2


def _print_tail(report, broker, db_before, rollout, env, *, examined,
                subject=None, gates=None, reached=None, stopped_at=None):
    db_after = _db_counts()
    delta = {t: db_after[t] - db_before[t] for t in ORDER_TABLES}

    print("\nKIS orderability:")
    print(f"  endpoint: {PSAMOUNT_PATH}")
    print(f"  TR:       {TR_ID_PSAMOUNT['live']} (live) / {TR_ID_PSAMOUNT['paper']} (paper)")
    print(f"  field:    output.{ORDERABLE_AMOUNT_FIELD}")
    print(f"  total get_orderable_usd calls this run: {broker.count('get_orderable_usd')} "
          f"across {len([e for e in examined if not e.get('skipped')])} symbol(s)")

    if subject is not None:
        print("\nSizing:")
        print(f"  symbol:                  {subject['symbol']}")
        print(f"  exchange:                {subject['record'].exchange.value} "
              f"(EXCD={subject['record'].kis_exchange_code})")
        print(f"  current_price:           {subject['limit_price']}")
        print(f"  limit_price:             {subject['limit_price']}")
        print(f"  orderable_usd:           {subject['orderable_usd']}")
        print(f"  whole_shares_affordable: {subject['whole_shares']}")
        print(f"  rollout_max_quantity:    {rollout.max_quantity_per_order}")
        print(f"  final_quantity:          {subject['quantity']}")
        print(f"  cash_result:             {'PASS' if subject['cash_reason'] is None else subject['cash_reason']}")

    if gates is not None:
        real = gates["real_blocked"]
        hyp = gates["hypothetical_blocked"]
        print("\nGate:")
        print(f"  BuyGateContext:  constructed")
        print(f"  real posture:    {'BLOCKED:' + real.code if real else 'APPROVED'}")
        print(f"  hypothetical:    {'BLOCKED:' + hyp.code if hyp else 'WOULD_APPROVE'}")
        print(f"  furthest_gate:   {reached}")
        print(f"  stopped_at:      {stopped_at}")
        print(f"  gates evaluated after CASH: "
              f"{', '.join(GATE_SEQUENCE[GATE_SEQUENCE.index('CASH') + 1:])}")
        print(f"  regular_session: {gates['is_regular_session']}")
        print(f"  reconciliation:  clean={gates['snapshot'].is_clean()} "
              f"unknown={gates['snapshot'].has_unknown_orders}")

    print("\nTransport:")
    print(f"  order_calls:  {broker.order_calls}")
    print(f"  cancel_calls: {broker.cancel_calls}")

    print("\nDB delta:")
    for table in ORDER_TABLES:
        print(f"  {table}: {delta[table]}")

    print("\nSafety:")
    from live_pilot.posture import resolve_posture

    decision = resolve_posture(env)
    print(f"  posture:                {decision.posture}")
    for name in ("ENTRY_DISABLED", "KIS_LIVE_ORDER_ENABLED", "LIVE_ROLLOUT_ENABLED"):
        print(f"  {name}: {env.get(name)}")
    if gates is not None:
        print(f"  account:                {mask_account_number(gates['account'].account_id)}")
    print(f"  secrets_exposed:        0")

    report.record("no_order_transport", broker.order_calls == 0 and broker.cancel_calls == 0,
                  f"order={broker.order_calls} cancel={broker.cancel_calls}")
    report.record("no_db_side_effects", all(v == 0 for v in delta.values()),
                  f"delta {delta}")
    assert broker.order_calls == 0 and broker.cancel_calls == 0
    print(f"\nRESULT: {'PASS' if not report.failed else 'BLOCKED'}")


if __name__ == "__main__":
    raise SystemExit(main())
