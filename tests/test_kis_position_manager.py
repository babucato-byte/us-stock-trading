from datetime import datetime, timedelta, timezone

import pytest

import kis_position_manager as kpm
import risk_config
from brokers.kis_broker_adapter import KISBrokerAdapter
from config import scalping_strategy_v1_config as strat_cfg
from domain.position import Position
from positions import states, store

NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path / "KILL_SWITCH.json"))
    monkeypatch.setenv("RECONCILIATION_STATE_FILE", str(tmp_path / "RECONCILIATION_STATE.json"))
    from execution import idempotency
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "KIS_ORDER_IDEMPOTENCY.lock")
    yield


class _FakeKISBroker:
    def __init__(self, price=100.0, positions=None, open_orders=None, fills=None, submit_responses=None):
        self.price = price
        self.positions = positions if positions is not None else []
        self.open_orders = open_orders or []
        self.fills = fills or []
        self.submit_calls = []
        self._submit_responses = submit_responses or []

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
        from domain.execution_event import ExecutionRecord
        if self._submit_responses:
            return self._submit_responses.pop(0)
        return ExecutionRecord(
            internal_order_id=order_intent.internal_order_id, broker="kis", broker_order_id="kis-exit-1",
            requested_quantity=order_intent.quantity, requested_price=order_intent.limit_price,
            filled_quantity=0.0, average_fill_price=None, status="ACCEPTED",
            submitted_at=NOW, updated_at=NOW,
        )


def _create_filled_position(broker, symbol="AAPL", qty=10, avg_price=100.0):
    record = kpm.create_kis_position_after_buy(
        strategy_id="TEST_STRAT", strategy_version="v1", symbol=symbol, quantity=qty,
        client_order_id=f"kislive-{symbol}-1", broker_order_id="kis-entry-1", now=NOW,
    )
    from positions import lifecycle
    lifecycle.record_fill(record["position_id"], qty, avg_price)
    return kpm.finalize_stop_and_targets_from_fill(record["position_id"], avg_price)


class TestCreateKISPositionAfterBuy:
    def test_creates_position_in_entry_submitted(self):
        record = kpm.create_kis_position_after_buy(
            strategy_id="TEST_STRAT", strategy_version="v1", symbol="AAPL", quantity=5,
            client_order_id="kislive-AAPL-1", broker_order_id="kis-entry-1", now=NOW,
        )
        assert record["state"] == states.ENTRY_SUBMITTED
        assert record["symbol"] == "AAPL"
        assert record["requested_qty"] == 5
        assert record["broker_order_id"] == "kis-entry-1"
        assert record["stop_price"] is None  # finalized only after real fill


class TestFinalizeStopAndTargetsFromFill:
    def test_computes_from_actual_fill_price_not_signal_price(self):
        record = kpm.create_kis_position_after_buy(
            strategy_id="TEST_STRAT", strategy_version="v1", symbol="AAPL", quantity=5,
            client_order_id="kislive-AAPL-2", broker_order_id="kis-entry-2", now=NOW,
        )
        from positions import lifecycle
        lifecycle.record_fill(record["position_id"], 5, 98.0)  # actual fill below the signal/limit price
        finalized = kpm.finalize_stop_and_targets_from_fill(record["position_id"], 98.0)
        expected_stop = 98.0 * (1 + risk_config.STOP_LOSS_RATE)
        assert finalized["stop_price"] == pytest.approx(expected_stop)
        risk_per_share = 98.0 - expected_stop
        assert finalized["target_1_price"] == pytest.approx(98.0 + risk_per_share * strat_cfg.TARGET_1_R_MULTIPLE)
        assert finalized["target_2_price"] == pytest.approx(98.0 + risk_per_share * strat_cfg.TARGET_2_R_MULTIPLE)

    def test_idempotent_does_not_clobber_later_state(self):
        record = kpm.create_kis_position_after_buy(
            strategy_id="TEST_STRAT", strategy_version="v1", symbol="AAPL", quantity=5,
            client_order_id="kislive-AAPL-3", broker_order_id="kis-entry-3", now=NOW,
        )
        from positions import lifecycle
        lifecycle.record_fill(record["position_id"], 5, 100.0)
        first = kpm.finalize_stop_and_targets_from_fill(record["position_id"], 100.0)
        second = kpm.finalize_stop_and_targets_from_fill(record["position_id"], 999.0)  # should be ignored
        assert second["stop_price"] == first["stop_price"]


