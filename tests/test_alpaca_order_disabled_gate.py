"""KIS migration: broker/broker_config.py's Alpaca-order-disabled gate
(BrokerConfig.validate_alpaca_order_permitted()). Alpaca is being
repositioned as market-data-only; these flags default to disabled for
both paper and live modes per the migration spec's recommended env
block. Not yet wired into AlpacaBroker.submit_order() (see
_default_alpaca_order_enabled()'s docstring in broker_config.py) -- these
tests cover the gate function itself in isolation.
"""
import pytest

from broker import BrokerConfig


def _config(**overrides):
    kwargs = dict(trading_mode="paper", enable_real_trading=False, live_dry_run=True,
                  api_key="k", secret_key="s")
    kwargs.update(overrides)
    return BrokerConfig(**kwargs)


class TestDefaults:
    def test_alpaca_order_enabled_defaults_false(self):
        assert _config().alpaca_order_enabled is False

    def test_alpaca_paper_order_enabled_defaults_false(self):
        assert _config().alpaca_paper_order_enabled is False

    def test_from_env_defaults_false_when_unset(self, monkeypatch):
        monkeypatch.delenv("ALPACA_ORDER_ENABLED", raising=False)
        monkeypatch.delenv("ALPACA_PAPER_ORDER_ENABLED", raising=False)
        cfg = BrokerConfig.from_env()
        assert cfg.alpaca_order_enabled is False
        assert cfg.alpaca_paper_order_enabled is False

    def test_from_env_reads_explicit_true(self, monkeypatch):
        monkeypatch.setenv("ALPACA_ORDER_ENABLED", "true")
        monkeypatch.setenv("ALPACA_PAPER_ORDER_ENABLED", "true")
        cfg = BrokerConfig.from_env()
        assert cfg.alpaca_order_enabled is True
        assert cfg.alpaca_paper_order_enabled is True


class TestValidateAlpacaOrderPermitted:
    def test_paper_mode_blocked_by_default(self):
        with pytest.raises(RuntimeError, match="Alpaca paper orders are disabled"):
            _config(trading_mode="paper").validate_alpaca_order_permitted()

    def test_paper_mode_allowed_when_flag_explicitly_true(self):
        assert _config(trading_mode="paper", alpaca_paper_order_enabled=True) \
            .validate_alpaca_order_permitted() is True

    def test_live_mode_blocked_by_default(self):
        with pytest.raises(RuntimeError, match="Alpaca live orders are disabled"):
            _config(trading_mode="live").validate_alpaca_order_permitted()

    def test_live_mode_allowed_when_flag_explicitly_true(self):
        assert _config(trading_mode="live", alpaca_order_enabled=True) \
            .validate_alpaca_order_permitted() is True

    def test_live_flag_does_not_permit_paper_orders(self):
        # Setting alpaca_order_enabled=True must not accidentally also
        # permit paper orders -- the two flags are independent.
        with pytest.raises(RuntimeError, match="Alpaca paper orders are disabled"):
            _config(trading_mode="paper", alpaca_order_enabled=True).validate_alpaca_order_permitted()

    def test_paper_flag_does_not_permit_live_orders(self):
        with pytest.raises(RuntimeError, match="Alpaca live orders are disabled"):
            _config(trading_mode="live", alpaca_paper_order_enabled=True).validate_alpaca_order_permitted()

    def test_unrecognized_mode_blocked(self):
        with pytest.raises(RuntimeError, match="unrecognized trading_mode"):
            _config(trading_mode="sandbox").validate_alpaca_order_permitted()
