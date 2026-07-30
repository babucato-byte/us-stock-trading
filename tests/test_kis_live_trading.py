from datetime import datetime, timezone

import pytest

from brokers.kis_broker import KISAmbiguousResponseError, KISBrokerError
from config.live_rollout_config import LiveRolloutConfig
from domain.account_snapshot import AccountSnapshot
from domain.execution_event import ExecutionRecord
import kis_live_trading as klt
from operations import kill_switch as ops_kill_switch

NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path / "KILL_SWITCH.json"))
    monkeypatch.setenv("OPERATIONS_HALT_STATE_FILE", str(tmp_path / "OPS_HALT.json"))
    from execution import idempotency
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "KIS_ORDER_IDEMPOTENCY.lock")
    monkeypatch.setenv("VALIDATED_COMMIT", "c1")
    monkeypatch.setenv("DEPLOYED_COMMIT", "c1")
    monkeypatch.setenv("KIS_ALLOWED_ACCOUNT_NO", "12345678")
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
                 open_orders=None):
        self.price = price
        self.cash_usd = cash_usd
        self.submit_response = submit_response
        self.submit_raise = submit_raise
        self.open_orders = open_orders or []
        self.submit_calls = []

    def get_current_price(self, instrument):
        return self.price

    def get_account_snapshot(self):
        return AccountSnapshot(
            krw_cash=0.0, usd_cash=self.cash_usd, usd_orderable_cash=self.cash_usd,
            usd_reserved_in_open_orders=0.0, as_of=NOW, source="kis_balance", account_id="12345678",
        )

    def get_open_orders(self):
        return self.open_orders

    def submit_order(self, order_intent, instrument):
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

    def test_halted_raises(self, monkeypatch):
        _patch_common(monkeypatch)
        ops_kill_switch.set_halt(True, reason="test", actor="tester")
        with pytest.raises(klt.KISLiveTradingError, match="HALT"):
            klt.run_live_buy_entry_cycle(broker=_FakeBroker(), live_rollout=_rollout(), now=NOW)

    def test_entry_off_raises(self, monkeypatch):
        _patch_common(monkeypatch)
        import kill_switch_state
        kill_switch_state.activate(kill_switch_state.ENTRY_DISABLED, "test", "tester")
        with pytest.raises(klt.KISLiveTradingError, match="ENTRY_OFF"):
            klt.run_live_buy_entry_cycle(broker=_FakeBroker(), live_rollout=_rollout(), now=NOW)

    def test_commit_mismatch_raises(self, monkeypatch):
        _patch_common(monkeypatch)
        monkeypatch.setenv("DEPLOYED_COMMIT", "different")
        with pytest.raises(klt.KISLiveTradingError, match="commit"):
            klt.run_live_buy_entry_cycle(broker=_FakeBroker(), live_rollout=_rollout(), now=NOW)

    def test_missing_allowed_account_raises(self, monkeypatch):
        _patch_common(monkeypatch)
        monkeypatch.delenv("KIS_ALLOWED_ACCOUNT_NO", raising=False)
        with pytest.raises(klt.KISLiveTradingError, match="KIS_ALLOWED_ACCOUNT_NO"):
            klt.run_live_buy_entry_cycle(broker=_FakeBroker(), live_rollout=_rollout(), now=NOW)


class TestPerSymbolOutcomes:
    def test_success(self, monkeypatch):
        _patch_common(monkeypatch)
        broker = _FakeBroker()
        results = klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(), now=NOW)
        assert results["submitted"] == ["AAPL"]
        assert len(broker.submit_calls) == 1

    def test_symbol_not_on_allow_list_skipped(self, monkeypatch):
        _patch_common(monkeypatch, tickers=("MSFT",))
        broker = _FakeBroker()
        results = klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(), now=NOW)
        assert results["submitted"] == []
        assert any(s == "MSFT" for s, _ in results["skipped"])
        assert broker.submit_calls == []

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

    def test_insufficient_cash_blocked(self, monkeypatch):
        _patch_common(monkeypatch)
        broker = _FakeBroker(cash_usd=1.0)  # not enough for 1 share at $100
        results = klt.run_live_buy_entry_cycle(broker=broker, live_rollout=_rollout(), now=NOW)
        assert results["submitted"] == []
        assert broker.submit_calls == []

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
