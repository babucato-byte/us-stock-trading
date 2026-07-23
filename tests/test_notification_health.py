"""[MEDIUM] Slack notification self-health-monitoring (notification_health.py).

Every test here is fully offline: Slack sends are simulated either with a
plain fake callable, or by monkeypatching slack_utils.requests.post to a fake
that never touches the network. No real HTTP call is ever made.
"""

import kill_switch_state as kss
import notification_health as nh
import paper_strategy_order as pso
import pytest
import requests
import slack_utils


# ---------------------------------------------------------------------------
# Fixtures: isolate notification_health's state/log files and the kill
# switch's state file to tmp_path, same pattern as test_kill_switch_states.py.
# ---------------------------------------------------------------------------

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


class FakeSlackResponse:
    def __init__(self, status_code=200, text="OK"):
        self.status_code = status_code
        self.text = text


# ---------------------------------------------------------------------------
# 0. No history -> UNKNOWN
# ---------------------------------------------------------------------------

def test_no_history_is_unknown(monkeypatch, tmp_path):
    _use_tmp_health(monkeypatch, tmp_path)

    assert nh.get_status() == nh.UNKNOWN
    assert "UNKNOWN" in nh.summarize()


# ---------------------------------------------------------------------------
# 1. Slack success -> HEALTHY
# ---------------------------------------------------------------------------

def test_slack_success_records_healthy(monkeypatch, tmp_path):
    _use_tmp_health(monkeypatch, tmp_path)
    monkeypatch.setattr(slack_utils, "SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/fake")
    monkeypatch.setattr(slack_utils.requests, "post", lambda *a, **k: FakeSlackResponse(200, "OK"))

    ok = nh.send_with_health_tracking(slack_utils.send_slack_message, "hello")

    assert ok is True
    assert nh.get_status() == nh.HEALTHY
    record = nh.get_record()
    assert record["consecutive_failures"] == 0
    assert record["last_success_at"] is not None


# ---------------------------------------------------------------------------
# 2. Timeout exception -> recorded failure, DEGRADED (below threshold)
# ---------------------------------------------------------------------------

def test_slack_timeout_records_failure_and_degraded(monkeypatch, tmp_path):
    _use_tmp_health(monkeypatch, tmp_path)
    monkeypatch.setattr(slack_utils, "SLACK_ALERT_WEBHOOK_URL", "https://hooks.slack.com/services/fake")

    def _raise_timeout(*a, **k):
        raise requests.exceptions.Timeout("simulated timeout")

    monkeypatch.setattr(slack_utils.requests, "post", _raise_timeout)

    ok = nh.send_with_health_tracking(slack_utils.send_slack_alert, "hello")

    assert ok is False
    assert nh.get_status() == nh.DEGRADED
    record = nh.get_record()
    assert record["consecutive_failures"] == 1
    assert record["last_error_kind"] == "Timeout"
    assert record["last_failure_at"] is not None


# ---------------------------------------------------------------------------
# 3. HTTP error response (e.g. 500) -> recorded failure
# ---------------------------------------------------------------------------

def test_slack_http_error_response_records_failure(monkeypatch, tmp_path):
    _use_tmp_health(monkeypatch, tmp_path)
    monkeypatch.setattr(slack_utils, "SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/fake")
    monkeypatch.setattr(slack_utils.requests, "post", lambda *a, **k: FakeSlackResponse(500, "internal error"))

    ok = nh.send_with_health_tracking(slack_utils.send_slack_message, "hello")

    assert ok is False
    record = nh.get_record()
    assert record["consecutive_failures"] == 1
    assert record["last_error_kind"] == "SEND_RETURNED_FALSY"
    assert nh.get_status() == nh.DEGRADED


# ---------------------------------------------------------------------------
# 4. Invalid webhook URL: unset config, and a malformed URL string
# ---------------------------------------------------------------------------

def test_missing_webhook_url_records_failure(monkeypatch, tmp_path):
    _use_tmp_health(monkeypatch, tmp_path)
    monkeypatch.setattr(slack_utils, "SLACK_WEBHOOK_URL", None)

    ok = nh.send_with_health_tracking(slack_utils.send_slack_message, "hello")

    assert ok is False
    assert nh.get_record()["consecutive_failures"] == 1


