"""CODEX-046: independent kill-switches for the KIS live-rollout's
"extra" exit behaviors -- partial profit-taking at target_1, the
breakeven-trailing-stop move, the time-stop forced close, and the
end-of-day forced close. All four default to False (the safest,
narrowest initial live-rollout posture: only stop-loss and full
take-profit at target_2 are active out of the box).

Basic stop-loss and full take-profit (target_2)/regular-session
limit-sell are NOT covered by any flag here -- spec requires them to
remain always active; positions/lifecycle.py's check_and_manage()
evaluates those unconditionally regardless of these flags.

These flags are read and applied BEFORE check_and_manage() evaluates
the corresponding condition (kis_position_manager.py passes them in as
constructor arguments) -- never as an after-the-fact filter on an
order intent that was already built."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _env_bool(env, name, default):
    value = env.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class LiveExitFlags:
    enable_partial_profit: bool
    enable_trailing_stop: bool
    enable_time_stop: bool
    enable_eod_exit: bool

    @classmethod
    def from_env(cls, env=None):
        env = env if env is not None else os.environ
        return cls(
            enable_partial_profit=_env_bool(env, "LIVE_ENABLE_PARTIAL_PROFIT", False),
            enable_trailing_stop=_env_bool(env, "LIVE_ENABLE_TRAILING_STOP", False),
            enable_time_stop=_env_bool(env, "LIVE_ENABLE_TIME_STOP", False),
            enable_eod_exit=_env_bool(env, "LIVE_ENABLE_EOD_EXIT", False),
        )
