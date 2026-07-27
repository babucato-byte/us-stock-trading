"""live_readiness/account_engine.py unit tests -- pure unit tests with a
fake broker + isolated SQLite, no real network."""
from datetime import datetime, timedelta, timezone

import pytest

from live_readiness import account_engine as ae
from live_readiness import entry_reservation_ledger as ledger
from state_store import db as state_db

NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate_state_db(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "TEST_STATE.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "POSITION_STORE.json"))
    monkeypatch.setattr(ledger, "_LOCK_FILE", tmp_path / "LIVE_ENTRY_RESERVATION.lock")
    yield


class _FakeConfig:
    def __init__(self, trading_mode="live"):
        self.trading_mode = trading_mode


class _FakeBroker:
    def __init__(self, account, trading_mode="live"):
        self.account = account
        self.config = _FakeConfig(trading_mode)
        self.calls = 0

    def get_account(self):
        self.calls += 1
        return self.account


def _conn():
    return state_db.open_db()


def test_valid_snapshot_effective_cash_is_min_of_cash_and_non_margin():
    broker = _FakeBroker({"cash": "20.00", "non_marginable_buying_power": "15.00"})
    snapshot = ae.build_account_snapshot(broker, 1_000.0, _conn(), now=NOW)
    assert snapshot.broker_cash_krw == pytest.approx(20_000.0)
    assert snapshot.non_margin_available_cash_krw == pytest.approx(15_000.0)
    assert snapshot.effective_cash_krw == pytest.approx(15_000.0)  # min(), not buying power


def test_missing_non_margin_field_falls_back_to_cash():
    broker = _FakeBroker({"cash": "20.00"})
    snapshot = ae.build_account_snapshot(broker, 1_000.0, _conn(), now=NOW)
    assert snapshot.non_margin_available_cash_krw == pytest.approx(20_000.0)
    assert snapshot.effective_cash_krw == pytest.approx(20_000.0)


def test_buying_power_larger_than_cash_never_used_as_ceiling():
    # A margin-inflated buying_power field (not read at all) must never
    # raise effective_cash above cash.
    broker = _FakeBroker({"cash": "20.00", "buying_power": "500.00",
                           "non_marginable_buying_power": "20.00"})
    snapshot = ae.build_account_snapshot(broker, 1_000.0, _conn(), now=NOW)
    assert snapshot.effective_cash_krw == pytest.approx(20_000.0)


@pytest.mark.parametrize("bad_non_margin", ["not-a-number", "-5", "nan", "inf"])
def test_invalid_non_margin_field_blocked(bad_non_margin):
    broker = _FakeBroker({"cash": "20.00", "non_marginable_buying_power": bad_non_margin})
    with pytest.raises(ae.AccountEngineError):
        ae.build_account_snapshot(broker, 1_000.0, _conn(), now=NOW)


def test_missing_cash_field_blocked():
    broker = _FakeBroker({"non_marginable_buying_power": "10.00"})
    with pytest.raises(ae.AccountEngineError, match="cash"):
        ae.build_account_snapshot(broker, 1_000.0, _conn(), now=NOW)


def test_broker_call_failure_blocked():
    class _RaisingBroker:
        config = _FakeConfig()

        def get_account(self):
            raise ConnectionError("network down")

    with pytest.raises(ae.AccountEngineError):
        ae.build_account_snapshot(_RaisingBroker(), 1_000.0, _conn(), now=NOW)


@pytest.mark.parametrize("bad_mode", [None, "", "unknown", "LIVE", "sandbox"])
def test_ambiguous_trading_mode_blocked(bad_mode):
    broker = _FakeBroker({"cash": "20.00"}, trading_mode=bad_mode)
    with pytest.raises(ae.AccountEngineError, match="trading mode"):
        ae.build_account_snapshot(broker, 1_000.0, _conn(), now=NOW)


def test_expected_trading_mode_mismatch_blocked():
    broker = _FakeBroker({"cash": "20.00"}, trading_mode="paper")
    with pytest.raises(ae.AccountEngineError, match="trading mode"):
        ae.build_account_snapshot(broker, 1_000.0, _conn(), now=NOW, expected_trading_mode="live")


def test_expected_account_id_mismatch_blocked():
    broker = _FakeBroker({"cash": "20.00", "account_number": "ACC-1"})
    with pytest.raises(ae.AccountEngineError, match="account id"):
        ae.build_account_snapshot(broker, 1_000.0, _conn(), now=NOW, expected_account_id="ACC-2")


def test_expected_account_id_match_accepted():
    broker = _FakeBroker({"cash": "20.00", "account_number": "ACC-1"})
    snapshot = ae.build_account_snapshot(
        broker, 1_000.0, _conn(), now=NOW, expected_account_id="ACC-1",
    )
    assert snapshot.account_id == "ACC-1"


def test_pending_unknown_open_position_reflected_from_ledger():
    conn = _conn()
    ledger.reserve(conn, "MSFT", 5_000.0, "coid-pending")
    coid_unknown = "coid-unknown"
    reservation_id = ledger.reserve(conn, "MSFT2", 3_000.0, coid_unknown)
    ledger.mark_submission_unknown(conn, reservation_id)

    broker = _FakeBroker({"cash": "100.00", "non_marginable_buying_power": "100.00"})
    snapshot = ae.build_account_snapshot(broker, 1_000.0, conn, now=NOW)
    assert snapshot.pending_buy_reservations_krw == pytest.approx(5_000.0)
    assert snapshot.unknown_submission_reservations_krw == pytest.approx(3_000.0)
    assert snapshot.reconciliation_required_reservations_krw == pytest.approx(3_000.0)
    assert not snapshot.reconciliation_complete


def test_reconciliation_complete_when_no_unknown_reservations():
    broker = _FakeBroker({"cash": "100.00", "non_marginable_buying_power": "100.00"})
    snapshot = ae.build_account_snapshot(broker, 1_000.0, _conn(), now=NOW)
    assert snapshot.reconciliation_complete


def test_is_account_snapshot_stale():
    broker = _FakeBroker({"cash": "20.00", "non_marginable_buying_power": "20.00"})
    snapshot = ae.build_account_snapshot(broker, 1_000.0, _conn(), now=NOW)
    assert not ae.is_account_snapshot_stale(snapshot, 300, now=NOW + timedelta(seconds=100))
    assert ae.is_account_snapshot_stale(snapshot, 300, now=NOW + timedelta(seconds=400))


def test_max_allocatable_cash_capped_by_trusted_ceiling():
    broker = _FakeBroker({"cash": "30000.00", "non_marginable_buying_power": "30000.00"})
    # cash is already in "KRW-equivalent" terms via fx_rate=1 for simplicity
    snapshot = ae.build_account_snapshot(broker, 1.0, _conn(), now=NOW)
    max_allocatable = ae.compute_max_allocatable_cash_krw(snapshot, 100)  # caller requests 100%
    assert max_allocatable == pytest.approx(15_000.0)  # capped at trusted 50%


def test_available_for_new_order_deducts_committed():
    conn = _conn()
    ledger.reserve(conn, "MSFT", 5_000.0, "coid-a")
    broker = _FakeBroker({"cash": "30000.00", "non_marginable_buying_power": "30000.00"})
    snapshot = ae.build_account_snapshot(broker, 1.0, conn, now=NOW)
    available = ae.compute_available_for_new_order_krw(snapshot, 100)
    assert available == pytest.approx(15_000.0 - 5_000.0)
