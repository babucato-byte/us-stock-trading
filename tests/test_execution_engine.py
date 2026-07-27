"""live_readiness/execution_engine.py unit tests.

Uses a minimal fake broker (not a real AlpacaBroker) since
ExecutionEngine only cares about the broker.submit_order(...) contract
duck-typed -- kill switch/credential plumbing is broker/alpaca_client.py's
own concern, already covered by tests/test_live_order_gateway.py etc.
"""
import glob
import os
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from live_readiness import entry_reservation_ledger as ledger
from live_readiness import execution_engine as ee
from live_readiness import sizing_engine as se
from live_readiness.order_gateway import LiveEntryContext
from state_store import db as state_db

NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate_state_db(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setattr(ledger, "_LOCK_FILE", tmp_path / "LIVE_ENTRY_RESERVATION.lock")
    yield


class _StubResponse:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self.data = data or {}


class _FakeBroker:
    """No account_cash_snapshot parameter at all -- proves
    submit_validated_command() never breaks an older duck-typed broker
    double when account_cash_snapshot is omitted."""

    def __init__(self, response=None):
        self.calls = []
        self.response = response or _StubResponse(200, {"live_entry_reservation_id": "resv-1"})

    def submit_order(self, symbol, *, qty, side, client_order_id, live_entry_context=None):
        self.calls.append((symbol, qty, side, client_order_id))
        return self.response


class _FakeBrokerWithSnapshotSupport:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or _StubResponse(200, {"live_entry_reservation_id": "resv-1"})

    def submit_order(self, symbol, *, qty, side, client_order_id, live_entry_context=None,
                      account_cash_snapshot=None):
        self.calls.append((symbol, qty, side, client_order_id, account_cash_snapshot))
        return self.response


def _sizing_decision(actual_qty=1, price=10.0):
    return se.SizingDecision(
        sizing_decision_id="sizing-1",
        actual_qty=actual_qty,
        balance_based_qty=actual_qty,
        risk_based_qty=actual_qty,
        strategy_max_qty=None,
        buffered_entry_price_usd=price,
        below_minimum_order=False,
    )


def _command(**overrides):
    defaults = dict(
        signal_id="sig-1", strategy_id="strat-1", symbol="AAPL", side="buy", purpose="ENTRY_ORDER",
        sizing_decision=_sizing_decision(), account_snapshot_id="acct-1", risk_decision_id="risk-1",
        client_order_id="coid-exec-1", now=NOW,
    )
    defaults.update(overrides)
    return ee.build_validated_order_command(**defaults)


def _ctx(symbol="AAPL"):
    return LiveEntryContext(
        symbol=symbol, expected_fill_price_usd=10.0, allow_list=[symbol],
        available_cash_krw=30_000, cash_usage_percent=50, cash_as_of=NOW.isoformat(),
        fx_rate_krw_per_usd=1_350.0, fx_rate_as_of=NOW.isoformat(),
        max_position_count=1, max_daily_entries=2, now=NOW,
    )


def test_valid_command_submits_once_and_returns_reservation_id():
    broker = _FakeBroker()
    command = _command()
    result = ee.submit_validated_command(command, broker, _ctx(), now=NOW)
    assert len(broker.calls) == 1
    assert broker.calls[0] == ("AAPL", 1, "buy", "coid-exec-1")
    assert result.reservation_id == "resv-1"
    assert result.broker_response.status_code == 200


def test_non_command_object_blocked_zero_broker_calls():
    broker = _FakeBroker()
    with pytest.raises(ee.ExecutionEngineError, match="ValidatedOrderCommand"):
        ee.submit_validated_command({"symbol": "AAPL", "qty": 1}, broker, _ctx(), now=NOW)
    assert broker.calls == []


def test_expired_command_blocked_zero_broker_calls():
    broker = _FakeBroker()
    command = _command(ttl_seconds=1, now=NOW - timedelta(seconds=10))
    with pytest.raises(ee.ExecutionEngineError, match="expired"):
        ee.submit_validated_command(command, broker, _ctx(), now=NOW)
    assert broker.calls == []


def test_mutated_notional_blocked_zero_broker_calls():
    broker = _FakeBroker()
    command = _command()
    mutated = replace(command, qty=1_000_000)  # qty changed after construction
    with pytest.raises(ee.ExecutionEngineError, match="mutation"):
        ee.submit_validated_command(mutated, broker, _ctx(), now=NOW)
    assert broker.calls == []


def test_symbol_mutation_blocked_zero_broker_calls():
    broker = _FakeBroker()
    command = _command()
    mutated = replace(command, symbol="TSLA", estimated_notional=command.estimated_notional)
    with pytest.raises(ee.ExecutionEngineError, match="symbol"):
        ee.submit_validated_command(mutated, broker, _ctx(), now=NOW)
    assert broker.calls == []


def test_live_entry_context_symbol_mismatch_blocked_zero_broker_calls():
    broker = _FakeBroker()
    command = _command(symbol="AAPL")
    with pytest.raises(ee.ExecutionEngineError, match="live_entry_context"):
        ee.submit_validated_command(command, broker, _ctx(symbol="MSFT"), now=NOW)
    assert broker.calls == []


def test_existing_reservation_symbol_mismatch_blocked_zero_broker_calls():
    conn = state_db.open_db()
    ledger.reserve(conn, "MSFT", 5_000.0, "coid-exec-2")
    broker = _FakeBroker()
    command = _command(symbol="AAPL", client_order_id="coid-exec-2")
    with pytest.raises(ee.ExecutionEngineError, match="does not match"):
        ee.submit_validated_command(command, broker, _ctx(), now=NOW)
    assert broker.calls == []


def test_existing_reservation_matching_symbol_allows_submission():
    conn = state_db.open_db()
    ledger.reserve(conn, "AAPL", 5_000.0, "coid-exec-3")
    broker = _FakeBroker()
    command = _command(symbol="AAPL", client_order_id="coid-exec-3")
    result = ee.submit_validated_command(command, broker, _ctx(), now=NOW)
    assert len(broker.calls) == 1


def test_no_existing_reservation_allows_submission():
    broker = _FakeBroker()
    command = _command(client_order_id="coid-brand-new")
    ee.submit_validated_command(command, broker, _ctx(), now=NOW)
    assert len(broker.calls) == 1


# ---------------------------------------------------------------------------
# CODEX-036/040: account_cash_snapshot forwarding.
# ---------------------------------------------------------------------------

def test_account_cash_snapshot_forwarded_when_supplied():
    broker = _FakeBrokerWithSnapshotSupport()
    command = _command(client_order_id="coid-snap-1")
    snapshot = object()  # opaque sentinel -- forwarding is structural, not type-specific here
    ee.submit_validated_command(command, broker, _ctx(), now=NOW, account_cash_snapshot=snapshot)
    assert broker.calls[0][4] is snapshot


def test_no_account_cash_snapshot_supplied_does_not_break_older_broker_double():
    # _FakeBroker's submit_order() has no account_cash_snapshot parameter
    # at all -- omitting it (the default) must never pass it through and
    # raise a TypeError.
    broker = _FakeBroker()
    command = _command(client_order_id="coid-snap-2")
    result = ee.submit_validated_command(command, broker, _ctx(), now=NOW)
    assert len(broker.calls) == 1
    assert result.reservation_id == "resv-1"


# ---------------------------------------------------------------------------
# Architecture boundary guard: only execution_engine.py and the
# grandfathered legacy compat path (broker/alpaca_client.py itself,
# paper_strategy_order.py) may call `.submit_order(`.
# ---------------------------------------------------------------------------

_ALLOWED_SUBMIT_ORDER_CALLERS = {
    "broker/alpaca_client.py",       # broker's own internal definition/self-reference
    "paper_strategy_order.py",       # grandfathered legacy compat path (see its docstring)
    "live_readiness/execution_engine.py",
}


def test_only_execution_engine_and_legacy_compat_call_broker_submit_order():
    """Only a lowercase `broker`/`self.broker` variable call
    (`broker.submit_order(...)`) is treated as an actual broker call site
    -- docstring/comment prose referencing the class name
    (`AlpacaBroker.submit_order()`) or the sanctioned wrapper facade
    (`paper_strategy_order.submit_order()`, itself allowed as legacy
    compat) don't match this pattern."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    offenders = []
    pattern = re.compile(r"(?<![.\w])(?:self\.)?broker\.submit_order\s*\(")
    for path in glob.glob(os.path.join(repo_root, "**", "*.py"), recursive=True):
        rel_path = os.path.relpath(path, repo_root)
        if rel_path.startswith("venv") or rel_path.startswith("tests") or "/venv/" in rel_path:
            continue
        if rel_path in _ALLOWED_SUBMIT_ORDER_CALLERS:
            continue
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if pattern.search(line):
                    offenders.append(f"{rel_path}:{lineno}")
    assert offenders == [], f"unexpected broker.submit_order( call sites: {offenders}"
