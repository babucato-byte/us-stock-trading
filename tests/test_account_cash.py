"""CODEX-036: live_readiness/account_cash.py unit tests.

Pure unit tests -- no real broker, no network, no SQLite.
"""
from datetime import datetime, timezone

import pytest

from live_readiness.account_cash import (
    AccountCashSnapshot,
    AccountCashSnapshotError,
    fetch_account_cash_snapshot,
)

NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)


class _FakeBroker:
    def __init__(self, account=None, *, raises=None):
        self.account = account
        self.raises = raises
        self.calls = 0

    def get_account(self):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.account


def test_valid_fetch_converts_usd_to_krw():
    broker = _FakeBroker({"cash": "20.00"})
    snapshot = fetch_account_cash_snapshot(broker, 1_350.0, now=NOW)
    assert isinstance(snapshot, AccountCashSnapshot)
    assert snapshot.cash_krw == pytest.approx(27_000.0)
    assert snapshot.as_of == NOW
    assert snapshot.source == "broker_account_endpoint"
    assert broker.calls == 1


def test_missing_cash_field_blocked():
    broker = _FakeBroker({"buying_power": "100"})
    with pytest.raises(AccountCashSnapshotError, match="cash"):
        fetch_account_cash_snapshot(broker, 1_350.0, now=NOW)


def test_non_dict_response_blocked():
    broker = _FakeBroker("not a dict")
    with pytest.raises(AccountCashSnapshotError):
        fetch_account_cash_snapshot(broker, 1_350.0, now=NOW)


def test_non_numeric_cash_blocked():
    broker = _FakeBroker({"cash": "not-a-number"})
    with pytest.raises(AccountCashSnapshotError, match="not numeric"):
        fetch_account_cash_snapshot(broker, 1_350.0, now=NOW)


@pytest.mark.parametrize("bad_cash", ["-100", "nan", "inf"])
def test_invalid_cash_value_blocked(bad_cash):
    broker = _FakeBroker({"cash": bad_cash})
    with pytest.raises(AccountCashSnapshotError):
        fetch_account_cash_snapshot(broker, 1_350.0, now=NOW)


@pytest.mark.parametrize("bad_fx", [None, 0, -1, "1350", True, float("nan"), float("inf")])
def test_invalid_fx_rate_blocked(bad_fx):
    broker = _FakeBroker({"cash": "20.00"})
    with pytest.raises(AccountCashSnapshotError, match="fx_rate_krw_per_usd"):
        fetch_account_cash_snapshot(broker, bad_fx, now=NOW)
    assert broker.calls == 0  # invalid FX blocked before ever calling the broker


def test_network_failure_wrapped_as_snapshot_error():
    broker = _FakeBroker(raises=ConnectionError("network down"))
    with pytest.raises(AccountCashSnapshotError, match="broker.get_account"):
        fetch_account_cash_snapshot(broker, 1_350.0, now=NOW)


def test_runtime_error_propagates_unwrapped():
    # A RuntimeError from the broker's own safety gates (kill switch,
    # "real live trading disabled", credential mismatch) must NOT be
    # masked as a generic AccountCashSnapshotError -- callers rely on
    # being able to tell these apart (see broker/alpaca_client.py).
    broker = _FakeBroker(raises=RuntimeError("Real live trading is disabled in this pre-live PR."))
    with pytest.raises(RuntimeError, match="Real live trading is disabled"):
        fetch_account_cash_snapshot(broker, 1_350.0, now=NOW)


def test_defaults_now_when_not_supplied():
    broker = _FakeBroker({"cash": "1.00"})
    snapshot = fetch_account_cash_snapshot(broker, 1_000.0)
    assert snapshot.as_of.tzinfo is not None
