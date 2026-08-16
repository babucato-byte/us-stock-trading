from datetime import datetime, timezone

import pytest

import kis_position_manager as kpm
from brokers.kis_broker import KISAmbiguousResponseError, KISBrokerError
from brokers.kis_broker_adapter import KISBrokerAdapter, KISBrokerAdapterError
from domain.account_snapshot import AccountSnapshot
from domain.execution_event import ExecutionRecord
from domain.position import Position

NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
ACCOUNT_ID = "12345678"


def _seed_internal_position(symbol="AAPL", qty=5, avg_price=95.0):
    """CODEX-044: the Execution Engine reconciles the internal position
    store against KIS's own positions inside the order path, so a sell
    test must have a genuinely matching internal position -- not just a
    KIS one."""
    record = kpm.create_kis_position_after_buy(
        strategy_id="TEST_STRAT", strategy_version="v1", symbol=symbol, quantity=qty,
        client_order_id=f"seed-{symbol}-{qty}", broker_order_id=f"kis-seed-{symbol}", now=NOW,
    )
    from positions import lifecycle
    lifecycle.record_fill(record["position_id"], qty, avg_price)
    return kpm.finalize_stop_and_targets_from_fill(record["position_id"], avg_price)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setenv("RECONCILIATION_STATE_FILE", str(tmp_path / "RECONCILIATION_STATE.json"))
    monkeypatch.setenv("OPERATIONS_HALT_STATE_FILE", str(tmp_path / "OPS_HALT.json"))
    monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path / "KILL_SWITCH.json"))
    from execution import idempotency
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "KIS_ORDER_IDEMPOTENCY.lock")
    yield


class _FakeKISBroker:
    def __init__(self, price=100.0, positions=None, open_orders=None, submit_response=None,
                 submit_raise=None, fills=None, read_exc=None):
        self.price = price
        self.read_exc = read_exc
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
        if self.read_exc is not None:
            raise self.read_exc
        return self.positions

    def get_open_orders(self):
        if self.read_exc is not None:
            raise self.read_exc
        return self.open_orders

    def get_fills(self, *, start_date, end_date):
        if self.read_exc is not None:
            raise self.read_exc
        return self.fills

    def get_account_snapshot(self, *, source_label="kis_balance"):
        return AccountSnapshot(
            krw_cash=0.0, usd_cash=10000.0, usd_orderable_cash=10000.0,
            usd_reserved_in_open_orders=0.0, as_of=NOW, source=source_label,
            account_id=ACCOUNT_ID,
        )

    def submit_order(self, order_intent, instrument, *, authorization=None,
                     bootstrap_capability=None):
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
        _seed_internal_position(qty=5)
        adapter = KISBrokerAdapter(_FakeKISBroker(), now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=1, side="sell", client_order_id="exit-AAPL-1")
        assert response.status_code in (200, 201)
        assert response.data["status"] == "accepted"
        assert response.data["id"] == "kis-999"
        assert response.data["filled_qty"] is None

    def test_sell_quantity_exceeds_position_blocked_via_gate(self):
        _seed_internal_position(qty=1)
        broker = _FakeKISBroker(positions=[
            Position(symbol="AAPL", quantity=1, average_fill_price=95.0, unrealized_pnl=0.0,
                      realized_pnl=0.0, as_of=NOW, source="kis_balance")
        ])
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=5, side="sell", client_order_id="exit-AAPL-2")
        assert response.status_code not in (200, 201)
        assert broker.submit_calls == []

    def test_existing_sell_order_blocks_duplicate(self):
        _seed_internal_position(qty=5)
        broker = _FakeKISBroker(open_orders=[{"pdno": "AAPL"}])
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=1, side="sell", client_order_id="exit-AAPL-3")
        assert response.status_code not in (200, 201)
        assert broker.submit_calls == []

    def test_ambiguous_response_propagates_not_swallowed(self):
        _seed_internal_position(qty=5)
        broker = _FakeKISBroker(submit_raise=KISAmbiguousResponseError("timeout"))
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        with pytest.raises(KISAmbiguousResponseError):
            adapter.submit_order("AAPL", qty=1, side="sell", client_order_id="exit-AAPL-4")

    def test_definite_kis_error_returns_non_2xx_not_exception(self):
        _seed_internal_position(qty=5)
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

    def test_reconciliation_read_failure_blocks_zero_broker_calls(self):
        # CODEX-044: the sell path performs the reconciliation reads
        # itself. If KIS cannot be read, there IS no snapshot, so the
        # order is blocked -- never approved on a stale assumption.
        _seed_internal_position(qty=5)
        broker = _FakeKISBroker(read_exc=KISBrokerError("KIS unreachable"))
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=1, side="sell", client_order_id="exit-AAPL-9")
        assert response.status_code not in (200, 201)
        assert broker.submit_calls == []

    def test_position_mismatch_blocks_zero_broker_calls(self):
        # Internal store says 5 shares, KIS says 4 -- new sells blocked.
        _seed_internal_position(qty=5)
        broker = _FakeKISBroker(positions=[
            Position(symbol="AAPL", quantity=4, average_fill_price=95.0, unrealized_pnl=0.0,
                      realized_pnl=0.0, as_of=NOW, source="kis_balance")
        ])
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=1, side="sell", client_order_id="exit-AAPL-10")
        assert response.status_code not in (200, 201)
        assert broker.submit_calls == []

    def test_untracked_kis_open_order_blocks_zero_broker_calls(self):
        # A KIS open order this codebase has never heard of means
        # something else is trading the account -- fail closed.
        _seed_internal_position(qty=5)
        broker = _FakeKISBroker(open_orders=[{"ODNO": "kis-stranger", "pdno": "MSFT"}])
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=1, side="sell", client_order_id="exit-AAPL-11")
        assert response.status_code not in (200, 201)
        assert broker.submit_calls == []

    def test_unknown_order_on_any_symbol_blocks_new_sell_zero_broker_calls(self):
        # CODEX-044: account-wide, not (symbol, side)-scoped -- a prior
        # UNKNOWN BUY of a DIFFERENT symbol still blocks this sell.
        from state_store import db as state_db
        from helpers_order_state import register_and_drive
        conn = state_db.open_db()
        register_and_drive(
            conn, internal_order_id="prior-buy-1", signal_id="prior-buy-1", symbol="MSFT",
            side="buy", trading_date="2026-07-29", target="UNKNOWN",
        )
        _seed_internal_position(qty=5)
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
        _seed_internal_position(qty=5)
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
        _seed_internal_position(qty=5)
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
        _seed_internal_position(qty=5)
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
        _seed_internal_position(qty=5)
        broker = _FakeKISBroker(fills=[{"ODNO": "kis-999", "ft_ccld_qty": "0", "ft_ccld_unpr3": "0"}])
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=2, side="sell", client_order_id="exit-AAPL-22")
        assert response.status_code in (200, 201)
        broker.open_orders = [{"ODNO": "kis-999"}]
        info = adapter.get_order_by_client_order_id("exit-AAPL-22")
        assert info["status"] == "accepted"

    def test_cumulative_fill_exceeding_requested_quantity_halts_and_reports_data_integrity_error(self):
        from operations import kill_switch as ops_kill_switch
        _seed_internal_position(qty=5)
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
        _seed_internal_position(qty=5)
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