def test_malformed_webhook_url_records_failure(monkeypatch, tmp_path):
    _use_tmp_health(monkeypatch, tmp_path)
    monkeypatch.setattr(slack_utils, "SLACK_WEBHOOK_URL", "not-a-valid-url")

    def _raise_missing_schema(*a, **k):
        raise requests.exceptions.MissingSchema("Invalid URL 'not-a-valid-url'")

    monkeypatch.setattr(slack_utils.requests, "post", _raise_missing_schema)

    ok = nh.send_with_health_tracking(slack_utils.send_slack_message, "hello")

    assert ok is False
    record = nh.get_record()
    assert record["consecutive_failures"] == 1
    assert record["last_error_kind"] == "MissingSchema"


# ---------------------------------------------------------------------------
# 5. Consecutive failures accumulate -> FAILED, kill switch escalates
# ---------------------------------------------------------------------------

def test_consecutive_failures_transition_to_failed_and_escalate_kill_switch(monkeypatch, tmp_path):
    _use_tmp_health(monkeypatch, tmp_path)
    _use_tmp_kill_switch(monkeypatch, tmp_path)
    monkeypatch.setenv("NOTIFICATION_HEALTH_FAILURE_THRESHOLD", "3")

    assert kss.get_state() == kss.ACTIVE

    for _ in range(2):
        nh.record_failure(error_kind="ConnectionError")
    assert nh.get_status() == nh.DEGRADED
    assert kss.get_state() == kss.ACTIVE  # not yet at threshold

    nh.record_failure(error_kind="ConnectionError")

    assert nh.get_status() == nh.FAILED
    assert nh.get_record()["consecutive_failures"] == 3
    assert kss.get_state() == kss.ENTRY_DISABLED
    record = kss.get_current_record()
    assert record["activated_by"] == "notification_health"
    assert "3" in record["reason"]


# ---------------------------------------------------------------------------
# 8. At the threshold, new entries are blocked but liquidation stays allowed
# ---------------------------------------------------------------------------

def test_threshold_breach_blocks_entries_allows_liquidation(monkeypatch, tmp_path):
    _use_tmp_health(monkeypatch, tmp_path)
    _use_tmp_kill_switch(monkeypatch, tmp_path)
    monkeypatch.setenv("NOTIFICATION_HEALTH_FAILURE_THRESHOLD", "2")

    nh.record_failure(error_kind="Timeout")
    nh.record_failure(error_kind="Timeout")

    assert kss.is_entry_allowed() is False
    assert kss.is_liquidation_allowed() is True


def test_escalation_does_not_override_more_restrictive_existing_state(monkeypatch, tmp_path):
    """If a human/other subsystem has already escalated further (e.g.
    MANUAL_REVIEW), notification_health must not downgrade that back to
    ENTRY_DISABLED just because it also detected a Slack outage."""
    _use_tmp_health(monkeypatch, tmp_path)
    _use_tmp_kill_switch(monkeypatch, tmp_path)
    monkeypatch.setenv("NOTIFICATION_HEALTH_FAILURE_THRESHOLD", "1")
    kss.activate(kss.MANUAL_REVIEW, reason="unrelated incident", activated_by="ops1")

    nh.record_failure(error_kind="Timeout")

    assert kss.get_state() == kss.MANUAL_REVIEW
    assert kss.is_liquidation_allowed() is False


# ---------------------------------------------------------------------------
# 6. Recovery: a success after failures resets the streak back to HEALTHY
# ---------------------------------------------------------------------------

def test_recovery_after_failures_then_success(monkeypatch, tmp_path):
    _use_tmp_health(monkeypatch, tmp_path)
    monkeypatch.setenv("NOTIFICATION_HEALTH_FAILURE_THRESHOLD", "3")

    nh.record_failure(error_kind="Timeout")
    nh.record_failure(error_kind="Timeout")
    assert nh.get_status() == nh.DEGRADED

    nh.record_success(status_code=200)

    assert nh.get_status() == nh.HEALTHY
    record = nh.get_record()
    assert record["consecutive_failures"] == 0
    assert record["last_error_kind"] is None
    assert record["last_success_at"] is not None
    assert record["last_failure_at"] is not None  # history preserved, not erased


# ---------------------------------------------------------------------------
# 7. Slack failure must never change an order's own result (regression guard,
#    same shape as t7's test_order_event_notifications.py / t4's
#    test_api_failure_isolation.py, but routed through notification_health).
# ---------------------------------------------------------------------------

