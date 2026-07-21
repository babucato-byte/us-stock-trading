"""Restart-safety tests for the order_intent_ledger two-phase (reserve ->
commit/abort) protocol, and its wiring into paper_strategy_order.py.

All state lives on disk under tmp_path; nothing here ever touches the real
order_history.csv or order_intent_ledger.csv in the project directory.
"""

import pandas as pd
import pytest
import requests

import order_intent_ledger as oil
import paper_strategy_order as pso


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
    """Minimal broker double: no real Alpaca/HTTP calls, fully scripted responses."""

    def __init__(self, account=None, positions=None, submit_side_effects=None,
                 default_response=None, orders_by_client_id=None):
        self.config = FakeConfig()
        self._account = account or {"equity": "10000", "last_equity": "10000"}
        self._positions = positions or []
        self._submit_side_effects = submit_side_effects or {}
        self._default_response = default_response or FakeBrokerResponse(status_code=200, text="OK", dry_run=False)
        self._orders_by_client_id = orders_by_client_id or {}
        self.submit_calls = []
        self.client_order_ids = []

    def get_account(self):
        return self._account

    def get_positions(self):
        return self._positions

    def submit_order(self, symbol, qty=1, client_order_id=None):
        self.submit_calls.append((symbol, qty))
        self.client_order_ids.append(client_order_id)
        effect = self._submit_side_effects.get(symbol)
        if isinstance(effect, Exception):
            raise effect
        return effect or self._default_response

    def get_order_by_client_order_id(self, client_order_id):
        return self._orders_by_client_id.get(client_order_id)


class FlakyOnceBroker(FakeBroker):
    """Like FakeBroker, but a chosen symbol raises a Timeout on its first
    submit_order() call and succeeds on every call after that -- used to
    prove a same-day retry actually reaches submit_order() a second time
    through the real pso.main() order path, not just via a direct
    order_intent_ledger.reserve() call.
    """

    def __init__(self, flaky_symbol, **kwargs):
        super().__init__(**kwargs)
        self._flaky_symbol = flaky_symbol
        self._flaky_calls = 0

    def submit_order(self, symbol, qty=1, client_order_id=None):
        if symbol != self._flaky_symbol:
            return super().submit_order(symbol, qty=qty, client_order_id=client_order_id)
        self._flaky_calls += 1
        self.submit_calls.append((symbol, qty))
        self.client_order_ids.append(client_order_id)
        if self._flaky_calls == 1:
            raise requests.exceptions.Timeout("timed out")
        return self._default_response


def _high_score_result(symbol):
    return {
        "symbol": symbol,
        "price": 100.0,
        "ma200": 90.0,
        "rsi": 50.0,
        "volume_ratio": 1.5,
        "score": 100,
    }


def _patch_common(monkeypatch, tmp_path, tickers, broker, init_history=True):
    monkeypatch.setattr(pso, "load_watchlist", lambda: tickers)
    monkeypatch.setattr(pso, "analyze_stock", _high_score_result)
    monkeypatch.setattr(pso, "get_us_market_session", lambda: "regular")
    monkeypatch.setattr(pso, "ORDER_HISTORY_FILE", tmp_path / "order_history.csv")
    monkeypatch.setattr(pso, "ORDER_HISTORY_LOCK_FILE", tmp_path / "order_history.lock")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_FILE", tmp_path / "order_reconciliation.csv")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_LOCK_FILE", tmp_path / "order_reconciliation.lock")
    if init_history:
        pso.initialize_order_history()
    slack_calls = []
    monkeypatch.setattr(pso, "send_slack_alert", lambda msg: slack_calls.append(msg) or True)
    return slack_calls


# ---------------------------------------------------------------------------
# (1) crash right after reservation (never committed, never aborted) blocks
#     a second submission on the next run
# ---------------------------------------------------------------------------

def test_reserved_but_unresolved_intent_blocks_resubmission_after_restart(monkeypatch, tmp_path):
    broker = FakeBroker()
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    ledger_path = tmp_path / "order_intent_ledger.csv"
    lock_path = tmp_path / "order_intent_ledger.lock"
    stale_client_order_id = "scalp-AAPL-crash"
    # Simulate a prior run that reserved and wrote the order_history
    # PENDING_SUBMISSION row, then died before ever calling the broker (or
    # before recording the outcome) -- neither commit() nor abort() ran.
    oil.reserve(ledger_path, lock_path, "AAPL", TODAY, client_order_id=stale_client_order_id)
    pd.DataFrame(
        [{"symbol": "AAPL", "order_date": TODAY, "mode": "PAPER", "dry_run": False, "status": "PENDING_SUBMISSION"}],
        columns=pso.REQUIRED_HISTORY_COLUMNS,
    ).to_csv(tmp_path / "order_history.csv", index=False)

    pso.main(broker=broker)  # "restart"

    assert broker.submit_calls == []
    with pytest.raises(oil.DuplicateIntentError):
        oil.reserve(ledger_path, lock_path, "AAPL", TODAY)


