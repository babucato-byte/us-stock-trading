"""ORACLE-CASH-01, at the pipeline level: what the entry evaluation does
with each orderable-cash outcome.

tests/test_orderable_cash_sizing.py pins the broker read and the sizing
arithmetic. This file drives the real `_evaluate_symbol()` and checks the
three things an operator reads afterwards:

  - the reason code recorded (an unusable read must NOT be filed as
    INSUFFICIENT_CASH -- an API fault and an underfunded account are
    different days),
  - how far the evaluation got (the funded case must reach the Order
    Gate, which is the whole point of fixing the sizing),
  - that nothing was submitted, in every case.

The numbers are the ones observed on the Oracle live account on
2026-08-06: orderable $30.99, candidate IOVA at $5.82, rollout max
quantity 1.
"""
import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import shadow_audit
from brokers.kis_broker import KISOrderableCashUnavailableError
from domain.cash_sizing import INSUFFICIENT_CASH, ORDERABLE_CASH_UNAVAILABLE
from execution import idempotency
from state_store import db as state_db

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

NOW = datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc)

LIVE_ORDERABLE_USD = 30.99
CANDIDATE_PRICE_USD = 5.82

UNIVERSE = "symbol,exchange\nAAPL,NASDAQ\nIOVA,NASDAQ\n"


def _shadow_module():
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        return importlib.import_module("run_shadow_mode")
    finally:
        sys.path.remove(str(SCRIPTS_DIR))


class _Broker:
    """Read-only by construction. `orderable` is either a number the
    psamount read returns, or an exception it raises."""

    def __init__(self, *, orderable=LIVE_ORDERABLE_USD, price=CANDIDATE_PRICE_USD):
        self.calls = []
        self._orderable = orderable
        self._price = price

    def get_account_snapshot(self):
        self.calls.append("get_account_snapshot")

        class Snapshot:
            account_id = "44xxxxxx"
            usd_cash = None
            usd_orderable_cash = None
            usd_available_for_new_order = None
            cash_status = "UNAVAILABLE"
            cash_source = "TTTS3012R_DOES_NOT_PROVIDE"
        return Snapshot()

    def get_orderable_usd(self, instrument, limit_price_usd):
        self.calls.append(("get_orderable_usd", instrument.symbol, limit_price_usd))
        if isinstance(self._orderable, Exception):
            raise self._orderable
        return self._orderable

    def get_open_orders(self):
        self.calls.append("get_open_orders")
        return []

    def get_current_price(self, instrument):
        self.calls.append("get_current_price")
        return self._price

    def get_positions(self):
        self.calls.append("get_positions")
        return []

    def get_fills(self, **kwargs):
        self.calls.append("get_fills")
        return []

    def submit_order(self, *args, **kwargs):       # pragma: no cover
        raise AssertionError("the entry evaluation reached an order transport")

    def cancel_order(self, *args, **kwargs):       # pragma: no cover
        raise AssertionError("the entry evaluation reached a cancel transport")


class _Rollout:
    allowed_symbols = frozenset({"IOVA"})
    max_quantity_per_order = 1
    max_price_deviation_percent = 30.0
    regular_session_only = False
    max_open_positions = 99
    max_daily_entries = 99


@pytest.fixture
def shadow_env(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setenv("SHADOW_MODE_LOG_FILE", str(tmp_path / "SHADOW.jsonl"))
    monkeypatch.setenv("RECONCILIATION_STATE_FILE", str(tmp_path / "RECONCILIATION.json"))
    # The safety posture this whole exercise runs under, unchanged.
    monkeypatch.setenv("KIS_LIVE_ORDER_ENABLED", "false")
    monkeypatch.setenv("ENTRY_DISABLED", "true")
    monkeypatch.delenv("SHADOW_ALLOWED_SYMBOLS", raising=False)
    universe = tmp_path / "universe.csv"
    universe.write_text(UNIVERSE, encoding="utf-8")
    monkeypatch.setenv("UNIVERSE_FILE", str(universe))
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "IDEMPOTENCY.lock")
    from market_data import exchange_registry

    exchange_registry.reset_registry()
    state_db.open_db().close()
    yield tmp_path
    exchange_registry.reset_registry()


def _rows(table):
    conn = state_db.open_db()
    try:
        return conn.execute(f"select count(*) from {table}").fetchone()[0]
    finally:
        conn.close()


def _events(symbol=None):
    conn = state_db.open_db()
    try:
        rows = conn.execute(
            "select symbol, event_type, result, reason_code, payload "
            "from shadow_audit_events order by rowid").fetchall()
    finally:
        conn.close()
    return [dict(symbol=r[0], event_type=r[1], result=r[2], reason_code=r[3],
                 payload=json.loads(r[4]) if r[4] else None)
            for r in rows if symbol is None or r[0] == symbol]


