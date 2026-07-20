from datetime import datetime

import pandas as pd
import pytest
import requests

import order_safety
import paper_strategy_order as pso
from broker import AlpacaBroker, BrokerConfig


TODAY = datetime.now().strftime("%Y-%m-%d")


class DummySession:
    """Stands in for requests.Session; never performs real network I/O."""

    def __init__(self):
        self.posts = []

    def post(self, *args, **kwargs):
        self.posts.append((args, kwargs))
        raise AssertionError("Network order should not be submitted")


class FakeBrokerResponse:
    def __init__(self, status_code=200, text="OK", dry_run=False):
        self.status_code = status_code
        self.text = text
        self.dry_run = dry_run


class FakeConfig:
    status_label = "PAPER"


class FakeBroker:
    """Minimal broker double: no real Alpaca/HTTP calls, fully scripted responses."""

    def __init__(self, account=None, positions=None, submit_side_effects=None,
                 default_response=None):
        self.config = FakeConfig()
        self._account = account or {"equity": "10000", "last_equity": "10000"}
        self._positions = positions or []
        self._submit_side_effects = submit_side_effects or {}
        self._default_response = default_response or FakeBrokerResponse(
            status_code=200, text="OK", dry_run=False
        )
        self.submit_calls = []

    def get_account(self):
        return self._account

    def get_positions(self):
        return self._positions

    def submit_order(self, symbol, qty=1):
        self.submit_calls.append((symbol, qty))
        effect = self._submit_side_effects.get(symbol)
        if isinstance(effect, Exception):
            raise effect
        return effect or self._default_response


def _high_score_result(symbol):
    return {
        "symbol": symbol,
        "price": 100.0,
        "ma200": 90.0,
        "rsi": 50.0,
        "volume_ratio": 1.5,
        "score": 100,
    }


def _patch_common(monkeypatch, tmp_path, tickers, broker, market_session="regular"):
    monkeypatch.setattr(pso, "load_watchlist", lambda: tickers)
    monkeypatch.setattr(pso, "analyze_stock", _high_score_result)
    monkeypatch.setattr(pso, "get_us_market_session", lambda: market_session)
    monkeypatch.setattr(pso, "ORDER_HISTORY_FILE", tmp_path / "order_history.csv")
    slack_calls = []
    monkeypatch.setattr(pso, "send_slack_alert", lambda msg: slack_calls.append(msg) or True)
    return slack_calls


# ---------------------------------------------------------------------------
# Happy path (scenarios 1-3)
# ---------------------------------------------------------------------------

def test_valid_candidate_submits_order_once(monkeypatch, tmp_path):
    broker = FakeBroker()
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    pso.main(broker=broker)

    assert broker.submit_calls == [("AAPL", 1)]


def test_successful_order_is_persisted_to_history(monkeypatch, tmp_path):
    broker = FakeBroker()
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    pso.main(broker=broker)

    history = pd.read_csv(tmp_path / "order_history.csv")
    assert (history["symbol"] == "AAPL").any()
    assert (history["order_date"] == TODAY).any()


def test_successful_order_triggers_slack_notification(monkeypatch, tmp_path):
    broker = FakeBroker()
    slack_calls = _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    pso.main(broker=broker)

    assert any("Paper Strategy Order" in msg and "SUBMITTED" in msg for msg in slack_calls)


# ---------------------------------------------------------------------------
# Safety blocks (scenarios 4-11)
# ---------------------------------------------------------------------------

def test_live_url_order_blocked():
    config = BrokerConfig(
        trading_mode="live",
        enable_real_trading=True,
        live_dry_run=False,
        api_key="key",
        secret_key="secret",
    )
    assert config.base_url == "https://api.alpaca.markets"
    broker = AlpacaBroker(config=config, session=DummySession())

    with pytest.raises(RuntimeError):
        broker.submit_order("AAPL", qty=1)


def test_non_paper_mode_blocked_by_trading_mode_check(monkeypatch):
    class FakeLiveConfig:
        is_live_mode = True
        can_submit_live_order = True

    monkeypatch.setattr(order_safety, "BrokerConfig", lambda: FakeLiveConfig())

    with pytest.raises(Exception):
        order_safety.check_trading_mode()


def test_paper_mode_passes_trading_mode_check(monkeypatch):
    class FakePaperConfig:
        is_live_mode = False
        can_submit_live_order = False

    monkeypatch.setattr(order_safety, "BrokerConfig", lambda: FakePaperConfig())

    assert order_safety.check_trading_mode() is True