def test_reserve_without_broker_fails_closed_on_stale_reservation(tmp_path):
    ledger_path = tmp_path / "order_intent_ledger.csv"
    lock_path = tmp_path / "order_intent_ledger.lock"
    oil.reserve(ledger_path, lock_path, "AAPL", TODAY, client_order_id="intent-1")

    with pytest.raises(oil.DuplicateIntentError):
        oil.reserve(ledger_path, lock_path, "AAPL", TODAY)

    ledger = pd.read_csv(ledger_path)
    assert ledger.loc[ledger["client_order_id"] == "intent-1", "state"].iloc[0] == "RESERVED"


def test_reserve_confirmed_at_broker_blocks_and_upgrades_stale_reservation(tmp_path):
    ledger_path = tmp_path / "order_intent_ledger.csv"
    lock_path = tmp_path / "order_intent_ledger.lock"
    oil.reserve(ledger_path, lock_path, "AAPL", TODAY, client_order_id="intent-1")
    broker = FakeBroker(orders_by_client_id={"intent-1": {"status": "filled"}})

    with pytest.raises(oil.DuplicateIntentError):
        oil.reserve(ledger_path, lock_path, "AAPL", TODAY, broker=broker)

    ledger = pd.read_csv(ledger_path)
    assert ledger.loc[ledger["client_order_id"] == "intent-1", "state"].iloc[0] == "COMMITTED"


def test_reserve_broker_miss_on_stale_reservation_still_fails_closed(tmp_path):
    # The broker not recognizing the client_order_id is not proof the order
    # was never received (e.g. eventual consistency, a lookup edge case) --
    # this project's standing policy is to never auto-resubmit an ambiguous
    # order, so a miss must still block, not silently permit a retry.
    ledger_path = tmp_path / "order_intent_ledger.csv"
    lock_path = tmp_path / "order_intent_ledger.lock"
    oil.reserve(ledger_path, lock_path, "AAPL", TODAY, client_order_id="intent-1")
    broker = FakeBroker()  # orders_by_client_id empty -> lookup returns None

    with pytest.raises(oil.DuplicateIntentError):
        oil.reserve(ledger_path, lock_path, "AAPL", TODAY, broker=broker)


# ---------------------------------------------------------------------------
# (2) a committed order is never resubmitted after restart
# ---------------------------------------------------------------------------

def test_committed_intent_blocks_resubmission_after_restart(monkeypatch, tmp_path):
    broker = FakeBroker()
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    pso.main(broker=broker)  # first "run": reserves, submits, commits
    assert broker.submit_calls == [("AAPL", 1)]

    ledger = pd.read_csv(tmp_path / "order_intent_ledger.csv")
    assert ledger.loc[ledger["client_order_id"] == broker.client_order_ids[0], "state"].iloc[0] == "COMMITTED"

    broker.submit_calls.clear()
    pso.main(broker=broker)  # "restart": must not resubmit

    assert broker.submit_calls == []
    with pytest.raises(oil.DuplicateIntentError):
        oil.reserve(tmp_path / "order_intent_ledger.csv", tmp_path / "order_intent_ledger.lock", "AAPL", TODAY)


# ---------------------------------------------------------------------------
# (3) a reservation explicitly aborted after a submission failure allows a
#     fresh reservation for the same (symbol, trade_date) on a later run
# ---------------------------------------------------------------------------

def test_aborted_intent_after_submission_failure_allows_retry(tmp_path):
    ledger_path = tmp_path / "order_intent_ledger.csv"
    lock_path = tmp_path / "order_intent_ledger.lock"
    oil.reserve(ledger_path, lock_path, "AAPL", TODAY, client_order_id="intent-1")
    oil.abort(ledger_path, lock_path, "intent-1")

    new_id = oil.reserve(ledger_path, lock_path, "AAPL", TODAY, client_order_id="intent-2")

    assert new_id == "intent-2"
    ledger = pd.read_csv(ledger_path)
    assert ledger.loc[ledger["client_order_id"] == "intent-1", "state"].iloc[0] == "ABORTED"
    assert ledger.loc[ledger["client_order_id"] == "intent-2", "state"].iloc[0] == "RESERVED"


