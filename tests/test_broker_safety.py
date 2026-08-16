import importlib
from datetime import datetime, timezone

import pytest

from broker import AlpacaBroker, BrokerConfig
from broker import broker_config as broker_config_module
from live_readiness.order_gateway import LiveEntryContext


@pytest.fixture(autouse=True)
def _isolate_live_entry_state_db(tmp_path, monkeypatch):
    # CODEX-031: the live-entry gate now reads/writes an authoritative
    # SQLite ledger (live_readiness/entry_reservation_ledger.py) -- tests
    # that exercise it via _live_entry_context() below must isolate that
    # database exactly like every other SQLite-touching test file, or
    # they silently accumulate real reservations in the repo-root
    # TRADING_STATE.db.
    from live_readiness import entry_reservation_ledger as live_ledger
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setattr(live_ledger, "_LOCK_FILE", tmp_path / "LIVE_ENTRY_RESERVATION.lock")
    yield


def _live_entry_context(symbol="AAPL"):
    """CODEX-026/029: AlpacaBroker.submit_order() now requires a valid
    LiveEntryContext for any side="buy" call on a live-mode config, before
    it ever reaches the pre-existing dry-run/hard-disable checks these
    tests exercise. A minimal, fully-valid context that passes the
    CODEX-026/029 gate lets those older checks still run exactly as
    before -- this fixture does not weaken or bypass anything, it just
    supplies the input the newer gate now also requires."""
    now = datetime.now(timezone.utc)
    return LiveEntryContext(
        symbol=symbol, expected_fill_price_usd=10.0, allow_list=[symbol],
        available_cash_krw=30_000, cash_usage_percent=100, cash_as_of=now.isoformat(),
        fx_rate_krw_per_usd=1_350.0, fx_rate_as_of=now.isoformat(),
        max_order_notional_krw=30_000, max_daily_loss_krw=10_000, max_position_count=1,
        max_daily_entries=2, now=now,
    )

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
    response = broker.submit_order("AAPL", qty=1, side="buy", live_entry_context=_live_entry_context())
    assert response.dry_run is True
    assert session.posts == []


def test_live_real_order_disabled_even_with_flags():
    # KIS migration: this test targets validate_order_allowed()'s own
    # "live real trading disabled in this pre-live PR" block
    # specifically -- authorize Alpaca live orders so the check under
    # test is reached, not CODEX-042's earlier execution_broker gate.
    broker = AlpacaBroker(
        config=BrokerConfig(
            trading_mode="live",
            enable_real_trading=True,
            live_dry_run=False,
            api_key="key",
            secret_key="secret",
            alpaca_order_enabled=True,
            execution_broker="alpaca",
        ),
        session=DummySession(),
    )
    try:
        broker.submit_order("AAPL", qty=1, side="buy", live_entry_context=_live_entry_context())
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
        broker.submit_order("AAPL", qty=1, side="buy", live_entry_context=_live_entry_context())


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
        broker.submit_order("AAPL", qty=1, side="buy", live_entry_context=_live_entry_context())


def test_paper_endpoint_allows_mock_account_and_positions_calls(monkeypatch):
    # CODEX-018: the common gate now re-reads current environment
    # credentials on every request and requires them to match self.config's
    # captured values, so this success-path test must set matching env vars.
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
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


# HUMAN_REVIEW_FINDINGS.md 2026-07-22: BrokerConfig's dataclass fields used
# to be plain os.getenv(...)-derived defaults, computed once when
# broker_config.py was first imported, so a running process never observed
# env changes without a restart/reload. That gap is now fixed: fields use a
# default_factory that re-reads os.environ on every BrokerConfig() call, so
# a fresh instance in a long-running process (e.g. dashboard/app.py) always
# reflects the current environment without needing importlib.reload(). This
# test now pins that fixed behavior instead of the old bug.
def test_env_change_after_first_import_is_observed_without_reload(monkeypatch):
    baseline_mode = broker_config_module.BrokerConfig().trading_mode
    flipped_mode = "live" if baseline_mode == "paper" else "paper"

    monkeypatch.setenv("TRADING_MODE", flipped_mode)
    monkeypatch.setenv("ENABLE_REAL_TRADING", "true")
    monkeypatch.setenv("LIVE_DRY_RUN", "false")

    config = broker_config_module.BrokerConfig()

    assert config.trading_mode == flipped_mode
