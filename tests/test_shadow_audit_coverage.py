"""CODEX-048: end-to-end Shadow audit coverage of the REAL pipelines.

tests/test_shadow_audit.py covers the store; this file proves the store
is actually written from every branch of the buy cycle AND the sell
path -- the specific gap Codex found (`shadow_mode.persist()` was called
only from kis_live_trading.py's buy cycle, so the whole sell path
recorded nothing).
"""
from datetime import datetime, timezone

import pytest

import kis_live_trading as klt
import kis_position_manager as kpm
import shadow_audit
from brokers.kis_broker import KISBrokerError
from brokers.kis_broker_adapter import KISBrokerAdapter
from config.live_rollout_config import LiveRolloutConfig
from domain.account_snapshot import AccountSnapshot
from domain.execution_event import ExecutionRecord
from domain.position import Position
from execution import idempotency
from operations import kill_switch as ops_kill_switch
from state_store import db as state_db

NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
ACCOUNT_ID = "12345678"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path / "KILL_SWITCH.json"))
    monkeypatch.setenv("OPERATIONS_HALT_STATE_FILE", str(tmp_path / "OPS_HALT.json"))
    monkeypatch.setenv("SHADOW_MODE_LOG_FILE", str(tmp_path / "SHADOW.jsonl"))
    monkeypatch.setenv("RECONCILIATION_STATE_FILE", str(tmp_path / "RECON.json"))
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "KIS_ORDER_IDEMPOTENCY.lock")
    monkeypatch.setenv("VALIDATED_COMMIT", "c1")
    monkeypatch.setenv("DEPLOYED_COMMIT", "c1")
    monkeypatch.setenv("KIS_ALLOWED_ACCOUNT_NO", ACCOUNT_ID)
    yield


def _rollout(**overrides):
    kwargs = dict(
        enabled=True, allowed_symbols=frozenset({"AAPL"}), max_quantity_per_order=1,
        max_open_positions=1, max_positions_per_strategy=1, max_daily_entries=1, regular_session_only=True,
        allow_fractional=False, allow_market_order=False, allow_extended_hours=False,
        allow_leverage=False, allow_inverse=False, allow_short=False, allow_margin=False,
        max_price_deviation_percent=0.30,
    )
    kwargs.update(overrides)
    return LiveRolloutConfig(**kwargs)


class _FakeBroker:
    def __init__(self, price=100.1, cash_usd=1000.0, positions=None, open_orders=None,
                 fills=None, read_exc=None, submit_raise=None):
        self.price = price
        self.cash_usd = cash_usd
        self.positions = positions if positions is not None else []
        self.open_orders = open_orders if open_orders is not None else []
        self.fills = fills if fills is not None else []
        self.read_exc = read_exc
        self.submit_raise = submit_raise
        self.submit_calls = []

    def get_current_price(self, instrument):
        return self.price

    def get_account_snapshot(self, *, source_label="kis_balance"):
        # ORACLE-CASH-01: the balance read carries no cash field; sizing
        # comes from the per-candidate orderable-amount read below.
        return AccountSnapshot(
            krw_cash=None, usd_cash=None, usd_orderable_cash=None,
            usd_reserved_in_open_orders=0.0, as_of=NOW, source=source_label,
            account_id=ACCOUNT_ID, cash_source="TTTS3012R_DOES_NOT_PROVIDE",
        )

    def get_orderable_usd(self, instrument, limit_price_usd):
        return self.cash_usd

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

    def submit_order(self, order_intent, instrument, *, authorization=None):
        self.submit_calls.append(order_intent)
        if self.submit_raise is not None:
            raise self.submit_raise
        return ExecutionRecord(
            internal_order_id=order_intent.internal_order_id, broker="kis",
            broker_order_id="kis-1", requested_quantity=order_intent.quantity,
            requested_price=order_intent.limit_price, filled_quantity=0.0,
            average_fill_price=None, status="ACCEPTED", submitted_at=NOW, updated_at=NOW,
        )


def _patch_common(monkeypatch, score=100):
    monkeypatch.setattr(klt.pso, "load_watchlist", lambda: ["AAPL"])
    monkeypatch.setattr(klt.pso, "analyze_stock", lambda s: {
        "symbol": s, "price": 100.0, "ma200": 90.0, "rsi": 50.0, "volume_ratio": 1.5,
        "score": score,
    })
    monkeypatch.setattr(klt.pso, "get_us_market_session", lambda: "regular")


