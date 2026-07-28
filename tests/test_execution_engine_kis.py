"""Tests for execution/execution_engine.py -- the KIS-path orchestrator
(idempotency -> order_gate -> KISBroker -> state tracking). Uses a fake
KISBroker double (not the real requests-based one) since this module's
own job is orchestration, not the KIS wire protocol (already covered by
tests/test_kis_broker.py).
"""
from datetime import datetime, timezone

import pytest

from brokers.kis_broker import KISAmbiguousResponseError, KISBrokerError
from domain.execution_event import ExecutionRecord
from domain.instrument import build_instrument
from domain.order_intent import OrderIntent
from execution import execution_engine, order_gate
from execution.execution_engine import ExecutionEngineError
from state_store import db as state_db

NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    from execution import idempotency
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "KIS_ORDER_IDEMPOTENCY.lock")
    yield


def _conn():
    return state_db.open_db()


def _instrument():
    return build_instrument("AAPL", exchange="NASDAQ")


def _order_intent(**overrides):
    kwargs = dict(
        internal_order_id="ord-1", signal_id="sig-1", strategy_id="strat-1",
        symbol="AAPL", exchange="NASDAQ", side="buy", quantity=1, order_type="limit",
        limit_price=100.0, stop_price=95.0, target_price=110.0, created_at=NOW,
    )
    kwargs.update(overrides)
    return OrderIntent(**kwargs)


