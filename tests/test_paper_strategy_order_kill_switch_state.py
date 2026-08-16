"""CODEX-016: paper_strategy_order.submit_order() must be gated by both the
binary kill_switch.is_trading_halted() check (unchanged) and the multi-level
kill_switch_state state machine (is_entry_allowed() for buy, is_liquidation_
allowed() for sell), re-checked fresh on every call. Covers the direct
submit_order() path and the main() integration path. No real network calls;
FakeBroker is a pure in-memory spy, and every state/history file lives under
tmp_path.
"""

import pytest

import kill_switch_state as kss
import paper_strategy_order as pso


TODAY = pso.eastern_now().strftime("%Y-%m-%d")


class _FakeConfig:
    status_label = "PAPER"


class FakeBroker:
    """Minimal broker double: records call count/args, never touches the network."""

    def __init__(self):
        self.config = _FakeConfig()
        self.submit_calls = []

    def submit_order(self, symbol, qty=1, *, side, client_order_id=None):
        self.submit_calls.append((symbol, qty, side, client_order_id))
        return pso.BrokerResponse(status_code=200, text="OK", data={"status": "accepted"}, dry_run=False)

    def get_account(self):
        return {"equity": "10000", "last_equity": "10000"}

    def get_positions(self):
        return []

    def get_order_by_client_order_id(self, client_order_id):
        return None


def _isolate_kill_switches(monkeypatch, tmp_path, state_file_name="KILL_SWITCH_STATE.json"):
    """Point both kill switch mechanisms at tmp_path so the real repository's
    KILL_SWITCH file / KILL_SWITCH_STATE.json can never leak into a test.
    """
    monkeypatch.delenv("TRADING_HALTED", raising=False)
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "no_such_kill_switch_file"))
    state_path = tmp_path / state_file_name
    monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(state_path))
    return state_path


def _high_score_result(symbol):
    return {
        "symbol": symbol,
        "price": 100.0,
        "ma200": 90.0,
        "rsi": 50.0,
        "volume_ratio": 1.5,
        "score": 100,
    }


def _patch_main_environment(monkeypatch, tmp_path, tickers, broker):
    monkeypatch.setattr(pso, "load_watchlist", lambda: tickers)
    monkeypatch.setattr(pso, "analyze_stock", _high_score_result)
    monkeypatch.setattr(pso, "get_us_market_session", lambda: "regular")
    monkeypatch.setattr(pso, "ORDER_HISTORY_FILE", tmp_path / "order_history.csv")
    monkeypatch.setattr(pso, "ORDER_HISTORY_LOCK_FILE", tmp_path / "order_history.lock")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_FILE", tmp_path / "order_reconciliation.csv")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_LOCK_FILE", tmp_path / "order_reconciliation.lock")
    pso.initialize_order_history()
    monkeypatch.setattr(pso, "send_slack_alert", lambda msg: True)


# ---------------------------------------------------------------------------
# submit_order(): direct-call gating by kill_switch_state
# ---------------------------------------------------------------------------

def test_active_state_allows_buy_order(monkeypatch, tmp_path):
    _isolate_kill_switches(monkeypatch, tmp_path)
    broker = FakeBroker()

    response = pso.submit_order("AAPL", qty=1, broker=broker, side="buy")

    assert broker.submit_calls == [("AAPL", 1, "buy", None)]
    assert response.status_code == 200


def test_active_state_allows_sell_order(monkeypatch, tmp_path):
    _isolate_kill_switches(monkeypatch, tmp_path)
    broker = FakeBroker()

    response = pso.submit_order("AAPL", qty=1, broker=broker, side="sell")

    assert broker.submit_calls == [("AAPL", 1, "sell", None)]
    assert response.status_code == 200


def test_wrapper_requires_explicit_side(monkeypatch, tmp_path):
    _isolate_kill_switches(monkeypatch, tmp_path)
    broker = FakeBroker()

    with pytest.raises(TypeError):
        pso.submit_order("AAPL", qty=1, broker=broker)

    assert broker.submit_calls == []


@pytest.mark.parametrize("side", [None, "", "BUY", "SELL", " buy", "sell ", "hold", 1])
def test_wrapper_rejects_ambiguous_side_before_broker_call(monkeypatch, tmp_path, side):
    _isolate_kill_switches(monkeypatch, tmp_path)
    broker = FakeBroker()

    with pytest.raises(ValueError, match="exactly 'buy' or 'sell'"):
        pso.submit_order("AAPL", qty=1, broker=broker, side=side)

    assert broker.submit_calls == []


