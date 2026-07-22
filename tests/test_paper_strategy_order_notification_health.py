"""CODEX-017: paper_strategy_order._safe_send_slack_alert() must route through
notification_health.send_with_health_tracking() instead of calling
send_slack_alert directly, so every Slack send outcome is recorded via
record_success()/record_failure(), and consecutive failures reaching the
threshold escalate kill_switch_state to ENTRY_DISABLED exactly like calling
notification_health directly would (see test_notification_health.py). That
escalation must then actually block submit_order(side="buy") through the
existing CODEX-016 gating (test_paper_strategy_order_kill_switch_state.py).
Slack failures must never distort order success/fill status, and no test
here ever touches the real Slack network path.
"""

import kill_switch_state as kss
import notification_health as nh
import paper_strategy_order as pso


def _use_tmp_health(monkeypatch, tmp_path):
    monkeypatch.delenv("NOTIFICATION_HEALTH_STATE_FILE", raising=False)
    monkeypatch.delenv("NOTIFICATION_HEALTH_LOG_FILE", raising=False)
    monkeypatch.delenv("NOTIFICATION_HEALTH_FAILURE_THRESHOLD", raising=False)
    state_path = tmp_path / "NOTIFICATION_HEALTH_STATE.json"
    log_path = tmp_path / "notification_health.log"
    monkeypatch.setattr(nh, "STATE_FILE", state_path)
    monkeypatch.setattr(nh, "LOG_FILE", log_path)
    return state_path, log_path


def _use_tmp_kill_switch(monkeypatch, tmp_path):
    monkeypatch.delenv("KILL_SWITCH_STATE_FILE", raising=False)
    path = tmp_path / "KILL_SWITCH_STATE.json"
    monkeypatch.setattr(kss, "STATE_FILE", path)
    return path


def _isolate_binary_kill_switch(monkeypatch, tmp_path):
    monkeypatch.delenv("TRADING_HALTED", raising=False)
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "no_such_kill_switch_file"))


def _network_spy(monkeypatch):
    """Guarantee the real Slack network path is never touched anywhere in
    these tests: any call to slack_utils.requests.post fails the test
    immediately, and every attempted call is recorded for a 0-calls assert."""
    import slack_utils

    calls = []

    def _fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("real Slack network path (requests.post) must never be called in tests")

    monkeypatch.setattr(slack_utils.requests, "post", _fail)
    return calls


class _FakeConfig:
    status_label = "PAPER"


class FakeBroker:
    """Minimal broker double: records call count/args, never touches the network."""

    def __init__(self):
        self.config = _FakeConfig()
        self.submit_calls = []

    def submit_order(self, symbol, qty=1, client_order_id=None):
        self.submit_calls.append((symbol, qty, client_order_id))
        return pso.BrokerResponse(status_code=200, text="OK", data={"status": "accepted"}, dry_run=False)

    def get_account(self):
        return {"equity": "10000", "last_equity": "10000"}

    def get_positions(self):
        return []

    def get_order_by_client_order_id(self, client_order_id):
        return None


# ---------------------------------------------------------------------------
# 1. _safe_send_slack_alert is wired through notification_health
# ---------------------------------------------------------------------------

def test_safe_send_slack_alert_records_failure_via_health(monkeypatch, tmp_path):
    _use_tmp_health(monkeypatch, tmp_path)
    network_calls = _network_spy(monkeypatch)
    monkeypatch.setattr(pso, "send_slack_alert", lambda msg: False)

    result = pso._safe_send_slack_alert("test message")

    assert result is False
    assert nh.get_status() == nh.DEGRADED
    record = nh.get_record()
    assert record["consecutive_failures"] == 1
    assert record["last_error_kind"] == "SEND_RETURNED_FALSY"
    assert network_calls == []


def test_safe_send_slack_alert_records_success_via_health(monkeypatch, tmp_path):
    _use_tmp_health(monkeypatch, tmp_path)
    network_calls = _network_spy(monkeypatch)
    monkeypatch.setattr(pso, "send_slack_alert", lambda msg: True)

    result = pso._safe_send_slack_alert("test message")

    assert result is True
    assert nh.get_status() == nh.HEALTHY
    assert nh.get_record()["consecutive_failures"] == 0
    assert network_calls == []


def test_safe_send_slack_alert_swallows_exception_and_records_failure(monkeypatch, tmp_path):
    _use_tmp_health(monkeypatch, tmp_path)
    network_calls = _network_spy(monkeypatch)

    def _raise(msg):
        raise ConnectionError("simulated slack outage")

    monkeypatch.setattr(pso, "send_slack_alert", _raise)

    result = pso._safe_send_slack_alert("test message")

    assert result is False
    record = nh.get_record()
    assert record["consecutive_failures"] == 1
    assert record["last_error_kind"] == "ConnectionError"
    assert network_calls == []