def _evaluate(module, broker, *, symbol="IOVA", price=CANDIDATE_PRICE_USD, monkeypatch):
    monkeypatch.setattr(module.pso, "analyze_stock",
                        lambda s: {"score": 999, "price": price})

    class _Quote:
        price_usd = price

    conn = state_db.open_db()
    try:
        return module._evaluate_symbol(
            symbol=symbol, broker=broker, rollout=_Rollout(), conn=conn,
            kis_validation=type("V", (), {"get_price_quote": staticmethod(
                lambda s: _Quote())})(),
            deployed_commit="abc", validated_commit="abc",
            allowed_account_no="44xxxxxx", is_regular_session=True, now=NOW,
        )
    finally:
        conn.close()


def _order_row_counts():
    """Every table a real order would touch."""
    return {t: _rows(t) for t in (
        "orders", "fills", "positions", "kis_order_idempotency",
        "live_entry_reservations", "exit_intents", "order_state_events",
    )}


def _assert_no_transport(broker, before):
    assert _order_row_counts() == before, "an order-related row was written"
    assert not [c for c in broker.calls
                if isinstance(c, str) and c in ("submit_order", "cancel_order")]


def _terminal_events(symbol="IOVA"):
    return [e for e in _events(symbol)
            if e["event_type"] in shadow_audit.TERMINAL_EVENT_TYPES]


# ---------------------------------------------------------------------
# A. The funded control: $30.99 at $5.82 gets past cash and reaches the gate.
# ---------------------------------------------------------------------
class TestFundedCandidateReachesTheGate:
    def test_it_gets_past_the_cash_stage(self, shadow_env, monkeypatch):
        module = _shadow_module()
        broker = _Broker(orderable=LIVE_ORDERABLE_USD)
        before = _order_row_counts()
        outcome = _evaluate(module, broker, monkeypatch=monkeypatch)
        # The gate ran: `hypothetical` carries a verdict, not a
        # "NOT_EVALUATED:CASH" stub.
        assert outcome["hypothetical"] is not None
        assert not str(outcome["hypothetical"]).startswith("NOT_EVALUATED")
        assert outcome["reason_code"] != INSUFFICIENT_CASH
        assert outcome["reason_code"] != ORDERABLE_CASH_UNAVAILABLE
        _assert_no_transport(broker, before)

    def test_the_gate_was_actually_evaluated(self, shadow_env, monkeypatch):
        module = _shadow_module()
        _evaluate(module, _Broker(), monkeypatch=monkeypatch)
        types = [e["event_type"] for e in _events("IOVA")]
        assert shadow_audit.HYPOTHETICAL_INCOMPLETE not in types, (
            "the evaluation stopped before the gate")
        assert any(t in types for t in (shadow_audit.GATE_APPROVED,
                                        shadow_audit.GATE_REJECTED))

    def test_the_rollout_cap_still_wins(self, shadow_env, monkeypatch):
        """$30.99 pays for five shares at $5.82; the rollout allows one.
        The final quantity is the cap, not the affordability."""
        module = _shadow_module()
        from domain.cash_sizing import whole_shares_affordable

        assert whole_shares_affordable(LIVE_ORDERABLE_USD, CANDIDATE_PRICE_USD) == 5
        _evaluate(module, _Broker(), monkeypatch=monkeypatch)
        planned = [e for e in _events("IOVA")
                   if e["event_type"] == shadow_audit.EXECUTION_PLANNED]
        assert planned, "the hypothetical gate did not reach the planning step"
        assert "quantity=1" in (planned[0]["payload"] or {}).get("detail", "")

    def test_the_orderable_read_used_the_intent_limit_price(self, shadow_env, monkeypatch):
        """§3: one price for the orderable-amount question and for the
        order it justifies. A different price is a different answer."""
        module = _shadow_module()
        broker = _Broker()
        _evaluate(module, broker, monkeypatch=monkeypatch)
        reads = [c for c in broker.calls
                 if isinstance(c, tuple) and c[0] == "get_orderable_usd"]
        assert len(reads) == 1, "the orderable amount was read more than once"
        assert reads[0][1] == "IOVA"
        assert reads[0][2] == CANDIDATE_PRICE_USD

    def test_exactly_one_terminal_event(self, shadow_env, monkeypatch):
        module = _shadow_module()
        _evaluate(module, _Broker(), monkeypatch=monkeypatch)
        assert len(_terminal_events()) == 1