def _events():
    return shadow_audit.read_events()


def _event_types():
    return [row["event_type"] for row in _events()]


def _assert_every_run_terminated():
    assert shadow_audit.runs_without_terminal_event() == []


class TestBuyCycleAuditCoverage:
    def test_approved_buy_records_the_full_lifecycle(self, monkeypatch):
        _patch_common(monkeypatch)
        broker = _FakeBroker()
        klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(), now=NOW)
        types = _event_types()
        assert "SIGNAL_RECEIVED" in types
        assert "GATE_APPROVED" in types
        assert "EXECUTION_PLANNED" in types
        assert "SHADOW_COMPLETED" in types
        _assert_every_run_terminated()

    def test_halt_records_halt_blocked_and_terminates(self, monkeypatch):
        _patch_common(monkeypatch)
        ops_kill_switch.set_halt(True, reason="test", actor="tester")
        with pytest.raises(klt.KISLiveTradingError):
            klt.run_live_buy_entry_cycle(broker=_FakeBroker(), live_rollout=_rollout(), now=NOW)
        assert "HALT_BLOCKED" in _event_types()
        _assert_every_run_terminated()

    def test_config_block_records_config_blocked_and_terminates(self, monkeypatch):
        _patch_common(monkeypatch)
        with pytest.raises(klt.KISLiveTradingError):
            klt.run_live_buy_entry_cycle(
                broker=_FakeBroker(), live_rollout=_rollout(enabled=False), now=NOW,
            )
        assert "CONFIG_BLOCKED" in _event_types()
        _assert_every_run_terminated()

    def test_commit_mismatch_records_config_blocked(self, monkeypatch):
        _patch_common(monkeypatch)
        monkeypatch.setenv("VALIDATED_COMMIT", "c1")
        monkeypatch.setenv("DEPLOYED_COMMIT", "c2")
        with pytest.raises(klt.KISLiveTradingError):
            klt.run_live_buy_entry_cycle(broker=_FakeBroker(), live_rollout=_rollout(), now=NOW)
        assert "CONFIG_BLOCKED" in _event_types()
        _assert_every_run_terminated()

    def test_symbol_not_allowed_records_instrument_blocked(self, monkeypatch):
        _patch_common(monkeypatch)
        klt.run_live_buy_entry_cycle(
            broker=_FakeBroker(), live_rollout=_rollout(allowed_symbols=frozenset({"MSFT"})),
            now=NOW,
        )
        assert "INSTRUMENT_BLOCKED" in _event_types()
        _assert_every_run_terminated()

    def test_insufficient_cash_records_cash_blocked(self, monkeypatch):
        _patch_common(monkeypatch)
        klt.run_live_buy_entry_cycle(
            broker=_FakeBroker(cash_usd=1.0), live_rollout=_rollout(), now=NOW,
        )
        assert "CASH_BLOCKED" in _event_types()
        _assert_every_run_terminated()

    def test_reconciliation_failure_records_reconciliation_blocked(self, monkeypatch):
        _patch_common(monkeypatch)
        broker = _FakeBroker(positions=[Position(
            symbol="TSLA", quantity=3, average_fill_price=200.0, unrealized_pnl=0.0,
            realized_pnl=0.0, as_of=NOW, source="kis_balance",
        )])
        klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(), now=NOW)
        assert "RECONCILIATION_BLOCKED" in _event_types()
        assert broker.submit_calls == []
        _assert_every_run_terminated()

    def test_unknown_order_records_unknown_order_blocked(self, monkeypatch):
        from helpers_order_state import register_and_drive

        _patch_common(monkeypatch)
        conn = state_db.open_db()
        register_and_drive(
            conn, internal_order_id="prior-1", signal_id="prior-1", symbol="MSFT", side="buy",
            trading_date="2026-07-29", target="UNKNOWN", broker_order_id="kis-prior",
        )
        conn.close()
        broker = _FakeBroker(open_orders=[{"ODNO": "kis-prior"}])
        klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(), now=NOW)
        assert "UNKNOWN_ORDER_BLOCKED" in _event_types()
        assert broker.submit_calls == []
        _assert_every_run_terminated()

    def test_duplicate_signal_records_duplicate_blocked(self, monkeypatch):
        _patch_common(monkeypatch)
        broker = _FakeBroker()
        klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(), now=NOW)
        # A second cycle for the same signal/symbol/day hits the durable
        # idempotency guard. The KIS-side open order it just created also
        # has to be visible, or reconciliation would block first.
        broker.open_orders = [{"ODNO": "kis-1", "pdno": "AAPL"}]
        klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(), now=NOW)
        assert "DUPLICATE_BLOCKED" in _event_types()
        _assert_every_run_terminated()

    def test_unexpected_error_records_shadow_error(self, monkeypatch):
        _patch_common(monkeypatch)

        def _boom(symbol):
            raise RuntimeError("analysis exploded")

        monkeypatch.setattr(klt.pso, "analyze_stock", _boom)
        results = klt.run_live_buy_entry_cycle(
            broker=_FakeBroker(), live_rollout=_rollout(), now=NOW,
        )
        assert results["blocked"]
        assert "SHADOW_ERROR" in _event_types()
        _assert_every_run_terminated()


