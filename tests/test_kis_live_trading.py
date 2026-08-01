from datetime import datetime, timezone

import pytest

from brokers.kis_broker import KISAmbiguousResponseError, KISBrokerError
from config.live_rollout_config import LiveRolloutConfig
from domain.account_snapshot import AccountSnapshot
from domain.execution_event import ExecutionRecord
import kis_live_trading as klt
import shadow_mode
from operations import kill_switch as ops_kill_switch
from reconciliation import reconciliation_state

NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path / "KILL_SWITCH.json"))
    monkeypatch.setenv("OPERATIONS_HALT_STATE_FILE", str(tmp_path / "OPS_HALT.json"))
    monkeypatch.setenv("SHADOW_MODE_LOG_FILE", str(tmp_path / "SHADOW_MODE_LOG.jsonl"))
    monkeypatch.setenv("RECONCILIATION_STATE_FILE", str(tmp_path / "RECONCILIATION_STATE.json"))
    from execution import idempotency
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "KIS_ORDER_IDEMPOTENCY.lock")
    monkeypatch.setenv("VALIDATED_COMMIT", "c1")
    monkeypatch.setenv("DEPLOYED_COMMIT", "c1")
    monkeypatch.setenv("KIS_ALLOWED_ACCOUNT_NO", "12345678")
    # CODEX-044: the buy gate's reconciliation_ok now reads a real,
    # periodically-refreshed result (normally kept fresh by
    # kis_position_manager.sync_kis_fills_and_manage_exits()'s tick) --
    # seed a clean one so these tests exercise the buy path itself, not
    # the (separately, explicitly tested) reconciliation gate.
    reconciliation_state.record_result(clean=True, mismatch_count=0, now=NOW)
    yield


def _rollout(**overrides):
    kwargs = dict(
        enabled=True, allowed_symbols=frozenset({"AAPL"}), max_quantity_per_order=1,
        max_open_positions=1, max_daily_entries=1, regular_session_only=True,
        allow_fractional=False, allow_market_order=False, allow_extended_hours=False,
        allow_leverage=False, allow_inverse=False, allow_short=False, allow_margin=False,
        max_price_deviation_percent=0.30,
    )
    kwargs.update(overrides)
    return LiveRolloutConfig(**kwargs)


def _accepted_record(internal_order_id):
    return ExecutionRecord(
        internal_order_id=internal_order_id, broker="kis", broker_order_id="kis-1",
        requested_quantity=1, requested_price=100.0, filled_quantity=0.0, average_fill_price=None,
        status="ACCEPTED", submitted_at=NOW, updated_at=NOW,
    )


class _FakeBroker:
    def __init__(self, price=100.1, cash_usd=1000.0, submit_response=None, submit_raise=None,
                 open_orders=None, positions=None, fills=None, read_exc=None):
        self.price = price
        self.positions = positions if positions is not None else []
        self.fills = fills if fills is not None else []
        self.read_exc = read_exc
        self.cash_usd = cash_usd
        self.submit_response = submit_response
        self.submit_raise = submit_raise
        self.open_orders = open_orders or []
        self.submit_calls = []
        self.call_log = []

    def get_current_price(self, instrument):
        return self.price

    def get_account_snapshot(self):
        return AccountSnapshot(
            krw_cash=0.0, usd_cash=self.cash_usd, usd_orderable_cash=self.cash_usd,
            usd_reserved_in_open_orders=0.0, as_of=NOW, source="kis_balance", account_id="12345678",
        )

    def get_open_orders(self):
        self.call_log.append("get_open_orders")
        if self.read_exc is not None:
            raise self.read_exc
        return self.open_orders

    def get_positions(self):
        self.call_log.append("get_positions")
        if self.read_exc is not None:
            raise self.read_exc
        return self.positions

    def get_fills(self, *, start_date, end_date):
        self.call_log.append("get_fills")
        if self.read_exc is not None:
            raise self.read_exc
        return self.fills

    def submit_order(self, order_intent, instrument, *, authorization=None):
        self.call_log.append("submit_order")
        self.submit_calls.append((order_intent, instrument))
        if self.submit_raise is not None:
            raise self.submit_raise
        return self.submit_response or _accepted_record(order_intent.internal_order_id)


