"""[MEDIUM] Order submit/fill/reject/exception event notifications.

paper_strategy_order.main() reuses slack_utils.send_slack_alert (via the
_safe_send_slack_alert wrapper, same pattern as t4's
test_api_failure_isolation.py) for four order lifecycle events: submission,
fill, rejection, and submission exception. Every test here injects a fake
notifier (monkeypatching pso.send_slack_alert) so no real Slack webhook call
is ever made, and asserts the fake notifier's captured messages -- never a
real network call.
"""

import pandas as pd
import requests

import paper_strategy_order as pso

TODAY = pso.eastern_now().strftime("%Y-%m-%d")


class FakeBrokerResponse:
    def __init__(self, status_code=200, text="OK", dry_run=False, data=None):
        self.status_code = status_code
        self.text = text
        self.dry_run = dry_run
        self.data = data


class FakeConfig:
    status_label = "PAPER"


class FakeBroker:
    """No real Alpaca/HTTP calls. submit_side_effects maps a symbol to an
    Exception to raise (e.g. a timeout) instead of returning a response."""

    def __init__(self, account=None, positions=None, submit_side_effects=None, default_response=None):
        self.config = FakeConfig()
        self._account = account or {"equity": "10000", "last_equity": "10000"}
        self._positions = positions or []
        self._submit_side_effects = submit_side_effects or {}
        self._default_response = default_response or FakeBrokerResponse(status_code=200, text="OK", dry_run=False)
        self.submit_calls = []

    def get_account(self):
        return self._account

    def get_positions(self):
        return self._positions

    def submit_order(self, symbol, qty=1, client_order_id=None):
        self.submit_calls.append((symbol, qty))
        effect = self._submit_side_effects.get(symbol)
        if isinstance(effect, Exception):
            raise effect
        return effect or self._default_response

    def get_order_by_client_order_id(self, client_order_id):
        return None


def _high_score_result(symbol):
    return {"symbol": symbol, "price": 100.0, "ma200": 90.0, "rsi": 50.0, "volume_ratio": 1.5, "score": 100}


def _patch_common(monkeypatch, tmp_path, tickers, broker, market_session="regular"):
    monkeypatch.setattr(pso, "load_watchlist", lambda: tickers)
    monkeypatch.setattr(pso, "analyze_stock", _high_score_result)
    monkeypatch.setattr(pso, "get_us_market_session", lambda: market_session)
    monkeypatch.setattr(pso, "ORDER_HISTORY_FILE", tmp_path / "order_history.csv")
    monkeypatch.setattr(pso, "ORDER_HISTORY_LOCK_FILE", tmp_path / "order_history.lock")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_FILE", tmp_path / "order_reconciliation.csv")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_LOCK_FILE", tmp_path / "order_reconciliation.lock")
    pso.initialize_order_history()
    slack_calls = []
    monkeypatch.setattr(pso, "send_slack_alert", lambda msg: slack_calls.append(msg) or True)
    return slack_calls


# ---------------------------------------------------------------------------
# 1. Order submission event
# ---------------------------------------------------------------------------

def test_order_submission_notifies_fake_notifier_once(monkeypatch, tmp_path):
    """A broker accept that hasn't (yet) filled sends exactly one submission
    notification carrying the symbol and SUBMITTED status."""
    broker = FakeBroker(default_response=FakeBrokerResponse(status_code=200, text="OK", dry_run=False, data=None))
    slack_calls = _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    result = pso.main(broker=broker)

    assert result["submitted"] == ["AAPL"]
    submission_alerts = [msg for msg in slack_calls if "Paper Strategy Order" in msg and "SUBMITTED" in msg]
    assert len(submission_alerts) == 1
    assert "AAPL" in submission_alerts[0]
    # No fill/reject events should fire for a plain accept-but-not-filled response.
    assert not any("Order filled" in msg for msg in slack_calls)
    assert not any("Order rejected" in msg for msg in slack_calls)


# ---------------------------------------------------------------------------
# 2. Order fill event
# ---------------------------------------------------------------------------

def test_order_fill_notifies_fake_notifier_once(monkeypatch, tmp_path):
    broker = FakeBroker(
        default_response=FakeBrokerResponse(
            status_code=200,
            text="OK",
            dry_run=False,
            data={"status": "filled", "filled_qty": 1, "filled_avg_price": 101.5},
        )
    )
    slack_calls = _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    result = pso.main(broker=broker)

    assert result["submitted"] == ["AAPL"]
    fill_alerts = [msg for msg in slack_calls if "Order filled" in msg]
    assert len(fill_alerts) == 1
    assert "AAPL" in fill_alerts[0]
    assert "FILLED" in fill_alerts[0]

    history = pd.read_csv(tmp_path / "order_history.csv")
    assert history.loc[history["symbol"] == "AAPL", "status"].iloc[0] == "FILLED"


# ---------------------------------------------------------------------------
# 3. Order rejection event
# ---------------------------------------------------------------------------

def test_order_rejection_notifies_fake_notifier_once(monkeypatch, tmp_path):
    broker = FakeBroker(
        default_response=FakeBrokerResponse(status_code=422, text="insufficient buying power", dry_run=False)
    )
    slack_calls = _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    result = pso.main(broker=broker)

    assert result["failed"] == ["AAPL"]
    reject_alerts = [msg for msg in slack_calls if "Order rejected" in msg]
    assert len(reject_alerts) == 1
    assert "AAPL" in reject_alerts[0]

    history = pd.read_csv(tmp_path / "order_history.csv")
    assert history.loc[history["symbol"] == "AAPL", "status"].iloc[0] == "REJECTED"


# ---------------------------------------------------------------------------
# 4. Submission exception event
# ---------------------------------------------------------------------------

def test_order_submission_exception_notifies_fake_notifier_once(monkeypatch, tmp_path):
    broker = FakeBroker(submit_side_effects={"AAPL": requests.exceptions.Timeout("simulated timeout")})
    slack_calls = _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    result = pso.main(broker=broker)

    assert result["failed"] == ["AAPL"]
    exception_alerts = [msg for msg in slack_calls if "Order failed" in msg]
    assert len(exception_alerts) == 1
    assert "AAPL" in exception_alerts[0]

    history = pd.read_csv(tmp_path / "order_history.csv")
    assert history.loc[history["symbol"] == "AAPL", "status"].iloc[0] == "SUBMISSION_FAILED"


# ---------------------------------------------------------------------------
# 5. Notifier failure must never change the order execution result
# ---------------------------------------------------------------------------

def test_notifier_failure_does_not_change_order_result(monkeypatch, tmp_path):
    """t4-consistent: a notifier that raises for every event must never flip
    the submitted/failed aggregation, for any of the four event types."""
    broker = FakeBroker(
        default_response=FakeBrokerResponse(
            status_code=200,
            text="OK",
            dry_run=False,
            data={"status": "filled", "filled_qty": 1, "filled_avg_price": 101.5},
        )
    )
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    def _raise_notifier(message):
        raise requests.exceptions.ConnectionError("slack unreachable")

    monkeypatch.setattr(pso, "send_slack_alert", _raise_notifier)

    result = pso.main(broker=broker)  # must not raise despite every notification failing

    assert result["submitted"] == ["AAPL"]
    assert result["failed"] == []
    history = pd.read_csv(tmp_path / "order_history.csv")
    assert history.loc[history["symbol"] == "AAPL", "status"].iloc[0] == "FILLED"