# ---------------------------------------------------------------------
# B. A real zero balance is INSUFFICIENT_CASH -- and only that case is.
# ---------------------------------------------------------------------
class TestRealZeroBalance:
    def test_zero_is_insufficient_cash(self, shadow_env, monkeypatch):
        module = _shadow_module()
        broker = _Broker(orderable=0.0)
        before = _order_row_counts()
        outcome = _evaluate(module, broker, monkeypatch=monkeypatch)
        assert outcome["reason_code"] == INSUFFICIENT_CASH
        assert outcome["hypothetical"] == "NOT_EVALUATED:CASH"
        _assert_no_transport(broker, before)

    def test_it_is_audited_as_a_cash_block(self, shadow_env, monkeypatch):
        module = _shadow_module()
        _evaluate(module, _Broker(orderable=0.0), monkeypatch=monkeypatch)
        cash = [e for e in _events("IOVA")
                if e["event_type"] == shadow_audit.CASH_BLOCKED]
        assert [e["reason_code"] for e in cash] == [INSUFFICIENT_CASH]

    def test_not_quite_one_share_is_also_insufficient_cash(self, shadow_env, monkeypatch):
        """A successfully-read balance that cannot pay for one whole share
        is a balance verdict, not an unavailability."""
        module = _shadow_module()
        outcome = _evaluate(module, _Broker(orderable=5.819999), monkeypatch=monkeypatch)
        assert outcome["reason_code"] == INSUFFICIENT_CASH

    def test_exactly_one_terminal_event(self, shadow_env, monkeypatch):
        module = _shadow_module()
        _evaluate(module, _Broker(orderable=0.0), monkeypatch=monkeypatch)
        assert len(_terminal_events()) == 1


# ---------------------------------------------------------------------
# C/D. Every unusable read is ORDERABLE_CASH_UNAVAILABLE, never
#      INSUFFICIENT_CASH, and never sizes an order.
# ---------------------------------------------------------------------
UNAVAILABLE_CASES = [
    ("field missing", KISOrderableCashUnavailableError(
        "unusable (field_missing)", symbol="IOVA", detail="field_missing")),
    ("None", KISOrderableCashUnavailableError(
        "unusable (field_missing)", symbol="IOVA", detail="field_missing")),
    ("empty string", KISOrderableCashUnavailableError(
        "unusable (field_empty)", symbol="IOVA", detail="field_empty")),
    ("NaN", KISOrderableCashUnavailableError(
        "unusable (field_not_finite)", symbol="IOVA", detail="field_not_finite")),
    ("Infinity", KISOrderableCashUnavailableError(
        "unusable (field_not_finite)", symbol="IOVA", detail="field_not_finite")),
    ("negative", KISOrderableCashUnavailableError(
        "unusable (field_negative)", symbol="IOVA", detail="field_negative")),
    ("list", KISOrderableCashUnavailableError(
        "unusable (field_type_list)", symbol="IOVA", detail="field_type_list")),
    ("dict", KISOrderableCashUnavailableError(
        "unusable (field_type_dict)", symbol="IOVA", detail="field_type_dict")),
    ("network error", KISOrderableCashUnavailableError(
        "read failed", symbol="IOVA", detail="read_failed")),
    ("KIS non-success", KISOrderableCashUnavailableError(
        "read failed", symbol="IOVA", detail="read_failed")),
]


class TestUnusableReadsFailClosed:
    @pytest.mark.parametrize("label,error", UNAVAILABLE_CASES,
                             ids=[c[0] for c in UNAVAILABLE_CASES])
    def test_the_reason_is_unavailable_not_insufficient(self, label, error,
                                                        shadow_env, monkeypatch):
        module = _shadow_module()
        broker = _Broker(orderable=error)
        before = _order_row_counts()
        outcome = _evaluate(module, broker, monkeypatch=monkeypatch)
        assert outcome["reason_code"] == ORDERABLE_CASH_UNAVAILABLE, label
        assert outcome["reason_code"] != INSUFFICIENT_CASH, label
        _assert_no_transport(broker, before)

    @pytest.mark.parametrize("label,error", UNAVAILABLE_CASES,
                             ids=[c[0] for c in UNAVAILABLE_CASES])
    def test_no_gate_verdict_is_claimed(self, label, error, shadow_env, monkeypatch):
        module = _shadow_module()
        outcome = _evaluate(module, _Broker(orderable=error), monkeypatch=monkeypatch)
        assert outcome["hypothetical"] == "NOT_EVALUATED:CASH", label

    @pytest.mark.parametrize("label,error", UNAVAILABLE_CASES,
                             ids=[c[0] for c in UNAVAILABLE_CASES])
    def test_exactly_one_terminal_event(self, label, error, shadow_env, monkeypatch):
        module = _shadow_module()
        _evaluate(module, _Broker(orderable=error), monkeypatch=monkeypatch)
        assert len(_terminal_events()) == 1, label

    def test_the_audit_row_says_unavailable(self, shadow_env, monkeypatch):
        module = _shadow_module()
        _evaluate(module, _Broker(orderable=UNAVAILABLE_CASES[0][1]),
                  monkeypatch=monkeypatch)
        cash = [e for e in _events("IOVA")
                if e["event_type"] == shadow_audit.CASH_BLOCKED]
        assert [e["reason_code"] for e in cash] == [ORDERABLE_CASH_UNAVAILABLE]

    def test_the_two_outcomes_are_distinguishable_in_the_audit_trail(
            self, shadow_env, monkeypatch):
        """The property the whole distinction exists for: an outage and an
        empty account do not produce the same record."""
        module = _shadow_module()
        _evaluate(module, _Broker(orderable=0.0), symbol="IOVA", monkeypatch=monkeypatch)
        _evaluate(module, _Broker(orderable=UNAVAILABLE_CASES[8][1]), symbol="AAPL",
                  monkeypatch=monkeypatch)
        reasons = {e["symbol"]: e["reason_code"] for e in _events()
                   if e["event_type"] == shadow_audit.CASH_BLOCKED}
        assert reasons["IOVA"] == INSUFFICIENT_CASH
        assert reasons["AAPL"] == ORDERABLE_CASH_UNAVAILABLE


