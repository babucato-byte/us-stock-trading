"""[HIGH] API timeout / partial-response symbol-level failure isolation.

Covers two entry points that talk to external services per symbol:

- paper_strategy_order.main(): order submission (broker HTTP calls),
  order_history persistence, and Slack notifications. A failure in any one
  of these for one symbol must not (a) abort processing of the remaining
  symbols, or (b) distort the submitted/failed aggregation -- a side
  channel (Slack) failure must never flip a real outcome, and a persistence
  failure must never be counted as a success (fail-closed).
- scalping_watchlist.pipeline.run_scan_cycle(): market data provider calls.
  A 5xx/timeout/partial-response for one symbol must only exclude that
  symbol, never abort the scan for the rest of the universe.

Every test here uses fakes/monkeypatches to inject the failure; none of
them perform real network I/O.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import requests

import paper_strategy_order as pso
from scalping_watchlist import repository
from scalping_watchlist.data_provider import FakeMarketDataProvider, SymbolSnapshot
from scalping_watchlist.pipeline import run_scan_cycle

# ---------------------------------------------------------------------------
# paper_strategy_order.main() fixtures (same shape as test_paper_order_execution.py)
# ---------------------------------------------------------------------------

TODAY = pso.eastern_now().strftime("%Y-%m-%d")


class FakeConfig:
    status_label = "PAPER"


class FakeBrokerResponse:
    def __init__(self, status_code=200, text="OK", dry_run=False, data=None):
        self.status_code = status_code
        self.text = text
        self.dry_run = dry_run
        self.data = data


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
        return self._default_response

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
# 1. Order submission timeout: only that symbol fails, loop continues
# ---------------------------------------------------------------------------

def test_submit_timeout_isolates_symbol_and_loop_continues(monkeypatch, tmp_path):
    broker = FakeBroker(submit_side_effects={"TIMEOUT": requests.exceptions.Timeout("simulated timeout")})
    _patch_common(monkeypatch, tmp_path, ["TIMEOUT", "OK"], broker)

    result = pso.main(broker=broker)

    # Both symbols were reached -- the timeout on the first did not abort
    # the loop before the second was processed.
    assert broker.submit_calls == [("TIMEOUT", 1), ("OK", 1)]
    assert result["failed"] == ["TIMEOUT"]
    assert result["submitted"] == ["OK"]

    history = pd.read_csv(tmp_path / "order_history.csv")
    assert history.loc[history["symbol"] == "TIMEOUT", "status"].iloc[0] == "SUBMISSION_FAILED"
    assert history.loc[history["symbol"] == "OK", "status"].iloc[0] == "SUBMITTED"


# ---------------------------------------------------------------------------
# 2. order_history persistence failure -> not counted as a success
# ---------------------------------------------------------------------------

def test_order_history_save_failure_not_counted_as_success(monkeypatch, tmp_path):
    broker = FakeBroker()
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)
    # Broker accepts the order, but the local order_history write fails
    # (e.g. disk full / permission error) -- simulated by making the
    # post-submit status update report failure.
    monkeypatch.setattr(pso, "update_order_status", lambda *a, **k: False)

    result = pso.main(broker=broker)

    assert broker.submit_calls == [("AAPL", 1)]  # broker really did accept it
    assert result["submitted"] == []
    assert result["failed"] == ["AAPL"]


# ---------------------------------------------------------------------------
# 3. Slack failure must not flip the order-success judgment
# ---------------------------------------------------------------------------

def test_slack_failure_does_not_flip_order_success(monkeypatch, tmp_path):
    broker = FakeBroker()
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    def _raise_slack(message):
        raise requests.exceptions.ConnectionError("slack unreachable")

    monkeypatch.setattr(pso, "send_slack_alert", _raise_slack)

    result = pso.main(broker=broker)  # must not raise despite Slack failing

    assert result["submitted"] == ["AAPL"]
    assert result["failed"] == []
    history = pd.read_csv(tmp_path / "order_history.csv")
    assert history.loc[history["symbol"] == "AAPL", "status"].iloc[0] == "SUBMITTED"


def test_slack_failure_on_blocked_notification_does_not_stop_other_symbols(monkeypatch, tmp_path):
    broker = FakeBroker(positions=[{"symbol": "HELD", "market_value": 100.0}])
    _patch_common(monkeypatch, tmp_path, ["HELD", "OK"], broker)

    def _raise_slack(message):
        raise requests.exceptions.ConnectionError("slack unreachable")

    monkeypatch.setattr(pso, "send_slack_alert", _raise_slack)

    result = pso.main(broker=broker)  # must not raise

    assert result["blocked"] == ["HELD"]
    assert result["submitted"] == ["OK"]
    assert broker.submit_calls == [("OK", 1)]


# ---------------------------------------------------------------------------
# 4/5. Market data provider failures: symbol-level isolation
# ---------------------------------------------------------------------------

REGULAR_NOW = datetime(2026, 6, 15, 9, 45, tzinfo=ZoneInfo("America/New_York"))


def _good_snapshot(symbol="AAPL", price=100.0, previous_close=95.0, current_volume=5_000_000,
                    average_volume=1_000_000, atr=3.0, **overrides):
    kwargs = dict(symbol=symbol, price=price, previous_close=previous_close,
                  current_volume=current_volume, average_volume=average_volume, atr=atr)
    kwargs.update(overrides)
    return SymbolSnapshot(**kwargs)


def _patch_scalping_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(repository, "WATCHLIST_FILE", tmp_path / "scalping_watchlist.csv")
    monkeypatch.setattr(repository, "WATCHLIST_LOCK_FILE", tmp_path / "scalping_watchlist.lock")
    import scalping_watchlist.repeat_tracker as repeat_tracker_module
    monkeypatch.setattr(repeat_tracker_module, "REPEAT_STATE_FILE", tmp_path / "scalping_repeat_state.csv")
    monkeypatch.setattr(repeat_tracker_module, "REPEAT_STATE_LOCK_FILE", tmp_path / "scalping_repeat_state.lock")


def test_provider_missing_field_excludes_symbol_others_kept(monkeypatch, tmp_path):
    _patch_scalping_paths(monkeypatch, tmp_path)
    # BAD stands in for a 5xx/partial-JSON response: a required field
    # (average_volume) simply never arrived.
    provider = FakeMarketDataProvider(
        universe_symbols=["GOOD", "BAD"],
        snapshots={"GOOD": _good_snapshot(symbol="GOOD"), "BAD": _good_snapshot(symbol="BAD", average_volume=None)},
    )

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    assert [r["symbol"] for r in result["selected"]] == ["GOOD"]
    bad_row = next(r for r in result["rejected"] if r["symbol"] == "BAD")
    assert "AVERAGE_VOLUME_UNAVAILABLE" in bad_row["rejection_reasons"]


def test_provider_exception_for_symbol_does_not_stop_others(monkeypatch, tmp_path):
    _patch_scalping_paths(monkeypatch, tmp_path)
    provider = FakeMarketDataProvider(
        universe_symbols=["BOOM", "AAPL"],
        snapshots={"BOOM": requests.exceptions.Timeout("simulated API timeout"), "AAPL": _good_snapshot()},
    )

    result = run_scan_cycle(provider, now=REGULAR_NOW)

    # Both symbols were actually requested from the provider -- the
    # exception on the first did not abort evaluation of the rest.
    assert provider.requested_symbols == ["BOOM", "AAPL"]
    assert [r["symbol"] for r in result["selected"]] == ["AAPL"]
    bad_row = next(r for r in result["rejected"] if r["symbol"] == "BOOM")
    assert "PROVIDER_ERROR" in bad_row["rejection_reasons"]