def test_entry_disabled_blocks_buy_order(monkeypatch, tmp_path):
    _isolate_kill_switches(monkeypatch, tmp_path)
    kss.activate(kss.ENTRY_DISABLED, reason="incident", activated_by="ops1")
    broker = FakeBroker()

    response = pso.submit_order("AAPL", qty=1, broker=broker, side="buy")

    assert broker.submit_calls == []
    assert response.status_code == 423


def test_entry_disabled_allows_liquidation_sell_order(monkeypatch, tmp_path):
    _isolate_kill_switches(monkeypatch, tmp_path)
    kss.activate(kss.ENTRY_DISABLED, reason="incident", activated_by="ops1")
    broker = FakeBroker()

    response = pso.submit_order("AAPL", qty=1, broker=broker, side="sell")

    assert broker.submit_calls == [("AAPL", 1, "sell", None)]
    assert response.status_code == 200


@pytest.mark.parametrize("state", [kss.ALL_TRADING_DISABLED, kss.MANUAL_REVIEW])
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_all_trading_disabled_and_manual_review_block_both_sides(monkeypatch, tmp_path, state, side):
    _isolate_kill_switches(monkeypatch, tmp_path)
    kss.activate(state, reason="incident", activated_by="ops1")
    broker = FakeBroker()

    response = pso.submit_order("AAPL", qty=1, broker=broker, side=side)

    assert broker.submit_calls == []
    assert response.status_code == 423


def test_corrupted_state_file_blocks_order_fail_closed(monkeypatch, tmp_path):
    state_path = _isolate_kill_switches(monkeypatch, tmp_path)
    state_path.write_text("{ not valid json ]")
    broker = FakeBroker()

    response = pso.submit_order("AAPL", qty=1, broker=broker, side="buy")

    assert broker.submit_calls == []
    assert response.status_code == 423


def test_missing_state_file_defaults_to_active_existing_behavior(monkeypatch, tmp_path):
    _isolate_kill_switches(monkeypatch, tmp_path, state_file_name="does_not_exist.json")
    broker = FakeBroker()

    response = pso.submit_order("AAPL", qty=1, broker=broker, side="buy")

    assert broker.submit_calls == [("AAPL", 1, "buy", None)]
    assert response.status_code == 200


def test_binary_halt_still_blocks_even_when_state_is_active(monkeypatch, tmp_path):
    """The pre-existing kill_switch.is_trading_halted() gate must remain in
    effect alongside the new state-machine gate (both must pass)."""
    _isolate_kill_switches(monkeypatch, tmp_path)
    monkeypatch.setenv("TRADING_HALTED", "true")
    broker = FakeBroker()

    response = pso.submit_order("AAPL", qty=1, broker=broker, side="buy")

    assert broker.submit_calls == []
    assert response.status_code == 423


# ---------------------------------------------------------------------------
# main(): the order-call path inside the loop is gated the same way
# ---------------------------------------------------------------------------

def test_main_blocks_new_orders_when_entry_disabled(monkeypatch, tmp_path):
    _isolate_kill_switches(monkeypatch, tmp_path)
    kss.activate(kss.ENTRY_DISABLED, reason="incident", activated_by="ops1")
    broker = FakeBroker()
    _patch_main_environment(monkeypatch, tmp_path, ["AAPL"], broker)

    pso.main(broker=broker)

    assert broker.submit_calls == []


def test_main_submits_normally_when_active(monkeypatch, tmp_path):
    _isolate_kill_switches(monkeypatch, tmp_path)
    broker = FakeBroker()
    _patch_main_environment(monkeypatch, tmp_path, ["AAPL"], broker)

    pso.main(broker=broker)

    assert len(broker.submit_calls) == 1
    assert broker.submit_calls[0][0] == "AAPL"


@pytest.mark.parametrize("state", [kss.ALL_TRADING_DISABLED, kss.MANUAL_REVIEW])
def test_main_blocks_new_orders_in_all_trading_disabled_and_manual_review(monkeypatch, tmp_path, state):
    _isolate_kill_switches(monkeypatch, tmp_path)
    kss.activate(state, reason="incident", activated_by="ops1")
    broker = FakeBroker()
    _patch_main_environment(monkeypatch, tmp_path, ["AAPL"], broker)

    pso.main(broker=broker)

    assert broker.submit_calls == []
