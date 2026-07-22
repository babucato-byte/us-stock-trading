import dataclasses

import pytest

from broker import broker_config as broker_config_module
from broker.broker_config import BrokerConfig, validate_order_allowed_now

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


# (a) A from_env() call made after monkeypatch.setenv reflects the new value,
# with no importlib.reload anywhere in this file.
def test_from_env_reflects_env_change_after_import(monkeypatch):
    _clear_broker_env(monkeypatch)
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("ENABLE_REAL_TRADING", "true")
    monkeypatch.setenv("LIVE_DRY_RUN", "false")

    config = BrokerConfig.from_env()

    assert config.trading_mode == "live"
    assert config.enable_real_trading is True
    assert config.live_dry_run is False
    assert config.can_submit_live_order is True


# (b) An object built before the env change is unaffected, and the frozen
# dataclass still refuses mutation.
def test_previously_created_config_is_unaffected_and_frozen(monkeypatch):
    _clear_broker_env(monkeypatch)
    monkeypatch.setenv("TRADING_MODE", "paper")

    original = BrokerConfig.from_env()
    assert original.trading_mode == "paper"

    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("ENABLE_REAL_TRADING", "true")
    monkeypatch.setenv("LIVE_DRY_RUN", "false")

    # Object created before the env change is untouched.
    assert original.trading_mode == "paper"
    assert original.enable_real_trading is False
    assert original.live_dry_run is True

    with pytest.raises(dataclasses.FrozenInstanceError):
        original.trading_mode = "live"

    # A fresh read after the change does observe it.
    updated = BrokerConfig.from_env()
    assert updated.trading_mode == "live"


# (c) Running the same validation twice, in opposite env orderings, gives the
# same result each time -- no cross-call environment pollution.
def test_repeated_validation_in_different_orders_is_consistent(monkeypatch):
    def paper_then_live():
        _clear_broker_env(monkeypatch)
        monkeypatch.setenv("TRADING_MODE", "paper")
        monkeypatch.setenv("ALPACA_API_KEY", "key")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
        paper_result = BrokerConfig.from_env().validate_order_allowed()

        _clear_broker_env(monkeypatch)
        monkeypatch.setenv("TRADING_MODE", "live")
        monkeypatch.setenv("ENABLE_REAL_TRADING", "true")
        monkeypatch.setenv("LIVE_DRY_RUN", "false")
        live_config = BrokerConfig.from_env()
        live_error = None
        try:
            live_config.validate_order_allowed()
        except RuntimeError as exc:
            live_error = str(exc)
        return paper_result, live_error

    def live_then_paper():
        _clear_broker_env(monkeypatch)
        monkeypatch.setenv("TRADING_MODE", "live")
        monkeypatch.setenv("ENABLE_REAL_TRADING", "true")
        monkeypatch.setenv("LIVE_DRY_RUN", "false")
        live_config = BrokerConfig.from_env()
        live_error = None
        try:
            live_config.validate_order_allowed()
        except RuntimeError as exc:
            live_error = str(exc)

        _clear_broker_env(monkeypatch)
        monkeypatch.setenv("TRADING_MODE", "paper")
        monkeypatch.setenv("ALPACA_API_KEY", "key")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
        paper_result = BrokerConfig.from_env().validate_order_allowed()

        return paper_result, live_error

    first_paper, first_live_error = paper_then_live()
    second_paper, second_live_error = live_then_paper()

    assert first_paper == second_paper is True
    assert first_live_error == second_live_error
    assert "Real live trading is disabled" in first_live_error


# (d) With no relevant env vars set at all, from_env() falls back to the
# paper-safe defaults.
def test_from_env_with_no_env_vars_falls_back_to_paper_defaults(monkeypatch):
    _clear_broker_env(monkeypatch)

    config = BrokerConfig.from_env()

    assert config.trading_mode == "paper"
    assert config.is_paper_mode
    assert config.enable_real_trading is False
    assert config.live_dry_run is True
    assert config.can_submit_live_order is False

    # Bare BrokerConfig() (used throughout broker/**, dashboard/app.py,
    # order_safety.py, slack_report.py) must be equally safe by default.
    bare = BrokerConfig()
    assert bare.trading_mode == "paper"
    assert bare.enable_real_trading is False
    assert bare.live_dry_run is True


