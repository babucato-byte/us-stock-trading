"""Global kill switch: environment variable or sentinel file that blocks all
new order submissions regardless of anything else in the pipeline.

Fail-open by design (opposite of order_history's fail-closed policy): with
neither TRADING_HALTED set nor a kill-switch file present, is_trading_halted()
must return False so existing behavior is unchanged for every current
deployment that has never heard of this switch.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
KILL_SWITCH_FILE = BASE_DIR / "KILL_SWITCH"


def _resolve_kill_switch_path():
    override = os.environ.get("KILL_SWITCH_FILE")
    return Path(override) if override else KILL_SWITCH_FILE


def is_trading_halted():
    """True if either halt mechanism is engaged; False only when neither is."""
    if os.environ.get("TRADING_HALTED", "").strip().lower() == "true":
        return True
    return _resolve_kill_switch_path().exists()


# Re-exported multi-level kill switch state machine (kill_switch_state.py) so
# callers that only import kill_switch get both mechanisms from one place.
# is_trading_halted() above is untouched by this -- it is the pre-existing,
# fail-open binary halt check and stays exactly as it was.
from kill_switch_state import (  # noqa: E402,F401
    ACTIVE,
    ENTRY_DISABLED,
    ALL_TRADING_DISABLED,
    MANUAL_REVIEW,
    KillSwitchStateError,
    get_state,
    get_current_record,
    get_history,
    is_entry_allowed,
    is_liquidation_allowed,
    activate,
    release,
)
