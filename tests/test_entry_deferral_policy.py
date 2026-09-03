"""An S6 exit suppresses re-entry for its symbol, not the account."""

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import kis_live_trading as klt  # noqa: E402
from s6_live import position_store as ps  # noqa: E402

NOW = datetime(2026, 9, 3, 15, 0, tzinfo=timezone.utc)


class _Intent:
    internal_order_id = "iid-1"
    quantity = 1
    limit_price = 100.0


class _Instrument:
    symbol = "NVDA"
    exchange = "NASDAQ"


class _Broker:
    def get_open_orders(self):
        return []

    def get_orderable_usd(self, instrument, price):
        return 1000.0


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "state.db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


@pytest.fixture(autouse=True)
def permissive_switches(monkeypatch):
    monkeypatch.setattr(klt.ops_kill_switch, "is_halted", lambda: False)
    monkeypatch.setattr(klt.ops_kill_switch, "is_entry_allowed", lambda: True)
    monkeypatch.delenv("ENTRY_DISABLED", raising=False)


def _exit_submitted(conn, symbol):
    pid = ps.record_submission(
        conn, symbol=symbol, variant="S6-R", entry_session="REGULAR",
        client_order_id=f"cid-{symbol}", now=NOW)
    ps.open_from_fill(conn, pid, quantity=1, average_fill_price=100.0, now=NOW)
    ps.latch_pending_exit(conn, pid, "RANGE_REENTRY", now=NOW)
    ps.mark_exit_submitted(conn, pid, "RANGE_REENTRY", now=NOW)
    return pid


def _revalidate(conn, symbol):
    instrument = _Instrument()
    instrument.symbol = symbol
    return klt._revalidate_before_submit(
        symbol=symbol, broker=_Broker(), conn=conn, instrument=instrument,
        order_intent=_Intent(), buffered_price=100.0, live_state={}, now=NOW)


def test_same_symbol_exit_submitted_blocks_candidate(conn):
    _exit_submitted(conn, "FLS")
    code, detail = _revalidate(conn, "FLS")
    assert code == klt.REVALIDATION_EXIT_IN_FLIGHT
    assert "FLS" in detail


def test_unrelated_ready_candidate_continues(conn):
    _exit_submitted(conn, "FLS")
    assert _revalidate(conn, "NVDA") is None


def test_multiple_exits_block_only_their_symbols(conn):
    _exit_submitted(conn, "FLS")
    _exit_submitted(conn, "MTCH")
    assert _revalidate(conn, "NVDA") is None
    assert _revalidate(conn, "FLS")[0] == klt.REVALIDATION_EXIT_IN_FLIGHT
    assert _revalidate(conn, "MTCH")[0] == klt.REVALIDATION_EXIT_IN_FLIGHT


def test_no_exit_in_flight_is_unchanged(conn):
    assert _revalidate(conn, "NVDA") is None


def test_exit_state_change_before_submit_is_caught_under_lock(conn):
    # This state appears after candidate evaluation and immediately before
    # the caller's locked submit-time revalidation.
    _exit_submitted(conn, "NVDA")
    assert _revalidate(conn, "NVDA")[0] == klt.REVALIDATION_EXIT_IN_FLIGHT


def test_global_precycle_exit_deferral_is_removed():
    spec = importlib.util.spec_from_file_location(
        "run_live_buy_entry", REPO_ROOT / "scripts" / "run_live_buy_entry.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    assert not hasattr(entry, "_exit_in_flight")
    assert "ENTRY_DEFERRED_EXIT_PENDING" not in Path(entry.__file__).read_text()
