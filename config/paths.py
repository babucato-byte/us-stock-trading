import os
from pathlib import Path


def get_project_root() -> Path:
    """Resolve the repository root.

    Uses TRADING_PROJECT_ROOT when set (e.g. on the Oracle Cloud server,
    where scripts are invoked by systemd/cron outside the repo). Falls back
    to auto-detecting the repo root relative to this file, which is what a
    fresh dev checkout (MacBook) needs with no extra configuration.
    """
    env_root = os.getenv("TRADING_PROJECT_ROOT")
    if env_root:
        return Path(env_root)
    return Path(__file__).resolve().parent.parent