# (e) An invalid TRADING_MODE value is blocked, not silently accepted as
# either paper or live.
def test_invalid_trading_mode_value_is_blocked(monkeypatch):
    _clear_broker_env(monkeypatch)
    monkeypatch.setenv("TRADING_MODE", "not-a-real-mode")
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")

    config = BrokerConfig.from_env()

    assert not config.is_paper_mode
    assert not config.is_live_mode
    with pytest.raises(RuntimeError):
        config.validate_order_allowed()


# (f) Only the live endpoint is configured, with no safety flags set --
# orders must still be blocked.
def test_live_base_url_alone_without_safety_flags_blocks_orders(monkeypatch):
    _clear_broker_env(monkeypatch)
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("ALPACA_LIVE_BASE_URL", "https://api.alpaca.markets")
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    # ENABLE_REAL_TRADING / LIVE_DRY_RUN deliberately left unset.

    config = BrokerConfig.from_env()

    assert config.is_live_mode
    assert config.enable_real_trading is False
    assert config.live_dry_run is True
    assert config.can_submit_live_order is False
    with pytest.raises(RuntimeError):
        config.validate_order_allowed()


# (g) Config can be re-created straight from a fresh env read, with no
# importlib.reload anywhere in this module (see module-level absence of the
# import and grep-checked by the task's pass criteria).
def test_from_env_can_be_recreated_repeatedly_without_reload(monkeypatch):
    _clear_broker_env(monkeypatch)
    monkeypatch.setenv("TRADING_MODE", "paper")
    first = BrokerConfig.from_env()
    assert first.trading_mode == "paper"

    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("ENABLE_REAL_TRADING", "true")
    monkeypatch.setenv("LIVE_DRY_RUN", "false")
    second = BrokerConfig.from_env()
    assert second.trading_mode == "live"
    assert second.can_submit_live_order is True

    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.delenv("ENABLE_REAL_TRADING", raising=False)
    monkeypatch.delenv("LIVE_DRY_RUN", raising=False)
    third = BrokerConfig.from_env()
    assert third.trading_mode == "paper"

    # Each instance is an independent, immutable snapshot.
    assert first.trading_mode == "paper"
    assert second.trading_mode == "live"


# (h) The runtime revalidation function used right before order submission
# reflects env changes made after the caller's own config was built.
def test_validate_order_allowed_now_reflects_current_env(monkeypatch):
    _clear_broker_env(monkeypatch)
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")

    stale_config = BrokerConfig.from_env()
    assert validate_order_allowed_now() is True

    # Environment flips to an unsafe combination between config creation and
    # the point of order submission -- the stale object doesn't know, but
    # the runtime revalidation call does.
    monkeypatch.setenv("ALPACA_PAPER_BASE_URL", "https://api.alpaca.markets")

    assert stale_config.validate_order_allowed() is True  # unaware of the change
    with pytest.raises(RuntimeError, match="not the official Paper endpoint"):
        validate_order_allowed_now()


def test_validate_order_allowed_now_accepts_explicit_env_mapping():
    safe_env = {
        "TRADING_MODE": "paper",
        "ALPACA_API_KEY": "key",
        "ALPACA_SECRET_KEY": "secret",
    }
    assert validate_order_allowed_now(env=safe_env) is True

    unsafe_env = {
        "TRADING_MODE": "live",
        "ENABLE_REAL_TRADING": "true",
        "LIVE_DRY_RUN": "false",
        "ALPACA_API_KEY": "key",
        "ALPACA_SECRET_KEY": "secret",
    }
    with pytest.raises(RuntimeError, match="Real live trading is disabled"):
        validate_order_allowed_now(env=unsafe_env)


def test_from_env_source_has_no_importlib_reload():
    import inspect

    source = inspect.getsource(broker_config_module)
    assert "importlib" not in source
