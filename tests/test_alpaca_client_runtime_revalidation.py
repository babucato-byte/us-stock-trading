"""CODEX-018: AlpacaBroker._request() must re-validate the environment
immediately before every HTTP call, not just trust the BrokerConfig snapshot
captured when the broker (or its .config) was constructed.

BrokerConfig()/BrokerConfig.from_env() already re-read os.environ on every
call (see test_broker_config_env.py), but self.config on an AlpacaBroker
instance is a single frozen snapshot that never changes on its own. Without a
fresh check at request time, TRADING_MODE/ALPACA_*_BASE_URL/etc. can flip to
an unsafe combination between broker construction and the moment a request is
actually sent, and the stale snapshot would never notice.
"""

import pytest

from broker import AlpacaBroker, BrokerConfig

_BROKER_ENV_KEYS = (
    "TRADING_MODE",
    "ENABLE_REAL_TRADING",
    "LIVE_DRY_RUN",
    "ALPACA_PAPER_BASE_URL",
    "ALPACA_LIVE_BASE_URL",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
)


def _clear_broker_env(monkeypatch):
    for key in _BROKER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


class RecordingSession:
    """Session double whose .request() is spied on: records every call and
    returns a canned 200 response, so tests can assert the call count without
    touching the network."""

    class _Response:
        status_code = 200
        text = "OK"

        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    def __init__(self):
        self.requests = []

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))
        return self._Response()


@pytest.fixture(autouse=True)
def _isolate_broker_env(monkeypatch):
    _clear_broker_env(monkeypatch)
    yield


def test_env_flipped_to_bad_live_endpoint_after_construction_blocks_request(monkeypatch):
    # Safe at construction time: paper mode, official paper endpoint.
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    config = BrokerConfig.from_env()
    assert config.validate_order_allowed() is True

    session = RecordingSession()
    broker = AlpacaBroker(config=config, session=session)

    # Environment flips to a dangerous combination between construction and
    # the actual request -- broker.config itself is untouched.
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("ALPACA_LIVE_BASE_URL", "https://not-the-real-alpaca-host.example.com")
    monkeypatch.setenv("ENABLE_REAL_TRADING", "true")
    monkeypatch.setenv("LIVE_DRY_RUN", "false")

    with pytest.raises(RuntimeError):
        broker.get_account()

    assert session.requests == []