def _high_score_result(symbol):
    return {"symbol": symbol, "price": 100.0, "ma200": 90.0, "rsi": 50.0, "volume_ratio": 1.5, "score": 100}


def _low_score_result(symbol):
    return {"symbol": symbol, "price": 100.0, "ma200": 90.0, "rsi": 50.0, "volume_ratio": 1.5, "score": 50}


def _patch_common(monkeypatch, tickers=("AAPL",), analyze=None, market_session="regular"):
    # Patch the EXACT module object kis_live_trading.py already bound at
    # its own import time (`klt.pso`), not a fresh `import
    # paper_strategy_order` here -- tests/test_ai_analysis.py's
    # `test_ai_analysis_is_independent_from_order_modules` legitimately
    # pops "paper_strategy_order" from sys.modules and leaves it popped
    # (by design, proving ai_analysis doesn't transitively import order
    # modules), so a fresh import elsewhere in the suite can otherwise
    # land on a DIFFERENT module object than the one klt.pso references.
    monkeypatch.setattr(klt.pso, "load_watchlist", lambda: list(tickers))
    monkeypatch.setattr(klt.pso, "analyze_stock", analyze or _high_score_result)
    monkeypatch.setattr(klt.pso, "get_us_market_session", lambda: market_session)


class TestStructuralBlocks:
    def test_rollout_disabled_raises(self, monkeypatch):
        _patch_common(monkeypatch)
        with pytest.raises(klt.KISLiveTradingError, match="enabled is False"):
            klt.run_live_buy_entry_cycle(broker=_FakeBroker(), live_rollout=_rollout(enabled=False), now=NOW)
        rows = shadow_mode.read_all()
        assert len(rows) == 1
        assert rows[0]["risk_gate_result"] == "BLOCKED"
        assert rows[0]["symbol"] == "__CYCLE__"

    def test_halted_raises(self, monkeypatch):
        _patch_common(monkeypatch)
        ops_kill_switch.set_halt(True, reason="test", actor="tester")
        with pytest.raises(klt.KISLiveTradingError, match="HALT"):
            klt.run_live_buy_entry_cycle(broker=_FakeBroker(), live_rollout=_rollout(), now=NOW)
        rows = shadow_mode.read_all()
        assert len(rows) == 1
        assert rows[0]["risk_gate_result"] == "HALT"

    def test_entry_off_raises(self, monkeypatch):
        _patch_common(monkeypatch)
        import kill_switch_state
        kill_switch_state.activate(kill_switch_state.ENTRY_DISABLED, "test", "tester")
        with pytest.raises(klt.KISLiveTradingError, match="ENTRY_OFF"):
            klt.run_live_buy_entry_cycle(broker=_FakeBroker(), live_rollout=_rollout(), now=NOW)
        rows = shadow_mode.read_all()
        assert len(rows) == 1
        assert rows[0]["risk_gate_result"] == "BLOCKED"

    def test_commit_mismatch_raises(self, monkeypatch):
        _patch_common(monkeypatch)
        monkeypatch.setenv("DEPLOYED_COMMIT", "different")
        with pytest.raises(klt.KISLiveTradingError, match="commit"):
            klt.run_live_buy_entry_cycle(broker=_FakeBroker(), live_rollout=_rollout(), now=NOW)
        rows = shadow_mode.read_all()
        assert len(rows) == 1

    def test_missing_allowed_account_raises(self, monkeypatch):
        _patch_common(monkeypatch)
        monkeypatch.delenv("KIS_ALLOWED_ACCOUNT_NO", raising=False)
        with pytest.raises(klt.KISLiveTradingError, match="KIS_ALLOWED_ACCOUNT_NO"):
            klt.run_live_buy_entry_cycle(broker=_FakeBroker(), live_rollout=_rollout(), now=NOW)
        rows = shadow_mode.read_all()
        assert len(rows) == 1


