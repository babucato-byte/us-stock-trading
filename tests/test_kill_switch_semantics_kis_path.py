"""TCN-02A: what each kill-switch layer means on the KIS order path, pinned.

TCN-02A changes nothing about the kill switches. It records, in a test,
what they do TODAY on the KIS path so that TCN-02B changes them on
purpose rather than by accident:

    operations HALT (OPERATIONS_HALT_STATE.json)
        FULL_HALT: `authorize_new_order` refuses buy AND sell.

    kill_switch_state ENTRY_DISABLED (KILL_SWITCH_STATE.json)
        SAFE_EXIT_ONLY: the buy cycle refuses via `is_entry_allowed()`;
        the KIS sell path never consults it, so a protective EXIT
        proceeds.

    kill_switch_state ALL_TRADING_DISABLED / MANUAL_REVIEW
        `is_liquidation_allowed()` is False, but ONLY the Alpaca/paper
        path reads it. The KIS sell path is not gated by these states.
        This is the documented gap, left as-is here.

    KILL_SWITCH sentinel file / TRADING_HALTED env
        Alpaca/paper path only. Not consulted by the KIS path at all.

If any of these assertions starts failing, semantics moved.
"""

import pytest

import kill_switch_state as kss
from execution import authorization
from execution.authorization import UnauthorizedExecutionError
from operations import kill_switch as ops_kill_switch


@pytest.fixture(autouse=True)
def _isolate_halt_file(tmp_path, monkeypatch):
    """conftest isolates KILL_SWITCH_STATE.json but not the operations
    HALT file; without this, `set_halt` writes the real
    OPERATIONS_HALT_STATE.json at the repo root."""
    monkeypatch.setenv("OPERATIONS_HALT_STATE_FILE", str(tmp_path / "OPS_HALT.json"))
    yield


class _Intent:
    internal_order_id = "sell-1"
    side = "sell"
    symbol = "AAPL"


def _authorize_sell():
    return authorization.authorize_new_order(_Intent(), lambda: object(), lambda ctx: True)


class TestHaltIsFullHalt:
    def test_halt_refuses_a_sell(self):
        ops_kill_switch.set_halt(True, reason="test", actor="test")
        with pytest.raises(UnauthorizedExecutionError):
            _authorize_sell()

    def test_clear_permits_a_sell(self):
        ops_kill_switch.set_halt(False, reason="test", actor="test")
        assert _authorize_sell().side == "sell"


class TestEntryDisabledIsSafeExitOnly:
    def test_entry_disabled_blocks_entries(self):
        kss.activate(kss.ENTRY_DISABLED, "test", "test")
        assert kss.is_entry_allowed() is False
        assert ops_kill_switch.is_entry_allowed() is False

    def test_entry_disabled_does_not_touch_the_kis_sell_path(self):
        kss.activate(kss.ENTRY_DISABLED, "test", "test")
        assert kss.is_liquidation_allowed() is True
        assert _authorize_sell().side == "sell"


class TestTheDocumentedGap:
    """ALL_TRADING_DISABLED and MANUAL_REVIEW say "no exits" through
    `is_liquidation_allowed()`, and the KIS sell path does not read it.
    Pinned as CURRENT behaviour, not endorsed: closing it is a TCN-02B
    decision, and it must be closed deliberately."""

    @pytest.mark.parametrize("state", [kss.ALL_TRADING_DISABLED, kss.MANUAL_REVIEW])
    def test_the_state_says_no_liquidation(self, state):
        kss.activate(state, "test", "test")
        assert kss.is_liquidation_allowed() is False

    @pytest.mark.parametrize("state", [kss.ALL_TRADING_DISABLED, kss.MANUAL_REVIEW])
    def test_but_the_kis_sell_authorization_does_not_consult_it(self, state):
        kss.activate(state, "test", "test")
        assert _authorize_sell().side == "sell"

    def test_the_kis_sell_path_reads_only_halt(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        source = (root / "execution" / "authorization.py").read_text(encoding="utf-8")
        assert "is_automatic_order_allowed" in source
        assert "is_liquidation_allowed" not in source
