import pytest

import kill_switch_state as kss
import notification_health as nh


@pytest.fixture(autouse=True)
def _isolate_health_and_kill_switch_state_files(monkeypatch, tmp_path):
    """CODEX-017: paper_strategy_order._safe_send_slack_alert() now always
    routes through notification_health.send_with_health_tracking(), which can
    in turn escalate kill_switch_state. Without this, any test that merely
    monkeypatches pso.send_slack_alert (there are several, none of which are
    testing notification_health/kill_switch_state themselves) would silently
    read/write the real NOTIFICATION_HEALTH_STATE.json / notification_health.log
    / KILL_SWITCH_STATE.json at the repo root as a side effect, and could even
    escalate the real kill switch if consecutive failures crossed the
    threshold across tests. Point both at tmp_path by default; a test that
    wants to exercise these modules directly (e.g. test_notification_health.py,
    test_paper_strategy_order_notification_health.py) still monkeypatches its
    own path afterwards, which simply overrides this default.
    """
    monkeypatch.delenv("NOTIFICATION_HEALTH_STATE_FILE", raising=False)
    monkeypatch.delenv("NOTIFICATION_HEALTH_LOG_FILE", raising=False)
    monkeypatch.setattr(nh, "STATE_FILE", tmp_path / "NOTIFICATION_HEALTH_STATE.json")
    monkeypatch.setattr(nh, "LOG_FILE", tmp_path / "notification_health.log")

    monkeypatch.delenv("KILL_SWITCH_STATE_FILE", raising=False)
    monkeypatch.setattr(kss, "STATE_FILE", tmp_path / "KILL_SWITCH_STATE.json")
