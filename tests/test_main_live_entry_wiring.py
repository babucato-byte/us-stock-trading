"""CODEX-040: runtime integration tests proving paper_strategy_order.main()
actually routes live-mode entries through Account -> Risk -> Sizing ->
Affordability -> Execution Engine, never the legacy submit_order()/direct
broker path -- and that Paper-mode main() behavior is completely
unaffected.
"""
from datetime import datetime, timezone

import pandas as pd
import pytest

import paper_strategy_order as pso
from live_readiness import account_engine, execution_engine, live_entry_pipeline, risk_engine, sizing_engine
from live_readiness import entry_reservation_ledger as ledger
from live_readiness.watchlist_affordability import evaluate_affordability

import tests.test_paper_order_execution as paper_tests  # reuse _high_score_result/_write_history helpers


@pytest.fixture(autouse=True)
def _isolate_state_db(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setattr(ledger, "_LOCK_FILE", tmp_path / "LIVE_ENTRY_RESERVATION.lock")
    yield


class _LiveConfig:
    status_label = "LIVE_DRY_RUN"
    is_live_mode = True
    trading_mode = "live"


class _StubResponse:
    def __init__(self, status_code=200, data=None, dry_run=False, text="ok"):
        self.status_code = status_code
        self.data = data or {}
        self.dry_run = dry_run
        self.text = text


class LiveFakeBroker:
    """A live-mode broker double whose submit_order() accepts the full
    modern signature (live_entry_context/account_cash_snapshot), so the
    engine pipeline's actual call shape is exercised end to end."""

    def __init__(self, cash_usd="1000.00", response=None):
        self.config = _LiveConfig()
        self._account = {"equity": "10000", "last_equity": "10000",
                          "cash": cash_usd, "non_marginable_buying_power": cash_usd}
        self._positions = []
        self.response = response or _StubResponse(200, {"live_entry_reservation_id": "resv-live-1"})
        self.submit_calls = []
        self.get_account_calls = 0

    def get_account(self):
        self.get_account_calls += 1
        return self._account

    def get_positions(self):
        return self._positions

    def submit_order(self, symbol, qty=1, *, side, client_order_id=None,
                      live_entry_context=None, account_cash_snapshot=None):
        self.submit_calls.append((symbol, qty, side, client_order_id, live_entry_context,
                                   account_cash_snapshot))
        return self.response


def _patch_live_common(monkeypatch, tmp_path, tickers, broker, market_session="regular"):
    monkeypatch.setattr(pso, "load_watchlist", lambda: tickers)
    monkeypatch.setattr(pso, "analyze_stock", paper_tests._high_score_result)
    monkeypatch.setattr(pso, "get_us_market_session", lambda: market_session)
    monkeypatch.setattr(pso, "ORDER_HISTORY_FILE", tmp_path / "order_history.csv")
    monkeypatch.setattr(pso, "ORDER_HISTORY_LOCK_FILE", tmp_path / "order_history.lock")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_FILE", tmp_path / "order_reconciliation.csv")
    monkeypatch.setattr(pso, "ORDER_RECONCILIATION_LOCK_FILE", tmp_path / "order_reconciliation.lock")
    pso.initialize_order_history()
    slack_calls = []
    monkeypatch.setattr(pso, "send_slack_alert", lambda msg: slack_calls.append(msg) or True)
    monkeypatch.setenv("LIVE_FX_RATE_KRW_PER_USD", "1350.0")
    monkeypatch.setenv("LIVE_ENTRY_ALLOW_LIST", "AAPL")
    return slack_calls


def _spy(monkeypatch, module, func_name):
    calls = []
    original = getattr(module, func_name)

    def _wrapped(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(module, func_name, _wrapped)
    return calls


def test_live_mode_main_routes_through_all_four_engines_exactly_once(monkeypatch, tmp_path):
    broker = LiveFakeBroker()
    _patch_live_common(monkeypatch, tmp_path, ["AAPL"], broker)

    account_calls = _spy(monkeypatch, account_engine, "build_account_snapshot")
    risk_calls = _spy(monkeypatch, risk_engine, "compute_risk_decision")
    sizing_calls = _spy(monkeypatch, sizing_engine, "compute_sizing_decision")
    execution_calls = _spy(monkeypatch, execution_engine, "submit_validated_command")

    result = pso.main(broker=broker)

    assert len(account_calls) == 1
    assert len(risk_calls) == 1
    assert len(sizing_calls) == 1
    assert len(execution_calls) == 1
    assert len(broker.submit_calls) == 1
    assert result["submitted"] == ["AAPL"]


def test_live_mode_main_never_calls_legacy_submit_order_wrapper(monkeypatch, tmp_path):
    broker = LiveFakeBroker()
    _patch_live_common(monkeypatch, tmp_path, ["AAPL"], broker)

    legacy_calls = _spy(monkeypatch, pso, "submit_order")
    pso.main(broker=broker)
    assert legacy_calls == []
    assert len(broker.submit_calls) == 1


def test_live_mode_affordability_actually_evaluated(monkeypatch, tmp_path):
    broker = LiveFakeBroker()
    _patch_live_common(monkeypatch, tmp_path, ["AAPL"], broker)

    affordability_calls = []
    original = evaluate_affordability

    def _wrapped(*args, **kwargs):
        affordability_calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(live_entry_pipeline, "evaluate_affordability", _wrapped)
    pso.main(broker=broker)
    assert len(affordability_calls) == 1


def test_account_engine_failure_blocks_zero_broker_calls(monkeypatch, tmp_path):
    broker = LiveFakeBroker(cash_usd="1000.00")
    # Non-empty (so account_risk.check_daily_loss_limit()'s `account or
    # get_account()` doesn't treat it as falsy and fall back to a fresh
    # real AlpacaBroker() from env) but missing "cash" -- Account Engine
    # must fail closed on this.
    broker._account = {"equity": "10000", "last_equity": "10000"}
    _patch_live_common(monkeypatch, tmp_path, ["AAPL"], broker)

    result = pso.main(broker=broker)
    assert broker.submit_calls == []
    assert result["blocked"] == ["AAPL"]


def test_missing_fx_rate_blocks_zero_broker_calls(monkeypatch, tmp_path):
    broker = LiveFakeBroker()
    _patch_live_common(monkeypatch, tmp_path, ["AAPL"], broker)
    monkeypatch.delenv("LIVE_FX_RATE_KRW_PER_USD", raising=False)

    result = pso.main(broker=broker)
    assert broker.submit_calls == []
    assert result["blocked"] == ["AAPL"]


def test_symbol_not_in_allow_list_blocks_zero_session_calls(monkeypatch, tmp_path):
    # The allow-list gate itself lives inside order_gateway.py, reached
    # only via a REAL AlpacaBroker -- LiveFakeBroker (used elsewhere in
    # this file) doesn't replicate that gate, so this specific check
    # needs the real broker class + a network-forbidding session double,
    # same pattern as tests/test_live_order_gateway.py.
    from broker import AlpacaBroker, BrokerConfig

    class _NetworkForbiddenSession:
        def __init__(self):
            self.requests = []

        def request(self, *args, **kwargs):
            self.requests.append((args, kwargs))
            raise AssertionError("No network call should ever be made for a blocked live entry")

    session = _NetworkForbiddenSession()
    broker = AlpacaBroker(
        config=BrokerConfig(trading_mode="live", enable_real_trading=True, live_dry_run=False,
                             api_key="key", secret_key="secret"),
        session=session,
    )
    monkeypatch.setattr(broker, "get_account",
                         lambda: {"equity": "10000", "last_equity": "10000",
                                  "cash": "1000.00", "non_marginable_buying_power": "1000.00"})
    monkeypatch.setattr(broker, "get_positions", lambda: [])
    _patch_live_common(monkeypatch, tmp_path, ["AAPL"], broker)
    monkeypatch.setenv("LIVE_ENTRY_ALLOW_LIST", "MSFT")  # AAPL not on the list

    result = pso.main(broker=broker)
    assert session.requests == []
    # order_gateway.py's allow-list gate returns a 423 BrokerResponse
    # rather than raising -- main()'s existing status_code-based success/
    # failure classification (unchanged by this cycle) records that as
    # "failed", not "blocked". The invariant that matters here is zero
    # network calls, already asserted above.
    assert result["failed"] == ["AAPL"]
    assert result["submitted"] == []


def test_insufficient_cash_blocks_zero_broker_calls(monkeypatch, tmp_path):
    broker = LiveFakeBroker(cash_usd="0.01")
    _patch_live_common(monkeypatch, tmp_path, ["AAPL"], broker)

    result = pso.main(broker=broker)
    assert broker.submit_calls == []
    assert result["blocked"] == ["AAPL"]


def test_conflicting_reservation_blocks_zero_broker_calls(monkeypatch, tmp_path):
    conn_for_seed = None
    from state_store import db as state_db
    conn_for_seed = state_db.open_db()

    broker = LiveFakeBroker()
    _patch_live_common(monkeypatch, tmp_path, ["AAPL"], broker)

    # Force try_reserve_order() to mint a KNOWN client_order_id by
    # controlling uuid4 output, then seed a conflicting SQLite reservation
    # for that same id under a different symbol.
    import uuid as uuid_module

    class _FixedUUID:
        hex = "f" * 32

    monkeypatch.setattr(uuid_module, "uuid4", lambda: _FixedUUID())
    ledger.reserve(conn_for_seed, "MSFT", 5_000.0, f"scalp-AAPL-{pso.eastern_now().strftime('%Y-%m-%d')}-ffffffffff")

    result = pso.main(broker=broker)
    assert broker.submit_calls == []
    assert result["blocked"] == ["AAPL"]


def test_paper_mode_main_completely_unaffected_by_live_wiring(monkeypatch, tmp_path):
    # Regression guard: Paper mode (no is_live_mode attribute, matching
    # every existing test_paper_order_execution.py FakeBroker) must never
    # touch the new engines at all.
    broker = paper_tests.FakeBroker()
    paper_tests._patch_common(monkeypatch, tmp_path, ["AAPL"], broker)

    account_calls = _spy(monkeypatch, account_engine, "build_account_snapshot")
    execution_calls = _spy(monkeypatch, execution_engine, "submit_validated_command")

    result = pso.main(broker=broker)

    assert account_calls == []
    assert execution_calls == []
    assert broker.submit_calls == [("AAPL", 1)]
    assert result["submitted"] == ["AAPL"]
