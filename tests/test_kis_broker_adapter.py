from datetime import datetime, timezone

import pytest

from brokers.kis_broker import KISAmbiguousResponseError, KISBrokerError
from brokers.kis_broker_adapter import KISBrokerAdapter, KISBrokerAdapterError
from domain.execution_event import ExecutionRecord
from domain.position import Position
from reconciliation import reconciliation_state

NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("RECONCILIATION_STATE_FILE", str(tmp_path / "RECONCILIATION_STATE.json"))
    monkeypatch.setenv("OPERATIONS_HALT_STATE_FILE", str(tmp_path / "OPS_HALT.json"))
    from execution import idempotency
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "KIS_ORDER_IDEMPOTENCY.lock")
    # CODEX-044: the sell gate's reconciliation_ok now reads a real,
    # periodically-refreshed result (normally kept fresh by
    # kis_position_manager.sync_kis_fills_and_manage_exits()'s tick) --
    # seed a clean one so these tests exercise sell-path behavior, not
    # the (separately, explicitly tested) reconciliation gate itself.
    reconciliation_state.record_result(clean=True, mismatch_count=0, now=NOW)
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

    def submit_order(self, order_intent, instrument, *, authorization=None):
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

    def test_no_recorded_reconciliation_blocks_zero_broker_calls(self, tmp_path, monkeypatch):
        # Overrides the autouse fixture's seeded clean state by pointing
        # at a path nothing has ever written to -- "결과 없음".
        monkeypatch.setenv("RECONCILIATION_STATE_FILE", str(tmp_path / "NEVER_WRITTEN.json"))
        broker = _FakeKISBroker()
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=1, side="sell", client_order_id="exit-AAPL-9")
        assert response.status_code not in (200, 201)
        assert broker.submit_calls == []

    def test_stale_reconciliation_blocks_zero_broker_calls(self):
        from datetime import timedelta
        reconciliation_state.record_result(
            clean=True, mismatch_count=0,
            now=NOW - timedelta(seconds=reconciliation_state.DEFAULT_MAX_AGE_SECONDS + 1),
        )
        broker = _FakeKISBroker()
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=1, side="sell", client_order_id="exit-AAPL-10")
        assert response.status_code not in (200, 201)
        assert broker.submit_calls == []

    def test_dirty_reconciliation_blocks_zero_broker_calls(self):
        reconciliation_state.record_result(clean=False, mismatch_count=1, now=NOW)
        broker = _FakeKISBroker()
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=1, side="sell", client_order_id="exit-AAPL-11")
        assert response.status_code not in (200, 201)
        assert broker.submit_calls == []

    def test_unknown_sell_order_for_symbol_blocks_new_sell_zero_broker_calls(self):
        from execution import idempotency
        from state_store import db as state_db
        conn = state_db.open_db()
        idempotency.register(
            conn, internal_order_id="prior-sell-1", signal_id="prior-sell-1", symbol="AAPL",
            side="sell", trading_date="2026-07-29",
        )
        idempotency.update_status(conn, "prior-sell-1", "UNKNOWN")
        broker = _FakeKISBroker()
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=1, side="sell", client_order_id="exit-AAPL-12")
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

    def test_two_share_order_one_share_fill_reports_partially_filled_not_filled(self):
        # CODEX-045's exact scenario: a 2-share sell with only 1 share
        # filled must never be reported as "filled".
        broker = _FakeKISBroker(
            fills=[{"ODNO": "kis-999", "ft_ccld_qty": "1", "ft_ccld_unpr3": "101.5"}],
        )
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=2, side="sell", client_order_id="exit-AAPL-20")
        assert response.status_code in (200, 201)
        info = adapter.get_order_by_client_order_id("exit-AAPL-20")
        assert info["status"] == "partially_filled"
        assert info["filled_qty"] == 1.0

    def test_two_fill_rows_sum_to_full_quantity_reports_filled(self):
        broker = _FakeKISBroker(fills=[
            {"ODNO": "kis-999", "ft_ccld_qty": "1", "ft_ccld_unpr3": "100.0"},
            {"ODNO": "kis-999", "ft_ccld_qty": "1", "ft_ccld_unpr3": "102.0"},
        ])
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=2, side="sell", client_order_id="exit-AAPL-21")
        assert response.status_code in (200, 201)
        info = adapter.get_order_by_client_order_id("exit-AAPL-21")
        assert info["status"] == "filled"
        assert info["filled_qty"] == 2.0
        assert info["filled_avg_price"] == pytest.approx(101.0)  # weighted average, not the first row's price

    def test_zero_qty_fill_row_treated_as_not_filled(self):
        broker = _FakeKISBroker(fills=[{"ODNO": "kis-999", "ft_ccld_qty": "0", "ft_ccld_unpr3": "0"}])
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=2, side="sell", client_order_id="exit-AAPL-22")
        assert response.status_code in (200, 201)
        broker.open_orders = [{"ODNO": "kis-999"}]
        info = adapter.get_order_by_client_order_id("exit-AAPL-22")
        assert info["status"] == "accepted"

    def test_cumulative_fill_exceeding_requested_quantity_halts_and_reports_data_integrity_error(self):
        from operations import kill_switch as ops_kill_switch
        broker = _FakeKISBroker(fills=[
            {"ODNO": "kis-999", "ft_ccld_qty": "1", "ft_ccld_unpr3": "100.0"},
            {"ODNO": "kis-999", "ft_ccld_qty": "2", "ft_ccld_unpr3": "100.0"},  # 3 total > requested 2
        ])
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=2, side="sell", client_order_id="exit-AAPL-23")
        assert response.status_code in (200, 201)
        assert ops_kill_switch.is_halted() is False
        info = adapter.get_order_by_client_order_id("exit-AAPL-23")
        assert info["status"] == "data_integrity_error"
        assert ops_kill_switch.is_halted() is True

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
