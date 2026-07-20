import pytest

from broker import AlpacaBroker, BrokerConfig


class DummySession:
    def __init__(self):
        self.posts = []

    def post(self, *args, **kwargs):
        self.posts.append((args, kwargs))
        raise AssertionError("Network order should not be submitted")


def test_live_default_blocked():
    config = BrokerConfig(trading_mode="live", enable_real_trading=False, live_dry_run=True)
    assert not config.can_submit_live_order
    assert config.status_label == "LIVE_DRY_RUN"


def test_live_dry_run_order_not_submitted():
    session = DummySession()
    broker = AlpacaBroker(
        config=BrokerConfig(
            trading_mode="live",
            enable_real_trading=False,
            live_dry_run=True,
            api_key="key",
            secret_key="secret",
        ),
        session=session,
    )
    response = broker.submit_order("AAPL", qty=1)
    assert response.dry_run is True
    assert session.posts == []


def test_live_real_order_disabled_even_with_flags():
    broker = AlpacaBroker(
        config=BrokerConfig(
            trading_mode="live",
            enable_real_trading=True,
            live_dry_run=False,
            api_key="key",
            secret_key="secret",
        ),
        session=DummySession(),
    )
    try:
        broker.submit_order("AAPL", qty=1)
    except RuntimeError as exc:
        assert "Real live trading is disabled" in str(exc)
    else:
        raise AssertionError("Real live order should be disabled in this PR")


def test_paper_mode_uses_paper_base_url():
    config = BrokerConfig(trading_mode="paper", api_key="key", secret_key="secret")
    assert config.is_paper_mode
    assert config.base_url == "https://paper-api.alpaca.markets"


class DummyGetSession:
    """Session double whose .request() call is asserted never to happen."""

    def __init__(self):
        self.requests = []

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))
        raise AssertionError("Network GET should not be issued")


class RecordingGetSession:
    """Session double that returns a canned 200 response and records calls."""

    class _Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"equity": "10000", "last_equity": "10000"}

    def __init__(self):
        self.requests = []

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))
        return self._Response()


# CODEX-001: account/position/order GET calls must be blocked by the same
# safety gate as order submission, before any network access — not just at
# the submit_order() entry point.

def test_invalid_mode_blocks_account_get():
    config = BrokerConfig(trading_mode="papre", api_key="key", secret_key="secret")
    session = DummyGetSession()
    broker = AlpacaBroker(config=config, session=session)

    with pytest.raises(RuntimeError):
        broker.get_account()

    assert session.requests == []


def test_live_endpoint_blocks_account_get():
    config = BrokerConfig(
        trading_mode="live",
        enable_real_trading=True,
        live_dry_run=False,
        api_key="key",
        secret_key="secret",
    )
    session = DummyGetSession()
    broker = AlpacaBroker(config=config, session=session)

    with pytest.raises(RuntimeError):
        broker.get_account()

    assert session.requests == []


def test_live_endpoint_blocks_positions_get():
    config = BrokerConfig(
        trading_mode="live",
        enable_real_trading=True,
        live_dry_run=False,
        api_key="key",
        secret_key="secret",
    )
    session = DummyGetSession()
    broker = AlpacaBroker(config=config, session=session)

    with pytest.raises(RuntimeError):
        broker.get_positions()

    assert session.requests == []


def test_live_endpoint_blocks_order_post():
    config = BrokerConfig(
        trading_mode="live",
        enable_real_trading=True,
        live_dry_run=False,
        api_key="key",
        secret_key="secret",
    )
    broker = AlpacaBroker(config=config, session=DummySession())

    with pytest.raises(RuntimeError):
        broker.submit_order("AAPL", qty=1)


def test_endpoint_tampering_after_construction_blocks_all_calls():
    good_config = BrokerConfig(trading_mode="paper", api_key="key", secret_key="secret")
    broker = AlpacaBroker(config=good_config, session=DummyGetSession())

    # Reassigning .config simulates the base URL being tampered with after
    # the client object already exists; every call re-validates self.config
    # rather than trusting a value captured once at construction time.
    broker.config = BrokerConfig(
        trading_mode="paper",
        paper_base_url="https://api.alpaca.markets",
        api_key="key",
        secret_key="secret",
    )

    with pytest.raises(RuntimeError):
        broker.get_account()
    with pytest.raises(RuntimeError):
        broker.get_positions()
    with pytest.raises(RuntimeError):
        broker.submit_order("AAPL", qty=1)


def test_paper_endpoint_allows_mock_account_and_positions_calls():
    config = BrokerConfig(trading_mode="paper", api_key="key", secret_key="secret")
    session = RecordingGetSession()
    broker = AlpacaBroker(config=config, session=session)

    account = broker.get_account()

    assert account == {"equity": "10000", "last_equity": "10000"}
    assert len(session.requests) == 1
