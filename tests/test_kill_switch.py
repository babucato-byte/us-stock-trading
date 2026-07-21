import pytest

import kill_switch
import paper_strategy_order as pso


class FakeConfig:
    status_label = "PAPER"


class FakeBroker:
    """Minimal broker double: submit_order must never be reached while halted."""

    def __init__(self):
        self.config = FakeConfig()
        self.submit_calls = []

    def submit_order(self, symbol, qty=1, client_order_id=None):
        self.submit_calls.append((symbol, qty))
        raise AssertionError("submit_order must not be called while trading is halted")


def _clear_env(monkeypatch):
    monkeypatch.delenv("TRADING_HALTED", raising=False)
    monkeypatch.delenv("KILL_SWITCH_FILE", raising=False)


# ---------------------------------------------------------------------------
# kill_switch.is_trading_halted() unit behavior
# ---------------------------------------------------------------------------

def test_default_unset_allows_trading(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setattr(kill_switch, "KILL_SWITCH_FILE", tmp_path / "KILL_SWITCH")

    assert kill_switch.is_trading_halted() is False


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "tRuE"])
def test_trading_halted_env_var_blocks_case_insensitive(monkeypatch, tmp_path, value):
    _clear_env(monkeypatch)
    monkeypatch.setattr(kill_switch, "KILL_SWITCH_FILE", tmp_path / "KILL_SWITCH")
    monkeypatch.setenv("TRADING_HALTED", value)

    assert kill_switch.is_trading_halted() is True


def test_trading_halted_env_var_false_allows_trading(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setattr(kill_switch, "KILL_SWITCH_FILE", tmp_path / "KILL_SWITCH")
    monkeypatch.setenv("TRADING_HALTED", "false")

    assert kill_switch.is_trading_halted() is False


def test_kill_switch_file_presence_blocks_trading(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    kill_switch_path = tmp_path / "KILL_SWITCH"
    monkeypatch.setattr(kill_switch, "KILL_SWITCH_FILE", kill_switch_path)

    assert kill_switch.is_trading_halted() is False
    kill_switch_path.write_text("halted by operator")
    assert kill_switch.is_trading_halted() is True


def test_kill_switch_file_path_overridable_via_env_var(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setattr(kill_switch, "KILL_SWITCH_FILE", tmp_path / "unused_default")
    override_path = tmp_path / "custom_kill_switch"
    monkeypatch.setenv("KILL_SWITCH_FILE", str(override_path))

    assert kill_switch.is_trading_halted() is False
    override_path.write_text("halted")
    assert kill_switch.is_trading_halted() is True


# ---------------------------------------------------------------------------
# Wiring into paper_strategy_order.main()'s order submission entry point
# ---------------------------------------------------------------------------

def test_main_does_not_submit_orders_when_halted(monkeypatch):
    monkeypatch.setattr(pso, "is_trading_halted", lambda: True)
    broker = FakeBroker()

    result = pso.main(broker=broker)

    assert broker.submit_calls == []
    assert result == {"halted": True, "submitted": 0}


def test_main_proceeds_normally_when_not_halted(monkeypatch, tmp_path):
    monkeypatch.setattr(pso, "is_trading_halted", lambda: False)
    monkeypatch.setattr(pso, "load_watchlist", lambda: [])

    # An empty watchlist short-circuits main() right after the halt check,
    # confirming the non-halted path is otherwise untouched by this change.
    assert pso.main(broker=FakeBroker()) is None


def test_submit_order_entry_point_blocks_broker_when_halted_via_env_var(monkeypatch, tmp_path):
    # Reproduces calling pso.submit_order() directly (bypassing main()) with
    # TRADING_HALTED=true set via the real env var, not a monkeypatched
    # is_trading_halted -- the order submission entry point itself must
    # refuse to reach the broker.
    _clear_env(monkeypatch)
    monkeypatch.setattr(kill_switch, "KILL_SWITCH_FILE", tmp_path / "KILL_SWITCH")
    monkeypatch.setenv("TRADING_HALTED", "true")
    broker = FakeBroker()

    response = pso.submit_order("AAPL", broker=broker)

    assert broker.submit_calls == []
    assert response.status_code == 423
