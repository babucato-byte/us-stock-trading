import pytest

from config.live_rollout_config import LiveRolloutConfig, LiveRolloutConfigError


class TestFromEnv:
    def test_defaults_are_safe(self):
        cfg = LiveRolloutConfig.from_env(env={})
        assert cfg.enabled is False
        assert cfg.max_quantity_per_order == 1
        assert cfg.max_open_positions == 1
        assert cfg.max_daily_entries == 1
        assert cfg.regular_session_only is True
        assert cfg.allow_fractional is False
        assert cfg.allow_market_order is False
        assert cfg.allow_extended_hours is False
        assert cfg.allow_leverage is False
        assert cfg.allow_inverse is False
        assert cfg.allow_short is False
        assert cfg.allow_margin is False
        assert cfg.max_price_deviation_percent == pytest.approx(0.30)

    def test_reads_explicit_values(self):
        env = {
            "LIVE_ROLLOUT_ENABLED": "true", "LIVE_ROLLOUT_ALLOWED_SYMBOLS": "AAPL,msft",
            "LIVE_ROLLOUT_MAX_QUANTITY": "2", "LIVE_ROLLOUT_MAX_POSITIONS": "3",
            "LIVE_ROLLOUT_MAX_DAILY_ENTRIES": "4", "MAX_PRICE_DEVIATION_PERCENT": "0.5",
        }
        cfg = LiveRolloutConfig.from_env(env=env)
        assert cfg.enabled is True
        assert cfg.allowed_symbols == frozenset({"AAPL", "MSFT"})
        assert cfg.max_quantity_per_order == 2
        assert cfg.max_open_positions == 3
        assert cfg.max_daily_entries == 4
        assert cfg.max_price_deviation_percent == pytest.approx(0.5)

    def test_invalid_int_raises(self):
        with pytest.raises(LiveRolloutConfigError):
            LiveRolloutConfig.from_env(env={"LIVE_ROLLOUT_MAX_QUANTITY": "not-a-number"})


class TestValidate:
    def test_default_config_validates(self):
        assert LiveRolloutConfig.from_env(env={}).validate() is True

    @pytest.mark.parametrize("field,value", [
        ("max_quantity_per_order", 0), ("max_quantity_per_order", -1), ("max_quantity_per_order", 1.5),
        ("max_open_positions", 0), ("max_daily_entries", 0),
        ("max_price_deviation_percent", 0), ("max_price_deviation_percent", -1),
        ("max_price_deviation_percent", float("nan")),
    ])
    def test_invalid_numeric_field_blocks(self, field, value):
        cfg = LiveRolloutConfig.from_env(env={})
        bad = cfg.__class__(**{**cfg.__dict__, field: value})
        with pytest.raises(LiveRolloutConfigError):
            bad.validate()

    @pytest.mark.parametrize("field", [
        "allow_fractional", "allow_market_order", "allow_extended_hours",
        "allow_leverage", "allow_inverse", "allow_short", "allow_margin",
    ])
    def test_forbidden_flag_true_blocks(self, field):
        cfg = LiveRolloutConfig.from_env(env={})
        bad = cfg.__class__(**{**cfg.__dict__, field: True})
        with pytest.raises(LiveRolloutConfigError):
            bad.validate()
