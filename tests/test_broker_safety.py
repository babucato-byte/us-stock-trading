import importlib

import pytest

from broker import AlpacaBroker, BrokerConfig
from broker import broker_config as broker_config_module

# Every environment variable BrokerConfig's dataclass fields read a default
# from. Needed because those defaults are computed once when broker_config
# is first imported, not re-read per BrokerConfig() call -- see
# reloaded_broker_config below.
_BROKER_ENV_KEYS = (
    "TRADING_MODE",
    "ENABLE_REAL_TRADING",
    "LIVE_DRY_RUN",
    "ALPACA_PAPER_BASE_URL",
    "ALPACA_LIVE_BASE_URL",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
)


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


@pytest.fixture
def reloaded_broker_config(monkeypatch):
    """Reload broker_config so its os.getenv(...)-derived dataclass field
    defaults pick up env vars set via monkeypatch during the test.

    BrokerConfig's fields (trading_mode, enable_real_trading, ..., api_key)
    are plain dataclass defaults evaluated once when the module is first
    imported, not re-read from os.environ on every BrokerConfig() call (see
    test_env_change_after_first_import_is_not_observed_without_reload).
    Tests that want to exercise the os.getenv(...) fallback / normalization
    logic itself must reload the module after monkeypatch changes the
    environment, as this fixture's callers do. The finalizer always clears
    every relevant env var and reloads once more so later tests/files that
    import broker_config observe the untouched, safe defaults regardless of
    what this test set.
    """
    yield broker_config_module
    for key in _BROKER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    importlib.reload(broker_config_module)


def test_missing_all_env_vars_falls_back_to_safe_paper_defaults(monkeypatch, reloaded_broker_config):
    for key in _BROKER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    importlib.reload(reloaded_broker_config)

    config = reloaded_broker_config.BrokerConfig()

    assert config.trading_mode == "paper"
    assert config.is_paper_mode
    assert config.enable_real_trading is False
    assert config.live_dry_run is True
    assert config.can_submit_live_order is False
    with pytest.raises(RuntimeError):
        config.validate_for_request()


def test_env_trading_mode_case_and_whitespace_normalized(monkeypatch, reloaded_broker_config):
    monkeypatch.setenv("TRADING_MODE", "  PAPER  \n")
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    importlib.reload(reloaded_broker_config)

    config = reloaded_broker_config.BrokerConfig()

    assert config.trading_mode == "paper"
    assert config.is_paper_mode
    assert config.validate_order_allowed() is True


def test_env_trading_mode_typo_blocks_order_after_reload(monkeypatch, reloaded_broker_config):
    monkeypatch.setenv("TRADING_MODE", "papre")
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    importlib.reload(reloaded_broker_config)

    config = reloaded_broker_config.BrokerConfig()

    assert not config.is_paper_mode
    assert not config.is_live_mode
    with pytest.raises(RuntimeError):
        config.validate_order_allowed()


def test_live_mode_env_with_missing_flags_falls_back_to_dry_run(monkeypatch, reloaded_broker_config):
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.delenv("ENABLE_REAL_TRADING", raising=False)
    monkeypatch.delenv("LIVE_DRY_RUN", raising=False)
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    importlib.reload(reloaded_broker_config)

    config = reloaded_broker_config.BrokerConfig()

    assert config.is_live_mode
    assert config.enable_real_trading is False
    assert config.live_dry_run is True
    assert config.can_submit_live_order is False
    assert config.status_label == "LIVE_DRY_RUN"
    with pytest.raises(RuntimeError):
        config.validate_order_allowed()


def test_env_paper_base_url_set_to_live_host_blocks_orders(monkeypatch, reloaded_broker_config):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("ALPACA_PAPER_BASE_URL", "https://api.alpaca.markets")
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    importlib.reload(reloaded_broker_config)

    config = reloaded_broker_config.BrokerConfig()

    assert config.base_url == "https://api.alpaca.markets"
    with pytest.raises(RuntimeError, match="not the official Paper endpoint"):
        config.validate_order_allowed()


def test_env_paper_base_url_trailing_slash_still_allowed(monkeypatch, reloaded_broker_config):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("ALPACA_PAPER_BASE_URL", "https://paper-api.alpaca.markets/")
    importlib.reload(reloaded_broker_config)

    config = reloaded_broker_config.BrokerConfig()

    assert config.validate_order_allowed() is True


def test_partial_credentials_missing_secret_blocks_request():
    config = BrokerConfig(trading_mode="paper", api_key="key", secret_key=None)

    with pytest.raises(RuntimeError):
        config.validate_for_request()


def test_partial_credentials_missing_api_key_blocks_request():
    config = BrokerConfig(trading_mode="paper", api_key=None, secret_key="secret")

    with pytest.raises(RuntimeError):
        config.validate_for_request()


# HUMAN_REVIEW_FINDINGS.md 2026-07-22: BrokerConfig's dataclass field
# defaults are computed once when broker_config.py is first imported, not
# re-read from os.environ per BrokerConfig() call. This pins that CURRENT
# behavior (not the desired behavior): changing TRADING_MODE /
# ENABLE_REAL_TRADING / LIVE_DRY_RUN in the environment of an already-running
# process (e.g. dashboard/app.py, which calls BrokerConfig() fresh on every
# request) has no effect until the process restarts. Do not "fix" this by
# editing broker/broker_config.py -- broker/** is out of scope for this
# task; see the findings doc for the reproduction and recommended direction.
def test_env_change_after_first_import_is_not_observed_without_reload(monkeypatch):
    baseline_mode = broker_config_module.BrokerConfig().trading_mode
    flipped_mode = "live" if baseline_mode == "paper" else "paper"

    monkeypatch.setenv("TRADING_MODE", flipped_mode)
    monkeypatch.setenv("ENABLE_REAL_TRADING", "true")
    monkeypatch.setenv("LIVE_DRY_RUN", "false")

    config = broker_config_module.BrokerConfig()

    assert config.trading_mode == baseline_mode