def test_paper_mode_with_correct_endpoint_proceeds_normally(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    config = BrokerConfig.from_env()

    session = RecordingSession()
    broker = AlpacaBroker(config=config, session=session)

    result = broker.get_account()

    assert result == {"ok": True}
    assert len(session.requests) == 1


def test_live_mode_with_arbitrary_endpoint_blocks_request(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("ALPACA_LIVE_BASE_URL", "https://some-arbitrary-host.example.com")
    monkeypatch.setenv("ENABLE_REAL_TRADING", "true")
    monkeypatch.setenv("LIVE_DRY_RUN", "false")
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    config = BrokerConfig.from_env()

    session = RecordingSession()
    broker = AlpacaBroker(config=config, session=session)

    with pytest.raises(RuntimeError):
        broker.get_account()

    assert session.requests == []


def test_get_account_blocked_when_env_flips_before_request(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    config = BrokerConfig.from_env()

    session = RecordingSession()
    broker = AlpacaBroker(config=config, session=session)

    monkeypatch.setenv("ALPACA_PAPER_BASE_URL", "https://api.alpaca.markets")

    with pytest.raises(RuntimeError, match="not the official Paper endpoint"):
        broker.get_account()

    assert session.requests == []


def test_get_positions_blocked_when_env_flips_before_request(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    config = BrokerConfig.from_env()

    session = RecordingSession()
    broker = AlpacaBroker(config=config, session=session)

    monkeypatch.setenv("ALPACA_PAPER_BASE_URL", "https://api.alpaca.markets")

    with pytest.raises(RuntimeError, match="not the official Paper endpoint"):
        broker.get_positions()

    assert session.requests == []


def test_get_positions_proceeds_normally_when_env_stays_safe(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    config = BrokerConfig.from_env()

    session = RecordingSession()
    broker = AlpacaBroker(config=config, session=session)

    result = broker.get_positions()

    assert result == {"ok": True}
    assert len(session.requests) == 1


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_submit_order_payload_preserves_explicit_side(monkeypatch, side):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    session = RecordingSession()
    broker = AlpacaBroker(config=BrokerConfig.from_env(), session=session)

    response = broker.submit_order("AAPL", qty=2, side=side)

    assert response.status_code == 200
    assert len(session.requests) == 1
    args, kwargs = session.requests[0]
    assert args[0] == "POST"
    assert kwargs["json"]["side"] == side


def test_submit_order_requires_explicit_side(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    session = RecordingSession()
    broker = AlpacaBroker(config=BrokerConfig.from_env(), session=session)

    with pytest.raises(TypeError):
        broker.submit_order("AAPL")

    assert session.requests == []


@pytest.mark.parametrize("side", [None, "", "BUY", "SELL", " buy", "sell ", "hold", 1])
def test_submit_order_rejects_invalid_side_before_http(monkeypatch, side):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    session = RecordingSession()
    broker = AlpacaBroker(config=BrokerConfig.from_env(), session=session)

    with pytest.raises(ValueError, match="exactly 'buy' or 'sell'"):
        broker.submit_order("AAPL", side=side)

    assert session.requests == []


def test_env_flip_blocks_order_post(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    session = RecordingSession()
    broker = AlpacaBroker(config=BrokerConfig.from_env(), session=session)

    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("ENABLE_REAL_TRADING", "true")
    monkeypatch.setenv("LIVE_DRY_RUN", "false")

    with pytest.raises(RuntimeError):
        broker.submit_order("AAPL", side="buy")

    assert session.requests == []


def test_config_replacement_blocks_order_post(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    session = RecordingSession()
    broker = AlpacaBroker(config=BrokerConfig.from_env(), session=session)
    broker.config = BrokerConfig(
        trading_mode="paper",
        paper_base_url="https://api.alpaca.markets",
        api_key="key",
        secret_key="secret",
    )

    with pytest.raises(RuntimeError):
        broker.submit_order("AAPL", side="buy")

    assert session.requests == []


def test_env_flip_blocks_reconciliation_lookup(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    session = RecordingSession()
    broker = AlpacaBroker(config=BrokerConfig.from_env(), session=session)

    monkeypatch.setenv("ALPACA_PAPER_BASE_URL", "https://api.alpaca.markets")

    with pytest.raises(RuntimeError):
        broker.get_order_by_client_order_id("cid-1")

    assert session.requests == []


def test_config_replacement_blocks_reconciliation_lookup(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    session = RecordingSession()
    broker = AlpacaBroker(config=BrokerConfig.from_env(), session=session)
    broker.config = BrokerConfig(
        trading_mode="papre",
        api_key="key",
        secret_key="secret",
    )

    with pytest.raises(RuntimeError):
        broker.get_order_by_client_order_id("cid-1")

    assert session.requests == []


def test_cancel_delete_uses_common_runtime_gate(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    session = RecordingSession()
    broker = AlpacaBroker(config=BrokerConfig.from_env(), session=session)

    result = broker.cancel_order("order-1")

    assert result == {"ok": True}
    assert len(session.requests) == 1
    assert session.requests[0][0][0] == "DELETE"


def test_env_flip_blocks_cancel_delete(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    session = RecordingSession()
    broker = AlpacaBroker(config=BrokerConfig.from_env(), session=session)
    monkeypatch.setenv("TRADING_MODE", "invalid")

    with pytest.raises(RuntimeError):
        broker.cancel_order("order-1")

    assert session.requests == []