class FakeBrokerResponse:
    def __init__(self, status_code=200, text="OK", dry_run=False, data=None):
        self.status_code = status_code
        self.text = text
        self.dry_run = dry_run
        self.data = data


class FakeConfig:
    status_label = "PAPER"


class FakeBroker:
    def __init__(self):
        self.config = FakeConfig()
        self.submit_calls = []

    def get_account(self):
        return {"equity": "10000", "last_equity": "10000"}

    def get_positions(self):
        return []

    def submit_order(self, symbol, qty=1, *, side, client_order_id=None):
        self.submit_calls.append((symbol, qty))
        return FakeBrokerResponse(status_code=200, text="OK", dry_run=False)

    def get_order_by_client_order_id(self, client_order_id):
        return None


def _high_score_result(symbol):
    return {"symbol": symbol, "price": 100.0, "ma200": 90.0, "rsi": 50.0, "volume_ratio": 1.5, "score": 100}


def test_slack_failure_does_not_change_order_status(monkeypatch, tmp_path):
    import pandas as pd

    _use_tmp_health(monkeypatch, tmp_path)
    broker = FakeBroker()

    monkeypatch.setattr(pso, "load_watchlist", lambda: ["AAPL"])
    monkeypatch.setattr(pso, "analyze_stock", _high_score_result)
    monkeypatch.setattr(pso, "get_us_market_session", lambda: "regular")
    monkeypatch.setattr(pso, "ORDER_HISTORY_FILE", tmp_path / "order_history.csv")
    monkeypatch.setattr(pso, "ORDER_HISTORY_LOCK_FILE", tmp_path / "order_history.lock")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_FILE", tmp_path / "order_reconciliation.csv")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_LOCK_FILE", tmp_path / "order_reconciliation.lock")
    pso.initialize_order_history()

    def _raise_slack(message):
        raise requests.exceptions.ConnectionError("slack unreachable")

    monkeypatch.setattr(pso, "send_slack_alert", lambda msg: nh.send_with_health_tracking(_raise_slack, msg))

    result = pso.main(broker=broker)  # must not raise despite every notification failing

    assert result["submitted"] == ["AAPL"]
    assert result["failed"] == []
    history = pd.read_csv(tmp_path / "order_history.csv")
    assert history.loc[history["symbol"] == "AAPL", "status"].iloc[0] == "SUBMITTED"

    # The health module observed the failures, independent of order success.
    assert nh.get_record()["consecutive_failures"] >= 1
    assert nh.get_status() in (nh.DEGRADED, nh.FAILED)


# ---------------------------------------------------------------------------
# 9. Fallback local log always gets a line, success or failure
# ---------------------------------------------------------------------------

def test_fallback_log_records_success_and_failure(monkeypatch, tmp_path):
    _, log_path = _use_tmp_health(monkeypatch, tmp_path)

    nh.record_success(status_code=200)
    nh.record_failure(error_kind="Timeout")

    assert log_path.exists()
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert "event=success" in lines[0]
    assert "event=failure" in lines[1]
    assert "detail=Timeout" in lines[1]


def test_fallback_log_written_even_when_kill_switch_escalation_would_run(monkeypatch, tmp_path):
    """The local log must not depend on the kill switch module being
    importable/writable -- it is the last line of defense."""
    _, log_path = _use_tmp_health(monkeypatch, tmp_path)
    _use_tmp_kill_switch(monkeypatch, tmp_path)
    monkeypatch.setenv("NOTIFICATION_HEALTH_FAILURE_THRESHOLD", "1")

    nh.record_failure(error_kind="Timeout")

    assert log_path.exists()
    assert "event=failure" in log_path.read_text()


# ---------------------------------------------------------------------------
# Misc: get_record / summarize expose the documented fields
# ---------------------------------------------------------------------------

def test_summarize_includes_all_tracked_fields(monkeypatch, tmp_path):
    _use_tmp_health(monkeypatch, tmp_path)
    nh.record_failure(error_kind="Timeout", status_code=None, retry_result="retry_failed")

    summary = nh.summarize()

    assert nh.DEGRADED in summary
    assert "consecutive_failures" in summary
    assert "last_failure_at" in summary
    assert "last_error_kind" in summary
    assert "last_retry_result" in summary


def test_all_four_status_strings_are_exposed():
    assert {nh.HEALTHY, nh.DEGRADED, nh.FAILED, nh.UNKNOWN} == {"HEALTHY", "DEGRADED", "FAILED", "UNKNOWN"}