class _FakeBroker:
    def __init__(self, response=None, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc
        self.calls = []

    def submit_order(self, order_intent, instrument):
        self.calls.append((order_intent, instrument))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


def _accepted_record(order_intent):
    return ExecutionRecord(
        internal_order_id=order_intent.internal_order_id, broker="kis", broker_order_id="kis-1",
        requested_quantity=order_intent.quantity, requested_price=order_intent.limit_price,
        filled_quantity=0.0, average_fill_price=None, status="ACCEPTED",
        submitted_at=NOW, updated_at=NOW,
    )


def _passing_buy_ctx_builder(order_intent):
    def _build():
        return order_gate.BuyGateContext(
            execution_broker="kis", live_order_enabled=True, entry_disabled=False,
            validated_commit="c1", deployed_commit="c1", kis_account_no="123",
            allowed_account_no="123", order_intent=order_intent, instrument=_instrument(),
            signal=_make_signal(), is_regular_session=True, kis_price_usd=100.1,
            max_price_deviation_percent=0.30, usd_orderable_cash=1000.0,
            has_open_order_for_symbol=False, has_order_for_signal_id=False,
            allowed_symbols=frozenset({"AAPL"}), reconciliation_ok=True, has_unknown_order=False,
            now=NOW,
        )
    return _build


def _make_signal():
    from domain.signal import build_signal
    return build_signal(
        strategy_id="strat-1", strategy_version="v1", config_version="cfg-1",
        code_commit="abc", symbol="AAPL", exchange="NASDAQ", signal_price=100.0,
        score=90.0, entry_reason="breakout", valid_for_seconds=300, now=NOW,
    )


def _passing_sell_ctx_builder(order_intent):
    def _build():
        return order_gate.SellGateContext(
            execution_broker="kis", live_order_enabled=True, order_intent=order_intent,
            instrument=_instrument(), kis_position_quantity=5, position_source="kis",
            has_existing_sell_order_for_symbol=False, reconciliation_ok=True, has_unknown_order=False,
        )
    return _build


class TestSubmitBuyOrder:
    def test_success(self):
        conn = _conn()
        oi = _order_intent()
        broker = _FakeBroker(response=_accepted_record(oi))
        result = execution_engine.submit_buy_order(
            order_intent=oi, buy_gate_context_builder=_passing_buy_ctx_builder(oi),
            conn=conn, broker=broker, instrument=_instrument(), now=NOW,
        )
        assert result.status == "ACCEPTED"
        assert len(broker.calls) == 1

    def test_duplicate_blocked_before_broker_call(self):
        conn = _conn()
        oi = _order_intent()
        broker = _FakeBroker(response=_accepted_record(oi))
        execution_engine.submit_buy_order(
            order_intent=oi, buy_gate_context_builder=_passing_buy_ctx_builder(oi),
            conn=conn, broker=broker, instrument=_instrument(), now=NOW,
        )
        oi2 = _order_intent(internal_order_id="ord-2")  # same signal/symbol/side/date
        broker2 = _FakeBroker(response=_accepted_record(oi2))
        with pytest.raises(ExecutionEngineError, match="idempotency"):
            execution_engine.submit_buy_order(
                order_intent=oi2, buy_gate_context_builder=_passing_buy_ctx_builder(oi2),
                conn=conn, broker=broker2, instrument=_instrument(), now=NOW,
            )
        assert broker2.calls == []

    def test_gate_blocked_zero_broker_calls(self):
        conn = _conn()
        oi = _order_intent()
        broker = _FakeBroker(response=_accepted_record(oi))

        def _failing_ctx():
            ctx = _passing_buy_ctx_builder(oi)()
            return ctx.__class__(**{**ctx.__dict__, "entry_disabled": True})

        with pytest.raises(ExecutionEngineError, match="order gate"):
            execution_engine.submit_buy_order(
                order_intent=oi, buy_gate_context_builder=_failing_ctx,
                conn=conn, broker=broker, instrument=_instrument(), now=NOW,
            )
        assert broker.calls == []

    def test_ambiguous_broker_response_lands_in_unknown_and_reraises(self):
        conn = _conn()
        oi = _order_intent()
        broker = _FakeBroker(raise_exc=KISAmbiguousResponseError("timeout"))
        with pytest.raises(KISAmbiguousResponseError):
            execution_engine.submit_buy_order(
                order_intent=oi, buy_gate_context_builder=_passing_buy_ctx_builder(oi),
                conn=conn, broker=broker, instrument=_instrument(), now=NOW,
            )
        from execution import idempotency
        row = idempotency.find_existing(
            conn, internal_order_id=oi.internal_order_id, signal_id=oi.signal_id,
            symbol=oi.symbol, side=oi.side, trading_date=NOW.date().isoformat(),
        )
        assert row["status"] == "UNKNOWN"

    def test_definite_broker_error_marks_rejected(self):
        conn = _conn()
        oi = _order_intent()
        broker = _FakeBroker(raise_exc=KISBrokerError("bad request"))
        with pytest.raises(KISBrokerError):
            execution_engine.submit_buy_order(
                order_intent=oi, buy_gate_context_builder=_passing_buy_ctx_builder(oi),
                conn=conn, broker=broker, instrument=_instrument(), now=NOW,
            )
        from execution import idempotency
        row = idempotency.find_existing(
            conn, internal_order_id=oi.internal_order_id, signal_id=oi.signal_id,
            symbol=oi.symbol, side=oi.side, trading_date=NOW.date().isoformat(),
        )
        assert row["status"] == "REJECTED"


class TestSubmitSellOrder:
    def test_success(self):
        conn = _conn()
        oi = _order_intent(side="sell")
        broker = _FakeBroker(response=_accepted_record(oi))
        result = execution_engine.submit_sell_order(
            order_intent=oi, sell_gate_context_builder=_passing_sell_ctx_builder(oi),
            conn=conn, broker=broker, instrument=_instrument(), now=NOW,
        )
        assert result.status == "ACCEPTED"
        assert len(broker.calls) == 1

    def test_gate_blocked_zero_broker_calls(self):
        conn = _conn()
        oi = _order_intent(side="sell", quantity=99)
        broker = _FakeBroker(response=_accepted_record(oi))
        with pytest.raises(ExecutionEngineError, match="order gate"):
            execution_engine.submit_sell_order(
                order_intent=oi, sell_gate_context_builder=_passing_sell_ctx_builder(oi),
                conn=conn, broker=broker, instrument=_instrument(), now=NOW,
            )
        assert broker.calls == []
