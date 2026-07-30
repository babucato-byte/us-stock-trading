from datetime import datetime, timezone

import pytest

from brokers.kis_broker import KISAmbiguousResponseError, KISBrokerError
from brokers.kis_broker_adapter import KISBrokerAdapter, KISBrokerAdapterError
from domain.execution_event import ExecutionRecord
from domain.position import Position

NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    from execution import idempotency
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "KIS_ORDER_IDEMPOTENCY.lock")
    yield


class _FakeKISBroker:
    def __init__(self, price=100.0, positions=None, open_orders=None, submit_response=None,
                 submit_raise=None, fills=None):
        self.price = price
        self.positions = positions if positions is not None else [
            Position(symbol="AAPL", quantity=5, average_fill_price=95.0, unrealized_pnl=0.0,
                      realized_pnl=0.0, as_of=NOW, source="kis_balance")
        ]
        self.open_orders = open_orders or []
        self.submit_response = submit_response
        self.submit_raise = submit_raise
        self.fills = fills or []
        self.submit_calls = []

    def get_current_price(self, instrument):
        return self.price

    def get_positions(self):
        return self.positions

    def get_open_orders(self):
        return self.open_orders

    def get_fills(self, *, start_date, end_date):
        return self.fills

    def submit_order(self, order_intent, instrument):
        self.submit_calls.append((order_intent, instrument))
        if self.submit_raise is not None:
            raise self.submit_raise
        return self.submit_response or ExecutionRecord(
            internal_order_id=order_intent.internal_order_id, broker="kis", broker_order_id="kis-999",
            requested_quantity=order_intent.quantity, requested_price=order_intent.limit_price,
            filled_quantity=0.0, average_fill_price=None, status="ACCEPTED",
            submitted_at=NOW, updated_at=NOW,
        )


class TestSubmitOrderSide:
    def test_buy_side_rejected(self):
        adapter = KISBrokerAdapter(_FakeKISBroker())
        with pytest.raises(KISBrokerAdapterError):
            adapter.submit_order("AAPL", qty=1, side="buy")

    def test_sell_success_returns_accepted_alpaca_shaped_response(self):
        adapter = KISBrokerAdapter(_FakeKISBroker(), now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=1, side="sell", client_order_id="exit-AAPL-1")
        assert response.status_code in (200, 201)
        assert response.data["status"] == "accepted"
        assert response.data["id"] == "kis-999"
        assert response.data["filled_qty"] is None

    def test_sell_quantity_exceeds_position_blocked_via_gate(self):
        broker = _FakeKISBroker(positions=[
            Position(symbol="AAPL", quantity=1, average_fill_price=95.0, unrealized_pnl=0.0,
                      realized_pnl=0.0, as_of=NOW, source="kis_balance")
        ])
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=5, side="sell", client_order_id="exit-AAPL-2")
        assert response.status_code not in (200, 201)
        assert broker.submit_calls == []

    def test_existing_sell_order_blocks_duplicate(self):
        broker = _FakeKISBroker(open_orders=[{"pdno": "AAPL"}])
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=1, side="sell", client_order_id="exit-AAPL-3")
        assert response.status_code not in (200, 201)
        assert broker.submit_calls == []

    def test_ambiguous_response_propagates_not_swallowed(self):
        broker = _FakeKISBroker(submit_raise=KISAmbiguousResponseError("timeout"))
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        with pytest.raises(KISAmbiguousResponseError):
            adapter.submit_order("AAPL", qty=1, side="sell", client_order_id="exit-AAPL-4")

    def test_definite_kis_error_returns_non_2xx_not_exception(self):
        broker = _FakeKISBroker(submit_raise=KISBrokerError("rejected"))
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=1, side="sell", client_order_id="exit-AAPL-5")
        assert response.status_code not in (200, 201)

    def test_zero_position_blocks_zero_broker_calls(self):
        broker = _FakeKISBroker(positions=[])
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=1, side="sell", client_order_id="exit-AAPL-6")
        assert response.status_code not in (200, 201)
        assert broker.submit_calls == []


class TestGetOrderByClientOrderId:
    def test_unknown_client_order_id_returns_none(self):
        adapter = KISBrokerAdapter(_FakeKISBroker(), now_fn=lambda: NOW)
        assert adapter.get_order_by_client_order_id("nonexistent") is None

    def test_known_order_matches_fill_history(self):
        broker = _FakeKISBroker(fills=[{"ODNO": "kis-999", "ft_ccld_qty": "1", "ft_ccld_unpr3": "101.5"}])
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=1, side="sell", client_order_id="exit-AAPL-7")
        assert response.status_code in (200, 201)
        info = adapter.get_order_by_client_order_id("exit-AAPL-7")
        assert info is not None
        assert info["status"] == "filled"
        assert info["filled_qty"] == 1.0

    def test_known_order_matches_open_orders_when_no_fill(self):
        broker = _FakeKISBroker(open_orders=[])
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=1, side="sell", client_order_id="exit-AAPL-8")
        assert response.status_code in (200, 201)
        broker.open_orders = [{"ODNO": "kis-999"}]
        info = adapter.get_order_by_client_order_id("exit-AAPL-8")
        assert info["status"] == "accepted"


class TestGetPositions:
    def test_delegates_to_kis_broker(self):
        broker = _FakeKISBroker()
        adapter = KISBrokerAdapter(broker)
        assert adapter.get_positions() == broker.positions
