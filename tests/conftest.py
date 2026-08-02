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


@pytest.fixture(autouse=True)
def _isolate_kis_rate_limiter(monkeypatch, tmp_path):
    """HIGH-2: the KIS rate limiter paces every read by a real 3 seconds
    and records the budget in a shared file at the repo root. Without
    this, every unrelated KIS test would genuinely sleep and would drop a
    rate-limit state file into the repository.

    Only the WAITING is neutralized, and only for tests that are not about
    pacing: tests/test_kis_rate_limiting.py injects its own limiter with a
    virtual clock and asserts the real intervals there.
    """
    from brokers import kis_rate_limiter, kis_token_cache

    monkeypatch.setenv("KIS_RATE_LIMIT_STATE_FILE", str(tmp_path / "KIS_RATE_LIMIT.json"))
    # MEDIUM: same reasoning for the shared token cache -- an unisolated
    # test would read/write a real token file at the repo root.
    monkeypatch.setenv("KIS_TOKEN_CACHE_FILE", str(tmp_path / "KIS_TOKEN_CACHE.json"))
    monkeypatch.setenv("KIS_READ_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("KIS_TOKEN_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("KIS_ORDER_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("KIS_RATE_LIMIT_BASE_BACKOFF_SECONDS", "0")
    monkeypatch.setenv("KIS_RATE_LIMIT_MAX_BACKOFF_SECONDS", "0")
    kis_rate_limiter.reset_limiter()
    kis_token_cache.reset_cache()
    yield
    kis_rate_limiter.reset_limiter()
    kis_token_cache.reset_cache()
