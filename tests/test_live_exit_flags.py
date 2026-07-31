from config.live_exit_flags import LiveExitFlags


class TestLiveExitFlagsFromEnv:
    def test_all_default_false_when_unset(self):
        flags = LiveExitFlags.from_env(env={})
        assert flags.enable_partial_profit is False
        assert flags.enable_trailing_stop is False
        assert flags.enable_time_stop is False
        assert flags.enable_eod_exit is False

    def test_each_flag_independently_enabled(self):
        flags = LiveExitFlags.from_env(env={"LIVE_ENABLE_PARTIAL_PROFIT": "true"})
        assert flags.enable_partial_profit is True
        assert flags.enable_trailing_stop is False
        assert flags.enable_time_stop is False
        assert flags.enable_eod_exit is False

    def test_all_enabled(self):
        flags = LiveExitFlags.from_env(env={
            "LIVE_ENABLE_PARTIAL_PROFIT": "true", "LIVE_ENABLE_TRAILING_STOP": "true",
            "LIVE_ENABLE_TIME_STOP": "true", "LIVE_ENABLE_EOD_EXIT": "true",
        })
        assert flags.enable_partial_profit is True
        assert flags.enable_trailing_stop is True
        assert flags.enable_time_stop is True
        assert flags.enable_eod_exit is True

    def test_unrecognized_value_treated_as_false(self):
        flags = LiveExitFlags.from_env(env={"LIVE_ENABLE_PARTIAL_PROFIT": "maybe"})
        assert flags.enable_partial_profit is False