def test_submission_failure_aborts_ledger_via_paper_strategy_order(monkeypatch, tmp_path):
    broker = FakeBroker(submit_side_effects={"AAPL": requests.exceptions.Timeout("timed out")})
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    pso.main(broker=broker)  # submission fails -> history=SUBMISSION_FAILED, ledger=ABORTED

    assert broker.submit_calls == [("AAPL", 1)]
    history = pd.read_csv(tmp_path / "order_history.csv")
    assert history.loc[history["symbol"] == "AAPL", "status"].iloc[0] == "SUBMISSION_FAILED"

    ledger = pd.read_csv(tmp_path / "order_intent_ledger.csv")
    aborted_id = broker.client_order_ids[0]
    assert ledger.loc[ledger["client_order_id"] == aborted_id, "state"].iloc[0] == "ABORTED"

    # The specific guarantee this ledger adds: a fresh reservation for the
    # same (symbol, trade_date) is allowed once the failed attempt was
    # explicitly aborted -- unlike the still-RESERVED case in scenario (1).
    retry_id = oil.reserve(
        tmp_path / "order_intent_ledger.csv", tmp_path / "order_intent_ledger.lock",
        "AAPL", TODAY, client_order_id="retry-1",
    )
    assert retry_id == "retry-1"


def test_rejected_response_still_commits_and_blocks_retry(monkeypatch, tmp_path):
    # Unlike a network-level submission failure, a definitive (even
    # rejected) broker response means the client_order_id was consumed --
    # this must commit, not abort, so it is never mistaken for a safe retry.
    broker = FakeBroker(
        submit_side_effects={"AAPL": FakeBrokerResponse(status_code=422, text="rejected", dry_run=False)}
    )
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    pso.main(broker=broker)

    history = pd.read_csv(tmp_path / "order_history.csv")
    assert history.loc[history["symbol"] == "AAPL", "status"].iloc[0] == "REJECTED"

    ledger = pd.read_csv(tmp_path / "order_intent_ledger.csv")
    rejected_id = broker.client_order_ids[0]
    assert ledger.loc[ledger["client_order_id"] == rejected_id, "state"].iloc[0] == "COMMITTED"

    with pytest.raises(oil.DuplicateIntentError):
        oil.reserve(tmp_path / "order_intent_ledger.csv", tmp_path / "order_intent_ledger.lock", "AAPL", TODAY)


def test_submission_failure_retry_actually_resubmits_through_main(monkeypatch, tmp_path):
    """End-to-end version of scenario (3): an abort() after a submission
    failure must let the *real* pso.main() order path resubmit on the very
    next run -- not just a direct order_intent_ledger.reserve() call.

    This is the exact gap a prior review found: is_duplicate_order() matched
    on (symbol, order_date) alone, so the SUBMISSION_FAILED row left in
    order_history.csv by the first run kept blocking every later run's
    try_reserve_order(), even though the ledger itself had already recorded
    that attempt as safely ABORTED.
    """
    broker = FlakyOnceBroker("AAPL")
    _patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    pso.main(broker=broker)  # first run: Timeout -> history=SUBMISSION_FAILED, ledger=ABORTED

    assert broker.submit_calls == [("AAPL", 1)]
    history = pd.read_csv(tmp_path / "order_history.csv")
    assert (history["symbol"] == "AAPL").sum() == 1
    assert history.loc[history["symbol"] == "AAPL", "status"].iloc[0] == "SUBMISSION_FAILED"

    pso.main(broker=broker)  # "restart": the retry must actually reach submit_order()

    assert broker.submit_calls == [("AAPL", 1), ("AAPL", 1)]
    history = pd.read_csv(tmp_path / "order_history.csv")
    # Still exactly one row for AAPL/TODAY: the stale SUBMISSION_FAILED row
    # was replaced by the retry's reservation, not appended alongside it.
    assert (history["symbol"] == "AAPL").sum() == 1
    assert history.loc[history["symbol"] == "AAPL", "status"].iloc[0] == "SUBMITTED"

    ledger = pd.read_csv(tmp_path / "order_intent_ledger.csv")
    first_id, second_id = broker.client_order_ids
    assert ledger.loc[ledger["client_order_id"] == first_id, "state"].iloc[0] == "ABORTED"
    assert ledger.loc[ledger["client_order_id"] == second_id, "state"].iloc[0] == "COMMITTED"