class TestPerSymbolOutcomes:
    def test_success(self, monkeypatch):
        _patch_common(monkeypatch)
        broker = _FakeBroker()
        results = klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(), now=NOW)
        assert results["submitted"] == ["AAPL"]
        assert len(broker.submit_calls) == 1

    def test_success_persists_shadow_mode_record(self, monkeypatch):
        _patch_common(monkeypatch)
        broker = _FakeBroker()
        klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(), now=NOW)
        rows = shadow_mode.read_all()
        assert len(rows) == 1
        assert rows[0]["symbol"] == "AAPL"
        assert rows[0]["risk_gate_result"] == "APPROVED"
        assert rows[0]["kis_validation_price"] is not None
        assert rows[0]["price_difference_percent"] is not None

    def test_price_deviation_blocked_persists_shadow_mode_record(self, monkeypatch):
        _patch_common(monkeypatch)
        broker = _FakeBroker(price=105.0)  # 5% > 0.30% limit
        klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(), now=NOW)
        rows = shadow_mode.read_all()
        assert len(rows) == 1
        assert rows[0]["risk_gate_result"] == "BLOCKED"
        assert rows[0]["rejection_reason"] is not None

    def test_symbol_not_on_allow_list_skipped(self, monkeypatch):
        _patch_common(monkeypatch, tickers=("MSFT",))
        broker = _FakeBroker()
        results = klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(), now=NOW)
        assert results["submitted"] == []
        assert any(s == "MSFT" for s, _ in results["skipped"])
        assert broker.submit_calls == []
        rows = shadow_mode.read_all()
        assert len(rows) == 1
        assert rows[0]["symbol"] == "MSFT"
        assert rows[0]["risk_gate_result"] == "BLOCKED"

    def test_low_score_skipped(self, monkeypatch):
        _patch_common(monkeypatch, analyze=_low_score_result)
        broker = _FakeBroker()
        results = klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(), now=NOW)
        assert results["submitted"] == []
        assert broker.submit_calls == []

    def test_none_analysis_skipped(self, monkeypatch):
        _patch_common(monkeypatch, analyze=lambda s: None)
        broker = _FakeBroker()
        results = klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(), now=NOW)
        assert results["submitted"] == []

    def test_kis_price_check_failure_blocks(self, monkeypatch):
        _patch_common(monkeypatch)
        broker = _FakeBroker()

        def _raise_price(instrument):
            raise KISBrokerError("network down")
        monkeypatch.setattr(broker, "get_current_price", _raise_price)
        results = klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(), now=NOW)
        assert results["submitted"] == []
        assert broker.submit_calls == []
        assert any(s == "AAPL" for s, _ in results["blocked"])
        rows = shadow_mode.read_all()
        assert len(rows) == 1
        assert rows[0]["risk_gate_result"] == "BLOCKED"

    def test_insufficient_cash_blocked(self, monkeypatch):
        _patch_common(monkeypatch)
        broker = _FakeBroker(cash_usd=1.0)  # not enough for 1 share at $100
        results = klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(), now=NOW)
        assert results["submitted"] == []
        assert broker.submit_calls == []
        rows = shadow_mode.read_all()
        assert len(rows) == 1
        assert rows[0]["risk_gate_result"] == "BLOCKED"

    def test_price_deviation_exceeded_blocked_zero_broker_submit(self, monkeypatch):
        _patch_common(monkeypatch)
        broker = _FakeBroker(price=105.0)  # 5% > 0.30% limit
        results = klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(), now=NOW)
        assert results["submitted"] == []
        assert broker.submit_calls == []

    def test_outside_regular_session_blocked(self, monkeypatch):
        _patch_common(monkeypatch, market_session="premarket")
        broker = _FakeBroker()
        results = klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(), now=NOW)
        assert results["submitted"] == []
        assert broker.submit_calls == []

    def test_ambiguous_broker_response_recorded_as_blocked(self, monkeypatch):
        _patch_common(monkeypatch)
        broker = _FakeBroker(submit_raise=KISAmbiguousResponseError("timeout"))
        results = klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(), now=NOW)
        assert results["submitted"] == []
        assert len(broker.submit_calls) == 1
        assert any("UNKNOWN" in reason for _, reason in results["blocked"])

    def test_quantity_capped_by_max_quantity_per_order(self, monkeypatch):
        _patch_common(monkeypatch)
        broker = _FakeBroker(cash_usd=10_000.0, price=100.1)  # would afford ~99 shares
        klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(max_quantity_per_order=1), now=NOW)
        assert broker.submit_calls[0][0].quantity == 1

    def test_reconciliation_reads_always_precede_the_order(self, monkeypatch):
        # CODEX-044: on a completely cold start -- no recorded
        # reconciliation anywhere -- the engine still performs the real
        # KIS position/open-order/fill reads BEFORE the order is
        # submitted. There is no window in which an order can be placed
        # "before reconciliation has run".
        _patch_common(monkeypatch)
        broker = _FakeBroker()
        results = klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(), now=NOW)
        assert results["submitted"] == ["AAPL"]
        assert broker.call_log.index("get_positions") < broker.call_log.index("submit_order")
        assert broker.call_log.index("get_open_orders") < broker.call_log.index("submit_order")
        assert broker.call_log.index("get_fills") < broker.call_log.index("submit_order")

    def test_dirty_reconciliation_blocks_zero_broker_calls(self, monkeypatch):
        # CODEX-044: the block now comes from the Execution Engine's own
        # live comparison -- KIS reports a position the internal store
        # has never heard of.
        _patch_common(monkeypatch)
        from domain.position import Position
        broker = _FakeBroker(positions=[
            Position(symbol="TSLA", quantity=3, average_fill_price=200.0, unrealized_pnl=0.0,
                      realized_pnl=0.0, as_of=NOW, source="kis_balance"),
        ])
        results = klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(), now=NOW)
        assert results["submitted"] == []
        assert broker.submit_calls == []

    def test_reconciliation_read_failure_blocks_zero_broker_calls(self, monkeypatch):
        # A failed KIS read produces NO snapshot at all, so there is
        # nothing the gate could approve against.
        _patch_common(monkeypatch)
        broker = _FakeBroker(read_exc=KISBrokerError("KIS unreachable"))
        results = klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(), now=NOW)
        assert results["submitted"] == []
        assert broker.submit_calls == []

    def test_untracked_kis_open_order_blocks_new_buy_zero_broker_calls(self, monkeypatch):
        _patch_common(monkeypatch)
        broker = _FakeBroker(open_orders=[{"ODNO": "kis-stranger", "pdno": "MSFT"}])
        results = klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(), now=NOW)
        assert results["submitted"] == []
        assert broker.submit_calls == []

    def test_unknown_buy_order_for_symbol_blocks_new_buy_zero_broker_calls(self, monkeypatch):
        _patch_common(monkeypatch)
        from helpers_order_state import register_and_drive
        from state_store import db as state_db
        conn = state_db.open_db()
        register_and_drive(
            conn, internal_order_id="prior-buy-1", signal_id="prior-buy-1", symbol="AAPL",
            side="buy", trading_date="2026-07-29", target="UNKNOWN",
        )
        broker = _FakeBroker()
        results = klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(), now=NOW)
        assert results["submitted"] == []
        assert broker.submit_calls == []

    def test_unknown_sell_order_also_blocks_new_buy(self, monkeypatch):
        # CODEX-044: the UNKNOWN block is ACCOUNT-WIDE. The previous
        # (symbol, side) scoping let a new BUY through while a SELL of
        # the same symbol sat unresolved -- i.e. while this codebase did
        # not actually know the account's exposure.
        _patch_common(monkeypatch)
        from helpers_order_state import register_and_drive
        from state_store import db as state_db
        conn = state_db.open_db()
        register_and_drive(
            conn, internal_order_id="prior-sell-1", signal_id="prior-sell-1", symbol="AAPL",
            side="sell", trading_date="2026-07-29", target="UNKNOWN",
        )
        broker = _FakeBroker()
        results = klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(), now=NOW)
        assert results["submitted"] == []
        assert broker.submit_calls == []