def test_duplicate_order_blocks_resubmission(monkeypatch, tmp_path):
    history_file = tmp_path / "order_history.csv"
    pd.DataFrame([{"symbol": "AAPL", "order_date": TODAY, "mode": "PAPER", "dry_run": False}]).to_csv(
        history_file, index=False
    )

    broker = FakeBroker()
    slack_calls = _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    pso.main(broker=broker)

    assert broker.submit_calls == []
    assert any("Duplicate order prevented" in msg for msg in slack_calls)


def test_held_position_blocks_rebuy(monkeypatch, tmp_path):
    broker = FakeBroker(positions=[{"symbol": "AAPL"}])
    slack_calls = _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    pso.main(broker=broker)

    assert broker.submit_calls == []
    assert any("Already held" in msg for msg in slack_calls)


def test_daily_trade_count_limit_blocks_order(monkeypatch, tmp_path):
    monkeypatch.setattr(order_safety, "MAX_TRADES_PER_DAY", 0)
    broker = FakeBroker()
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    with pytest.raises(Exception):
        pso.main(broker=broker)

    assert broker.submit_calls == []


def test_daily_loss_limit_blocks_all_orders(monkeypatch, tmp_path):
    broker = FakeBroker(account={"equity": "9700", "last_equity": "10000"})
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    with pytest.raises(Exception):
        pso.main(broker=broker)

    assert broker.submit_calls == []


def test_outside_regular_session_orders_not_submitted(monkeypatch, tmp_path):
    broker = FakeBroker()
    slack_calls = _patch_common(monkeypatch, tmp_path, ["AAPL"], broker, market_session="premarket")

    pso.main(broker=broker)

    assert broker.submit_calls == []
    assert any("Order review only" in msg for msg in slack_calls)


def test_position_size_over_limit_is_blocked():
    with pytest.raises(Exception):
        order_safety.check_position_size(order_safety.MAX_POSITION_RATE + 0.5)


# ---------------------------------------------------------------------------
# External failure handling (scenarios 12-15)
# ---------------------------------------------------------------------------

def test_broker_timeout_is_handled_safely_and_next_symbol_continues(monkeypatch, tmp_path):
    broker = FakeBroker(
        submit_side_effects={"AAPL": requests.exceptions.Timeout("timed out")}
    )
    slack_calls = _patch_common(monkeypatch, tmp_path, ["AAPL", "MSFT"], broker)

    pso.main(broker=broker)  # must not raise

    assert broker.submit_calls == [("AAPL", 1), ("MSFT", 1)]
    history = pd.read_csv(tmp_path / "order_history.csv")
    assert not (history["symbol"] == "AAPL").any()
    assert (history["symbol"] == "MSFT").any()
    assert any("Order failed" in msg and "AAPL" in msg for msg in slack_calls)


def test_rejected_response_is_not_recorded_as_success(monkeypatch, tmp_path):
    broker = FakeBroker(
        submit_side_effects={"AAPL": FakeBrokerResponse(status_code=422, text="rejected", dry_run=False)}
    )
    slack_calls = _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    pso.main(broker=broker)

    assert not (tmp_path / "order_history.csv").exists()
    assert any("FAILED" in msg for msg in slack_calls)


def test_order_history_save_failure_is_logged_not_raised(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(pso, "ORDER_HISTORY_FILE", tmp_path / "missing_dir" / "order_history.csv")

    result = pso.save_order_history(pd.DataFrame([{"symbol": "AAPL", "order_date": TODAY}]))

    assert result is False
    assert "Failed to save order history" in capsys.readouterr().out


def test_slack_failure_does_not_prevent_history_save(monkeypatch, tmp_path):
    broker = FakeBroker()
    monkeypatch.setattr(pso, "load_watchlist", lambda: ["AAPL"])
    monkeypatch.setattr(pso, "analyze_stock", _high_score_result)
    monkeypatch.setattr(pso, "get_us_market_session", lambda: "regular")
    monkeypatch.setattr(pso, "ORDER_HISTORY_FILE", tmp_path / "order_history.csv")

    def _raise_slack(message):
        raise requests.exceptions.ConnectionError("slack unreachable")

    monkeypatch.setattr(pso, "send_slack_alert", _raise_slack)

    pso.main(broker=broker)  # must not raise despite Slack failing

    history = pd.read_csv(tmp_path / "order_history.csv")
    assert (history["symbol"] == "AAPL").any()
