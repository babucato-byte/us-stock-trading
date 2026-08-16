"""CODEX-044: the reconciliation snapshot -- collection, judgement and
fail-closed verification.

The gate-level and pipeline-level consequences are tested in
tests/test_order_gate.py, tests/test_kis_live_trading.py and
tests/test_kis_broker_adapter.py; this file covers the snapshot itself.
"""
from datetime import datetime, timedelta, timezone

import pytest

from domain.position import Position
from execution import idempotency
from reconciliation.snapshot import (
    ReconciliationBlockedError,
    ReconciliationSnapshot,
    ReconciliationUnavailableError,
    build_snapshot,
    max_age_seconds,
    verify_snapshot,
)
from state_store import db as state_db

NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
ACCOUNT_ID = "12345678"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setattr(idempotency, "_LOCK_FILE", tmp_path / "KIS_ORDER_IDEMPOTENCY.lock")
    monkeypatch.delenv("RECONCILIATION_MAX_AGE_SECONDS", raising=False)
    yield


def _conn():
    return state_db.open_db()


class _FakeBroker:
    def __init__(self, positions=None, open_orders=None, fills=None, fail=None):
        self.positions = positions or []
        self.open_orders = open_orders or []
        self.fills = fills or []
        self.fail = fail or set()

    def get_positions(self):
        if "positions" in self.fail:
            raise RuntimeError("KIS positions unavailable")
        return self.positions

    def get_open_orders(self):
        if "open_orders" in self.fail:
            raise RuntimeError("KIS open orders unavailable")
        return self.open_orders

    def get_fills(self, *, start_date, end_date):
        if "fills" in self.fail:
            raise RuntimeError("KIS fills unavailable")
        return self.fills


def _kis_position(symbol="AAPL", qty=5):
    return Position(symbol=symbol, quantity=qty, average_fill_price=100.0, unrealized_pnl=0.0,
                    realized_pnl=0.0, as_of=NOW, source="kis_balance")


def _internal_position(symbol="AAPL", qty=5):
    return Position(symbol=symbol, quantity=qty, average_fill_price=100.0, unrealized_pnl=0.0,
                    realized_pnl=0.0, as_of=NOW, source="internal_store")


def _snapshot(**overrides):
    kwargs = dict(
        account_id=ACCOUNT_ID, symbol="AAPL", checked_at=NOW, positions_match=True,
        open_orders_match=True, fills_match=True, has_unknown_orders=False, source="test",
    )
    kwargs.update(overrides)
    return ReconciliationSnapshot(**kwargs)


class TestBuildSnapshotFailsClosedOnReadFailure:
    @pytest.mark.parametrize("failing_read", ["positions", "open_orders", "fills"])
    def test_any_failed_kis_read_produces_no_snapshot_at_all(self, failing_read):
        conn = _conn()
        broker = _FakeBroker(fail={failing_read})
        with pytest.raises(ReconciliationUnavailableError):
            build_snapshot(broker=broker, conn=conn, account_id=ACCOUNT_ID, symbol="AAPL",
                           now=NOW, internal_positions=[])


class TestBuildSnapshotJudgement:
    def test_matching_state_is_clean(self):
        conn = _conn()
        broker = _FakeBroker(positions=[_kis_position()])
        snapshot = build_snapshot(
            broker=broker, conn=conn, account_id=ACCOUNT_ID, symbol="AAPL", now=NOW,
            internal_positions=[_internal_position()],
        )
        assert snapshot.is_clean()
        assert snapshot.checked_at == NOW
        assert snapshot.account_id == ACCOUNT_ID

    def test_position_quantity_mismatch_detected(self):
        conn = _conn()
        broker = _FakeBroker(positions=[_kis_position(qty=4)])
        snapshot = build_snapshot(
            broker=broker, conn=conn, account_id=ACCOUNT_ID, symbol="AAPL", now=NOW,
            internal_positions=[_internal_position(qty=5)],
        )
        assert snapshot.positions_match is False
        assert not snapshot.is_clean()

    def test_kis_position_unknown_to_internal_store_detected(self):
        conn = _conn()
        broker = _FakeBroker(positions=[_kis_position(symbol="TSLA", qty=3)])
        snapshot = build_snapshot(
            broker=broker, conn=conn, account_id=ACCOUNT_ID, symbol="AAPL", now=NOW,
            internal_positions=[],
        )
        assert snapshot.positions_match is False

    def test_untracked_kis_open_order_detected(self):
        conn = _conn()
        broker = _FakeBroker(open_orders=[{"ODNO": "kis-stranger"}])
        snapshot = build_snapshot(
            broker=broker, conn=conn, account_id=ACCOUNT_ID, symbol="AAPL", now=NOW,
            internal_positions=[],
        )
        assert snapshot.open_orders_match is False

    def test_internally_live_order_missing_at_kis_detected(self):
        from helpers_order_state import register_and_drive

        conn = _conn()
        register_and_drive(
            conn, internal_order_id="ord-1", signal_id="sig-1", symbol="AAPL", side="buy",
            trading_date="2026-07-29", target="ACCEPTED", broker_order_id="kis-1",
        )
        broker = _FakeBroker()  # KIS reports neither an open order nor a fill
        snapshot = build_snapshot(
            broker=broker, conn=conn, account_id=ACCOUNT_ID, symbol="AAPL", now=NOW,
            internal_positions=[],
        )
        assert snapshot.open_orders_match is False

    def test_fill_for_an_order_we_recorded_as_rejected_detected(self):
        from helpers_order_state import register_and_drive

        conn = _conn()
        register_and_drive(
            conn, internal_order_id="ord-1", signal_id="sig-1", symbol="AAPL", side="buy",
            trading_date="2026-07-29", target="REJECTED", broker_order_id="kis-1",
        )
        broker = _FakeBroker(fills=[{"ODNO": "kis-1", "ft_ccld_qty": "1"}])
        snapshot = build_snapshot(
            broker=broker, conn=conn, account_id=ACCOUNT_ID, symbol="AAPL", now=NOW,
            internal_positions=[],
        )
        assert snapshot.fills_match is False

    def test_overfill_beyond_requested_quantity_detected(self):
        from helpers_order_state import register_and_drive

        conn = _conn()
        register_and_drive(
            conn, internal_order_id="ord-1", signal_id="sig-1", symbol="AAPL", side="buy",
            trading_date="2026-07-29", target="ACCEPTED", broker_order_id="kis-1",
            requested_quantity=2,
        )
        broker = _FakeBroker(fills=[{"ODNO": "kis-1", "ft_ccld_qty": "3"}])
        snapshot = build_snapshot(
            broker=broker, conn=conn, account_id=ACCOUNT_ID, symbol="AAPL", now=NOW,
            internal_positions=[],
        )
        assert snapshot.fills_match is False

    def test_unknown_order_anywhere_on_the_account_is_reported(self):
        from helpers_order_state import register_and_drive

        conn = _conn()
        register_and_drive(
            conn, internal_order_id="ord-1", signal_id="sig-1", symbol="MSFT", side="sell",
            trading_date="2026-07-29", target="UNKNOWN", broker_order_id="kis-9",
        )
        broker = _FakeBroker(open_orders=[{"ODNO": "kis-9"}])
        snapshot = build_snapshot(
            broker=broker, conn=conn, account_id=ACCOUNT_ID, symbol="AAPL", now=NOW,
            internal_positions=[],
        )
        assert snapshot.has_unknown_orders is True
        assert not snapshot.is_clean()