# ---------------------------------------------------------------------
# The defect itself: an unavailable BALANCE must not read as $0 anywhere.
# ---------------------------------------------------------------------
class TestEntryLimitObservability:
    """The two rollout caps are visible in the audit trail (ORACLE-LIMIT-01
    §19). A cap block that recorded only its reason code would leave an
    operator unable to tell 1-of-1 from 1-of-5 without querying the DB."""

    def test_the_gate_events_carry_the_capacity_numbers(self, shadow_env, monkeypatch):
        module = _shadow_module()
        _evaluate(module, _Broker(), monkeypatch=monkeypatch)
        gate_events = [e for e in _events("IOVA")
                       if e["event_type"] in (shadow_audit.GATE_APPROVED,
                                              shadow_audit.GATE_REJECTED)]
        assert gate_events, "no gate verdict was recorded"
        payload = gate_events[0]["payload"] or {}
        for key in ("max_open_positions", "open_positions", "pending_entries",
                    "effective_positions", "max_daily_entries", "daily_entries",
                    "trading_day"):
            assert key in payload, f"{key} missing from the gate audit payload"

    def test_the_payload_carries_no_identifier(self, shadow_env, monkeypatch):
        module = _shadow_module()
        _evaluate(module, _Broker(), monkeypatch=monkeypatch)
        gate_events = [e for e in _events("IOVA")
                       if e["event_type"] in (shadow_audit.GATE_APPROVED,
                                              shadow_audit.GATE_REJECTED)]
        text = repr(gate_events[0]["payload"])
        assert "44xxxxxx" not in text


class TestTheAccountSnapshotIsNoLongerACashSource:
    def test_the_pipeline_does_not_size_from_the_snapshot(self, shadow_env, monkeypatch):
        """The regression guard. This broker's snapshot reports cash as
        UNAVAILABLE (as a real one does) while the orderable read says
        $30.99. Sizing from the snapshot would block at cash; sizing from
        the orderable read reaches the gate."""
        module = _shadow_module()
        outcome = _evaluate(module, _Broker(orderable=LIVE_ORDERABLE_USD),
                            monkeypatch=monkeypatch)
        assert outcome["hypothetical"] != "NOT_EVALUATED:CASH", (
            "the evaluation still sizes from the account snapshot")

    def test_the_source_file_no_longer_reads_cash_from_the_snapshot(self):
        source = (SCRIPTS_DIR / "run_shadow_mode.py").read_text(encoding="utf-8")
        assert "usd_available_for_new_order" not in source
        assert "get_orderable_usd" in source

    def test_the_live_entry_path_was_fixed_the_same_way(self):
        source = (REPO_ROOT / "kis_live_trading.py").read_text(encoding="utf-8")
        assert "usd_available_for_new_order" not in source
        assert "get_orderable_usd" in source

    def test_no_source_file_still_reads_the_absent_balance_fields(self):
        """The field names that do not exist in the response must not come
        back as a fallback anywhere in the entry path."""
        for path in (REPO_ROOT / "brokers" / "kis_broker.py",):
            source = path.read_text(encoding="utf-8")
            assert 'get("frcr_dncl_amt1", 0)' not in source
            assert 'get("frcr_use_psbl_amt", usd_cash)' not in source