def _seed_internal_position(symbol="AAPL", qty=5, avg_price=95.0):
    record = kpm.create_kis_position_after_buy(
        strategy_id="TEST", strategy_version="v1", symbol=symbol, quantity=qty,
        client_order_id=f"seed-{symbol}", broker_order_id=f"kis-seed-{symbol}", now=NOW,
    )
    from positions import lifecycle
    lifecycle.record_fill(record["position_id"], qty, avg_price)
    return kpm.finalize_stop_and_targets_from_fill(record["position_id"], avg_price)


class TestSellPathAuditCoverage:
    """The gap Codex found: the sell path recorded nothing at all."""

    def test_approved_sell_records_the_full_lifecycle(self):
        _seed_internal_position(qty=5)
        broker = _FakeBroker(positions=[Position(
            symbol="AAPL", quantity=5, average_fill_price=95.0, unrealized_pnl=0.0,
            realized_pnl=0.0, as_of=NOW, source="kis_balance",
        )])
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=1, side="sell", client_order_id="exit-1")
        assert response.status_code in (200, 201)
        types = _event_types()
        assert "SIGNAL_RECEIVED" in types
        assert "GATE_APPROVED" in types
        assert "EXECUTION_PLANNED" in types
        assert "SHADOW_COMPLETED" in types
        assert all(row["side"] == "sell" for row in _events())
        _assert_every_run_terminated()

    def test_gate_blocked_sell_is_recorded(self):
        _seed_internal_position(qty=1)
        broker = _FakeBroker(positions=[Position(
            symbol="AAPL", quantity=1, average_fill_price=95.0, unrealized_pnl=0.0,
            realized_pnl=0.0, as_of=NOW, source="kis_balance",
        )])
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=5, side="sell", client_order_id="exit-2")
        assert response.status_code not in (200, 201)
        assert "GATE_REJECTED" in _event_types()
        assert broker.submit_calls == []
        _assert_every_run_terminated()

    def test_reconciliation_blocked_sell_is_recorded(self):
        _seed_internal_position(qty=5)
        broker = _FakeBroker(read_exc=KISBrokerError("KIS unreachable"))
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=1, side="sell", client_order_id="exit-3")
        assert response.status_code not in (200, 201)
        assert "RECONCILIATION_BLOCKED" in _event_types()
        assert broker.submit_calls == []
        _assert_every_run_terminated()

    def test_unknown_order_blocked_sell_is_recorded(self):
        from helpers_order_state import register_and_drive

        conn = state_db.open_db()
        register_and_drive(
            conn, internal_order_id="prior-1", signal_id="prior-1", symbol="MSFT", side="buy",
            trading_date="2026-07-29", target="UNKNOWN", broker_order_id="kis-prior",
        )
        conn.close()
        _seed_internal_position(qty=5)
        broker = _FakeBroker(
            positions=[Position(
                symbol="AAPL", quantity=5, average_fill_price=95.0, unrealized_pnl=0.0,
                realized_pnl=0.0, as_of=NOW, source="kis_balance",
            )],
            open_orders=[{"ODNO": "kis-prior"}],
        )
        adapter = KISBrokerAdapter(broker, now_fn=lambda: NOW)
        response = adapter.submit_order("AAPL", qty=1, side="sell", client_order_id="exit-4")
        assert response.status_code not in (200, 201)
        assert "UNKNOWN_ORDER_BLOCKED" in _event_types()
        assert broker.submit_calls == []
        _assert_every_run_terminated()