class TestVerifySnapshot:
    def test_clean_current_snapshot_passes(self):
        assert verify_snapshot(_snapshot(), account_id=ACCOUNT_ID, symbol="AAPL", now=NOW) is None

    def test_missing_snapshot_blocked(self):
        with pytest.raises(ReconciliationBlockedError):
            verify_snapshot(None, account_id=ACCOUNT_ID, symbol="AAPL", now=NOW)

    def test_a_true_boolean_is_not_a_snapshot(self):
        with pytest.raises(ReconciliationBlockedError):
            verify_snapshot(True, account_id=ACCOUNT_ID, symbol="AAPL", now=NOW)

    def test_wrong_account_blocked(self):
        with pytest.raises(ReconciliationBlockedError, match="different account"):
            verify_snapshot(_snapshot(account_id="99999999"), account_id=ACCOUNT_ID,
                            symbol="AAPL", now=NOW)

    def test_wrong_symbol_blocked(self):
        with pytest.raises(ReconciliationBlockedError, match="MSFT"):
            verify_snapshot(_snapshot(symbol="MSFT"), account_id=ACCOUNT_ID, symbol="AAPL", now=NOW)

    def test_stale_snapshot_blocked(self):
        stale = _snapshot(checked_at=NOW - timedelta(seconds=max_age_seconds() + 1))
        with pytest.raises(ReconciliationBlockedError, match="old"):
            verify_snapshot(stale, account_id=ACCOUNT_ID, symbol="AAPL", now=NOW)

    def test_future_dated_snapshot_blocked(self):
        future = _snapshot(checked_at=NOW + timedelta(seconds=60))
        with pytest.raises(ReconciliationBlockedError, match="future"):
            verify_snapshot(future, account_id=ACCOUNT_ID, symbol="AAPL", now=NOW)

    @pytest.mark.parametrize("field", ["positions_match", "open_orders_match", "fills_match"])
    def test_any_mismatch_blocked(self, field):
        with pytest.raises(ReconciliationBlockedError):
            verify_snapshot(_snapshot(**{field: False}), account_id=ACCOUNT_ID,
                            symbol="AAPL", now=NOW)

    def test_unknown_orders_blocked(self):
        with pytest.raises(ReconciliationBlockedError, match="UNKNOWN"):
            verify_snapshot(_snapshot(has_unknown_orders=True), account_id=ACCOUNT_ID,
                            symbol="AAPL", now=NOW)


class TestTtlConfiguration:
    def test_default_is_conservative(self):
        assert max_age_seconds() == 30

    def test_env_override_respected(self, monkeypatch):
        monkeypatch.setenv("RECONCILIATION_MAX_AGE_SECONDS", "5")
        assert max_age_seconds() == 5
        stale = _snapshot(checked_at=NOW - timedelta(seconds=6))
        with pytest.raises(ReconciliationBlockedError):
            verify_snapshot(stale, account_id=ACCOUNT_ID, symbol="AAPL", now=NOW)

    @pytest.mark.parametrize("bad", ["", "abc", "0", "-1", "nan"])
    def test_unusable_override_falls_back_to_the_default_never_to_no_limit(self, monkeypatch, bad):
        monkeypatch.setenv("RECONCILIATION_MAX_AGE_SECONDS", bad)
        assert max_age_seconds() == 30
