"""live_readiness/live_entry_pipeline.py unit/integration tests.

Uses a minimal fake broker (get_account() + submit_order()) -- no real
AlpacaBroker, no network. Isolated SQLite per test.
"""
from datetime import datetime, timezone

import pytest

from live_readiness import entry_reservation_ledger as ledger
from live_readiness import live_entry_pipeline as lep
from state_store import db as state_db

NOW = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate_state_db(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setattr(ledger, "_LOCK_FILE", tmp_path / "LIVE_ENTRY_RESERVATION.lock")
    yield


class _StubResponse:
    def __init__(self, status_code=200, data=None, dry_run=False):
        self.status_code = status_code
        self.data = data or {}
        self.dry_run = dry_run
        self.text = "ok"


class _FakeConfig:
    trading_mode = "live"


class _FakeBroker:
    def __init__(self, account, response=None, *, raises=None):
        self.account = account
        self.response = response or _StubResponse(200, {"live_entry_reservation_id": "resv-1"})
        self.raises = raises
        self.config = _FakeConfig()
        self.submit_calls = []
        self.account_calls = 0

    def get_account(self):
        self.account_calls += 1
        if self.raises is not None:
            raise self.raises
        return self.account

    def submit_order(self, symbol, *, qty, side, client_order_id, live_entry_context=None,
                      account_cash_snapshot=None):
        self.submit_calls.append((symbol, qty, side, client_order_id, account_cash_snapshot))
        return self.response


def _pipeline_kwargs(**overrides):
    defaults = dict(
        symbol="AAPL", strategy_id="strat-1", signal_id="sig-1", entry_price_usd=10.0,
        conn=None, fx_rate_krw_per_usd=1_350.0, fx_rate_as_of=NOW.isoformat(),
        allow_list=["AAPL"], now=NOW,
    )
    defaults.update(overrides)
    return defaults


def test_happy_path_calls_broker_once_with_sizing_qty():
    broker = _FakeBroker({"cash": "1000.00", "non_marginable_buying_power": "1000.00"})
    conn = state_db.open_db()
    result = lep.run_live_entry_pipeline(broker=broker, **_pipeline_kwargs(conn=conn))
    assert len(broker.submit_calls) == 1
    # Account Engine's build_account_snapshot() calls broker.get_account()
    # twice by design (once via fetch_account_cash_snapshot() for cash,
    # once for the non_marginable_buying_power field) -- see
    # account_engine.py's module docstring.
    assert broker.account_calls == 2
    assert result.reservation_id == "resv-1"
    symbol, qty, side, client_order_id, account_cash_snapshot = broker.submit_calls[0]
    assert symbol == "AAPL"
    assert side == "buy"
    assert qty > 0
    assert account_cash_snapshot is not None
    assert account_cash_snapshot.cash_krw == pytest.approx(1_000.0 * 1_350.0)


def test_account_engine_failure_blocks_zero_broker_calls():
    broker = _FakeBroker(None, raises=ConnectionError("network down"))
    conn = state_db.open_db()
    with pytest.raises(lep.LiveEntryPipelineError, match="Account Engine"):
        lep.run_live_entry_pipeline(broker=broker, **_pipeline_kwargs(conn=conn))
    assert broker.submit_calls == []


def test_missing_cash_field_blocks_zero_broker_calls():
    broker = _FakeBroker({})  # no "cash" field at all
    conn = state_db.open_db()
    with pytest.raises(lep.LiveEntryPipelineError, match="Account Engine"):
        lep.run_live_entry_pipeline(broker=broker, **_pipeline_kwargs(conn=conn))
    assert broker.submit_calls == []


def test_invalid_stop_price_blocks_zero_broker_calls():
    broker = _FakeBroker({"cash": "1000.00", "non_marginable_buying_power": "1000.00"})
    conn = state_db.open_db()
    with pytest.raises(lep.LiveEntryPipelineError, match="Risk Engine"):
        lep.run_live_entry_pipeline(
            broker=broker,
            **_pipeline_kwargs(conn=conn, stop_price_usd=10.0),  # == entry price -- no defined risk
        )
    assert broker.submit_calls == []


def test_nan_max_risk_per_trade_blocks_zero_broker_calls():
    broker = _FakeBroker({"cash": "1000.00", "non_marginable_buying_power": "1000.00"})
    conn = state_db.open_db()
    with pytest.raises(lep.LiveEntryPipelineError, match="Risk Engine"):
        lep.run_live_entry_pipeline(
            broker=broker,
            **_pipeline_kwargs(conn=conn, max_risk_per_trade_krw=float("nan")),
        )
    assert broker.submit_calls == []


def test_nan_strategy_max_qty_blocks_zero_broker_calls():
    broker = _FakeBroker({"cash": "1000.00", "non_marginable_buying_power": "1000.00"})
    conn = state_db.open_db()
    with pytest.raises(lep.LiveEntryPipelineError, match="Sizing Engine"):
        lep.run_live_entry_pipeline(
            broker=broker,
            **_pipeline_kwargs(conn=conn, strategy_max_qty=float("nan")),
        )
    assert broker.submit_calls == []


def test_insufficient_cash_blocks_with_no_affordable_quantity():
    broker = _FakeBroker({"cash": "0.01", "non_marginable_buying_power": "0.01"})
    conn = state_db.open_db()
    with pytest.raises(lep.LiveEntryPipelineError):
        lep.run_live_entry_pipeline(broker=broker, **_pipeline_kwargs(conn=conn))
    assert broker.submit_calls == []


def test_non_fractionable_too_expensive_blocked_by_affordability_zero_broker_calls():
    # Tiny balance, expensive non-fractionable symbol -- balance-based
    # sizing alone already produces 0, so this is blocked upstream at
    # Sizing Engine (both are legitimate fail-closed outcomes; the key
    # invariant is zero broker calls either way).
    broker = _FakeBroker({"cash": "1.00", "non_marginable_buying_power": "1.00"})
    conn = state_db.open_db()
    with pytest.raises(lep.LiveEntryPipelineError):
        lep.run_live_entry_pipeline(
            broker=broker,
            **_pipeline_kwargs(conn=conn, entry_price_usd=1_000_000.0, fractionable=False),
        )
    assert broker.submit_calls == []


def test_client_order_id_defaulted_when_not_supplied():
    broker = _FakeBroker({"cash": "1000.00", "non_marginable_buying_power": "1000.00"})
    conn = state_db.open_db()
    lep.run_live_entry_pipeline(broker=broker, **_pipeline_kwargs(conn=conn))
    coid = broker.submit_calls[0][3]
    assert coid.startswith("liveentry-AAPL-")


def test_conflicting_existing_reservation_blocks_zero_broker_calls():
    conn = state_db.open_db()
    ledger.reserve(conn, "MSFT", 5_000.0, "coid-conflict")
    broker = _FakeBroker({"cash": "1000.00", "non_marginable_buying_power": "1000.00"})
    with pytest.raises(lep.LiveEntryPipelineError, match="Execution Engine"):
        lep.run_live_entry_pipeline(
            broker=broker,
            **_pipeline_kwargs(conn=conn, symbol="AAPL", client_order_id="coid-conflict"),
        )
    assert broker.submit_calls == []


def test_strategy_max_qty_cannot_widen_beyond_balance():
    # cash $27 * 1,350 KRW/$ = 36,450 KRW effective cash; trusted 50% ->
    # 18,225 KRW allocatable -> $13.50 budget -> floor($13.50/$10) = 1 share.
    broker = _FakeBroker({"cash": "27.00", "non_marginable_buying_power": "27.00"})
    conn = state_db.open_db()
    result = lep.run_live_entry_pipeline(
        broker=broker,
        **_pipeline_kwargs(conn=conn, entry_price_usd=10.0, strategy_max_qty=1_000_000),
    )
    qty = broker.submit_calls[0][1]
    assert qty <= 1  # balance-based ceiling still binds despite the huge strategy cap
