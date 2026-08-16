from datetime import datetime, timedelta, timezone

import pytest

from operations import alerts, commands, health, kill_switch

NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATIONS_HALT_STATE_FILE", str(tmp_path / "OPS_HALT.json"))
    monkeypatch.setenv("KILL_SWITCH_STATE_FILE", str(tmp_path / "KILL_SWITCH.json"))
    yield


class TestHaltState:
    def test_not_halted_by_default(self):
        assert kill_switch.is_halted() is False
        assert kill_switch.is_automatic_order_allowed() is True

    def test_set_halt_true(self):
        kill_switch.set_halt(True, reason="test", actor="tester")
        assert kill_switch.is_halted() is True
        assert kill_switch.is_automatic_order_allowed() is False

    def test_unhalt_restores(self):
        kill_switch.set_halt(True, reason="test", actor="tester")
        kill_switch.set_halt(False, reason="resolved", actor="tester")
        assert kill_switch.is_halted() is False

    def test_corrupted_halt_file_fails_closed(self, tmp_path, monkeypatch):
        bad_path = tmp_path / "OPS_HALT.json"
        bad_path.write_text("not json")
        monkeypatch.setenv("OPERATIONS_HALT_STATE_FILE", str(bad_path))
        with pytest.raises(kill_switch.OperationsError):
            kill_switch.is_halted()


class TestEmergencyLiquidationApproval:
    def test_matching_token_approved(self):
        approval = kill_switch.request_emergency_liquidation_approval(
            approved_by="operator1", reason="market crash", confirmation_token="secret123",
            expected_token="secret123",
        )
        assert approval.approved is True
        assert approval.approved_by == "operator1"

    def test_mismatched_token_blocked(self):
        with pytest.raises(kill_switch.OperationsError):
            kill_switch.request_emergency_liquidation_approval(
                approved_by="operator1", reason="x", confirmation_token="wrong",
                expected_token="secret123",
            )

    def test_missing_approved_by_blocked(self):
        with pytest.raises(kill_switch.OperationsError):
            kill_switch.request_emergency_liquidation_approval(
                approved_by="", reason="x", confirmation_token="secret123", expected_token="secret123",
            )


class TestCommands:
    def test_entry_off_then_entry_on(self):
        commands.entry_off(reason="test halt", activated_by="tester")
        assert kill_switch.is_entry_allowed() is False
        commands.entry_on(released_by="tester", reason="resolved")
        assert kill_switch.is_entry_allowed() is True

    def test_halt_command(self):
        commands.halt(reason="risk event", actor="tester")
        assert kill_switch.is_halted() is True
        commands.unhalt(reason="resolved", actor="tester")
        assert kill_switch.is_halted() is False

    def test_request_emergency_liquidation_command(self):
        approval = commands.request_emergency_liquidation(
            approved_by="operator1", reason="test", confirmation_token="t1", expected_token="t1",
        )
        assert approval.approved is True


class TestHealth:
    def test_fresh_alpaca_and_kis_ok(self):
        decision = health.evaluate_data_health(
            alpaca_data_as_of=NOW - timedelta(seconds=10), alpaca_max_staleness_seconds=60,
            kis_read_ok=True, now=NOW,
        )
        assert decision.new_entry_discovery_allowed is True
        assert decision.existing_position_monitoring_allowed is True

    def test_stale_alpaca_stops_new_entries_only(self):
        decision = health.evaluate_data_health(
            alpaca_data_as_of=NOW - timedelta(seconds=120), alpaca_max_staleness_seconds=60,
            kis_read_ok=True, now=NOW,
        )
        assert decision.new_entry_discovery_allowed is False
        assert decision.existing_position_monitoring_allowed is True

    def test_missing_alpaca_data_stops_new_entries_only(self):
        decision = health.evaluate_data_health(
            alpaca_data_as_of=None, alpaca_max_staleness_seconds=60, kis_read_ok=True, now=NOW,
        )
        assert decision.new_entry_discovery_allowed is False
        assert decision.existing_position_monitoring_allowed is True

    def test_kis_unreadable_stops_everything(self):
        decision = health.evaluate_data_health(
            alpaca_data_as_of=NOW, alpaca_max_staleness_seconds=60, kis_read_ok=False, now=NOW,
        )
        assert decision.new_entry_discovery_allowed is False
        assert decision.existing_position_monitoring_allowed is False


class TestAlerts:
    def test_format_order_blocked_message(self):
        msg = alerts.format_order_blocked_message(symbol="AAPL", side="buy", reason="insufficient cash")
        assert "AAPL" in msg and "buy" in msg and "insufficient cash" in msg

    def test_format_reconciliation_mismatch_message(self):
        msg = alerts.format_reconciliation_mismatch_message(mismatch_count=2, details="AAPL qty diff")
        assert "2" in msg

    def test_format_unknown_order_message(self):
        msg = alerts.format_unknown_order_message(internal_order_id="ord-1", symbol="AAPL")
        assert "ord-1" in msg and "AAPL" in msg