class TestSyncKisFillsAndManageExits:
    def test_stop_loss_triggers_exactly_one_sell(self):
        record = _create_filled_position(None, avg_price=100.0)
        stop_price = record["stop_price"]
        broker = _FakeKISBroker(
            price=stop_price - 1.0,  # below stop
            positions=[Position(symbol="AAPL", quantity=10, average_fill_price=100.0,
                                 unrealized_pnl=0.0, realized_pnl=0.0, as_of=NOW, source="kis_balance")],
        )
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        summary = kpm.sync_kis_fills_and_manage_exits(kis_broker=broker, broker_adapter=adapter, now=NOW)
        assert len(broker.submit_calls) == 1
        assert broker.submit_calls[0][0].quantity == 10  # full exit on stop-loss
        assert "AAPL" in summary["managed"]

    def test_target_2_take_profit_triggers_exactly_one_full_sell(self):
        # "익절"/"일반 전략 매도": target_2 uses the SAME _force_full_exit()
        # code path as STOP_LOSS (just a different reason string), so
        # this proves the identical KISBrokerAdapter wiring a second,
        # independent way -- via a position already past target_1.
        record = _create_filled_position(None, avg_price=100.0)
        target_2 = record["target_2_price"]
        with store.locked_position(record["position_id"]) as locked:
            states.validate_transition(locked["state"], states.TARGET_1_ACTIVE)
            locked["state"] = states.TARGET_1_ACTIVE
        broker = _FakeKISBroker(
            price=target_2 + 0.5,
            positions=[Position(symbol="AAPL", quantity=10, average_fill_price=100.0,
                                 unrealized_pnl=0.0, realized_pnl=0.0, as_of=NOW, source="kis_balance")],
        )
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        summary = kpm.sync_kis_fills_and_manage_exits(kis_broker=broker, broker_adapter=adapter, now=NOW)
        assert len(broker.submit_calls) == 1
        assert broker.submit_calls[0][0].quantity == 10
        assert "AAPL" in summary["managed"]

    def test_target_1_triggers_exactly_one_partial_sell(self):
        record = _create_filled_position(None, avg_price=100.0)
        target_1 = record["target_1_price"]
        broker = _FakeKISBroker(
            price=target_1 + 0.5,
            positions=[Position(symbol="AAPL", quantity=10, average_fill_price=100.0,
                                 unrealized_pnl=0.0, realized_pnl=0.0, as_of=NOW, source="kis_balance")],
        )
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        kpm.sync_kis_fills_and_manage_exits(kis_broker=broker, broker_adapter=adapter, now=NOW)
        assert len(broker.submit_calls) == 1
        assert broker.submit_calls[0][0].quantity == int(10 * strat_cfg.PARTIAL_EXIT_FRACTION_AT_TARGET_1)

    def test_no_full_resell_after_partial_fill(self):
        # After a target-1 partial exit is SUBMITTED, the position moves
        # to PARTIAL_EXIT_SUBMITTED -- not one of the exit-eligible states
        # (_execute_exit's own duplicate-exit-prevention lock already
        # guarantees this structurally: a second concurrent/subsequent
        # attempt on the same position sees the updated state and has
        # nothing left to do). A second tick at the same price must not
        # submit a second sell for the already-in-flight exit.
        record = _create_filled_position(None, avg_price=100.0)
        target_1 = record["target_1_price"]
        broker = _FakeKISBroker(
            price=target_1 + 0.5,
            positions=[Position(symbol="AAPL", quantity=10, average_fill_price=100.0,
                                 unrealized_pnl=0.0, realized_pnl=0.0, as_of=NOW, source="kis_balance")],
        )
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        kpm.sync_kis_fills_and_manage_exits(kis_broker=broker, broker_adapter=adapter, now=NOW)
        assert len(broker.submit_calls) == 1
        updated = store.load_position(record["position_id"])
        assert updated["state"] == states.PARTIAL_EXIT_SUBMITTED
        # Second tick: the position is mid-flight (not exit-eligible), so
        # it is simply skipped -- no second sell attempt.
        kpm.sync_kis_fills_and_manage_exits(kis_broker=broker, broker_adapter=adapter, now=NOW)
        assert len(broker.submit_calls) == 1  # unchanged

    def test_reconciliation_mismatch_blocks_exit_zero_broker_calls(self):
        record = _create_filled_position(None, avg_price=100.0)
        stop_price = record["stop_price"]
        broker = _FakeKISBroker(
            price=stop_price - 1.0,
            positions=[Position(symbol="AAPL", quantity=3, average_fill_price=100.0,  # mismatch: internal has 10
                                 unrealized_pnl=0.0, realized_pnl=0.0, as_of=NOW, source="kis_balance")],
        )
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        summary = kpm.sync_kis_fills_and_manage_exits(kis_broker=broker, broker_adapter=adapter, now=NOW)
        assert broker.submit_calls == []
        assert any(s == "AAPL" for s, _ in summary["reconciliation_blocked"])

    def test_no_exit_when_price_between_stop_and_target(self):
        record = _create_filled_position(None, avg_price=100.0)
        broker = _FakeKISBroker(
            price=100.5,
            positions=[Position(symbol="AAPL", quantity=10, average_fill_price=100.0,
                                 unrealized_pnl=0.0, realized_pnl=0.0, as_of=NOW, source="kis_balance")],
        )
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        kpm.sync_kis_fills_and_manage_exits(kis_broker=broker, broker_adapter=adapter, now=NOW)
        assert broker.submit_calls == []

    def test_syncs_pending_fill_before_managing(self):
        entry = kpm.create_kis_position_after_buy(
            strategy_id="TEST_STRAT", strategy_version="v1", symbol="AAPL", quantity=10,
            client_order_id="kislive-AAPL-9", broker_order_id="kis-entry-9", now=NOW,
        )
        broker = _FakeKISBroker(
            price=50.0,  # far below any stop -- should not force-exit since it's still ENTRY_SUBMITTED pre-fill
            positions=[],
            fills=[{"ODNO": "kis-entry-9", "ft_ccld_qty": "10", "ft_ccld_unpr3": "100.0"}],
        )
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        summary = kpm.sync_kis_fills_and_manage_exits(kis_broker=broker, broker_adapter=adapter, now=NOW)
        assert "AAPL" in summary["synced_fills"]
        updated = store.load_position(entry["position_id"])
        assert updated["state"] == states.STOP_ACTIVE
        assert updated["stop_price"] is not None