# ---------------------------------------------------------------------------
# 2. Consecutive failures escalate kill_switch_state, which then actually
#    blocks submit_order(side="buy") via the existing CODEX-016 gating.
# ---------------------------------------------------------------------------

def test_consecutive_slack_failures_escalate_and_block_buy_order(monkeypatch, tmp_path):
    _use_tmp_health(monkeypatch, tmp_path)
    _use_tmp_kill_switch(monkeypatch, tmp_path)
    _isolate_binary_kill_switch(monkeypatch, tmp_path)
    monkeypatch.setenv("NOTIFICATION_HEALTH_FAILURE_THRESHOLD", "3")
    network_calls = _network_spy(monkeypatch)
    monkeypatch.setattr(pso, "send_slack_alert", lambda msg: False)

    assert kss.get_state() == kss.ACTIVE

    for _ in range(3):
        pso._safe_send_slack_alert("order blocked notice")

    assert nh.get_status() == nh.FAILED
    assert kss.get_state() == kss.ENTRY_DISABLED

    broker = FakeBroker()
    response = pso.submit_order("AAPL", qty=1, broker=broker, side="buy")

    assert broker.submit_calls == []
    assert response.status_code == 423
    assert network_calls == []


def test_recovery_after_failures_restores_healthy(monkeypatch, tmp_path):
    _use_tmp_health(monkeypatch, tmp_path)
    _use_tmp_kill_switch(monkeypatch, tmp_path)
    monkeypatch.setenv("NOTIFICATION_HEALTH_FAILURE_THRESHOLD", "5")
    network_calls = _network_spy(monkeypatch)

    monkeypatch.setattr(pso, "send_slack_alert", lambda msg: False)
    pso._safe_send_slack_alert("first failure")
    pso._safe_send_slack_alert("second failure")
    assert nh.get_status() == nh.DEGRADED

    monkeypatch.setattr(pso, "send_slack_alert", lambda msg: True)
    result = pso._safe_send_slack_alert("recovered")

    assert result is True
    assert nh.get_status() == nh.HEALTHY
    record = nh.get_record()
    assert record["consecutive_failures"] == 0
    assert record["last_success_at"] is not None
    assert record["last_failure_at"] is not None  # history preserved, not erased
    assert network_calls == []


# ---------------------------------------------------------------------------
# 3. Slack failure must never distort order success/fill status (regression).
# ---------------------------------------------------------------------------

def _high_score_result(symbol):
    return {"symbol": symbol, "price": 100.0, "ma200": 90.0, "rsi": 50.0, "volume_ratio": 1.5, "score": 100}


def test_slack_failure_does_not_change_order_outcome_via_main(monkeypatch, tmp_path):
    import pandas as pd

    _use_tmp_health(monkeypatch, tmp_path)
    _use_tmp_kill_switch(monkeypatch, tmp_path)
    _isolate_binary_kill_switch(monkeypatch, tmp_path)
    network_calls = _network_spy(monkeypatch)

    broker = FakeBroker()
    monkeypatch.setattr(pso, "load_watchlist", lambda: ["AAPL"])
    monkeypatch.setattr(pso, "analyze_stock", _high_score_result)
    monkeypatch.setattr(pso, "get_us_market_session", lambda: "regular")
    monkeypatch.setattr(pso, "ORDER_HISTORY_FILE", tmp_path / "order_history.csv")
    monkeypatch.setattr(pso, "ORDER_HISTORY_LOCK_FILE", tmp_path / "order_history.lock")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_FILE", tmp_path / "order_reconciliation.csv")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_LOCK_FILE", tmp_path / "order_reconciliation.lock")
    pso.initialize_order_history()

    def _raise(msg):
        raise ConnectionError("slack unreachable")

    monkeypatch.setattr(pso, "send_slack_alert", _raise)

    result = pso.main(broker=broker)

    assert result["submitted"] == ["AAPL"]
    assert result["failed"] == []
    history = pd.read_csv(tmp_path / "order_history.csv")
    assert history.loc[history["symbol"] == "AAPL", "status"].iloc[0] == "SUBMITTED"

    # The health module observed the failures, independent of order success.
    assert nh.get_record()["consecutive_failures"] >= 1
    assert nh.get_status() in (nh.DEGRADED, nh.FAILED)
    assert network_calls == []
