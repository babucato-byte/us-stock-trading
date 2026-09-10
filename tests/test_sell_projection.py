"""Terminal SELL projection reconciliation is evidence-driven only."""

from datetime import datetime, timezone

import tempfile

import pytest

from reconciliation.snapshot import ReconciliationSnapshot
from reconciliation import sell_projection
from s6_live import exit_runtime, position_store
from state_store import exit_intent_ledger

NOW = datetime(2026, 9, 10, 11, 0, tzinfo=timezone.utc)
CLIENT = "s6exit-RIG-test"
BROKER_ORDER = "0030556204"


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def _projection(conn, *, status="ACCEPTED", qty=28):
    conn.execute(
        "INSERT INTO kis_order_idempotency "
        "(internal_order_id, signal_id, symbol, side, trading_date, broker_order_id, "
        "status, created_at, updated_at, requested_quantity, version) "
        "VALUES (?, 'sig-rig', 'RIG', 'sell', '2026-09-10', ?, ?, ?, ?, ?, 0)",
        (CLIENT, BROKER_ORDER, status, NOW.isoformat(), NOW.isoformat(), qty),
    )
    conn.commit()


def _closed_confirmed(conn, *, qty=28):
    pid = position_store.record_submission(
        conn, symbol="RIG", variant="S6-P", entry_session="PREMARKET",
        client_order_id="s6buy-RIG-test", now=NOW)
    position_store.open_from_fill(conn, pid, quantity=qty,
                                  average_fill_price=5.79, now=NOW)
    position_store.latch_pending_exit(conn, pid, "VWAP_FAILURE", now=NOW)
    position_store.mark_exit_submitted(conn, pid, "VWAP_FAILURE", now=NOW)
    intent_id = exit_intent_ledger.reserve(
        conn, pid, "VWAP_FAILURE", qty, CLIENT)
    exit_intent_ledger.mark_submitted(conn, intent_id, broker_order_id=BROKER_ORDER)
    position_store.close_position(conn, pid, reason="VWAP_FAILURE", exit_price=5.79,
                                  now=NOW)
    exit_intent_ledger.mark_confirmed(conn, intent_id, qty)
    return pid


def _snapshot(quantity):
    return ReconciliationSnapshot(
        account_id="acct", symbol=None, checked_at=NOW,
        positions_match=True, open_orders_match=True, fills_match=True,
        has_unknown_orders=False, source="test",
        kis_position_quantities=(("RIG", quantity),),
    )


def _status(conn):
    return conn.execute(
        "SELECT status FROM kis_order_idempotency WHERE internal_order_id = ?", (CLIENT,)
    ).fetchone()["status"]


def test_full_sell_fill_advances_accepted_projection(conn):
    _projection(conn)
    pid = position_store.record_submission(
        conn, symbol="RIG", variant="S6-P", entry_session="PREMARKET",
        client_order_id="s6buy-RIG-fill", now=NOW)
    position_store.open_from_fill(conn, pid, quantity=28, average_fill_price=5.79, now=NOW)
    position_store.latch_pending_exit(conn, pid, "VWAP_FAILURE", now=NOW)
    position_store.mark_exit_submitted(conn, pid, "VWAP_FAILURE", now=NOW)
    intent = exit_intent_ledger.reserve(conn, pid, "VWAP_FAILURE", 28, CLIENT)
    exit_intent_ledger.mark_submitted(conn, intent, broker_order_id=BROKER_ORDER)

    exit_runtime.sync_sell_fills(
        conn, fills_for=lambda _row: {"filled_quantity": 28, "order_id": BROKER_ORDER}, now=NOW)

    assert _status(conn) == "FILLED"
    assert position_store.load(conn, pid)["status"] == "CLOSED"


def test_closed_confirmed_zero_broker_position_repairs_stale_projection(conn):
    _projection(conn)
    _closed_confirmed(conn)

    assert sell_projection.reconcile_closed_sell_projections(
        conn, snapshot=_snapshot(0), now=NOW) == [CLIENT]
    assert _status(conn) == "FILLED"
    event = conn.execute(
        "SELECT event_type FROM order_state_events WHERE internal_order_id = ? ORDER BY event_id DESC LIMIT 1",
        (CLIENT,)).fetchone()
    assert event["event_type"] == "SELL_PROJECTION_RECONCILED"


def test_closed_confirmed_without_broker_zero_does_not_terminalize(conn):
    _projection(conn)
    _closed_confirmed(conn)

    assert sell_projection.reconcile_closed_sell_projections(
        conn, snapshot=_snapshot(1), now=NOW) == []
    assert _status(conn) == "ACCEPTED"


def test_partial_fill_never_terminalizes_projection(conn):
    _projection(conn, qty=28)
    assert sell_projection.settle_confirmed_sell(
        conn, client_order_id=CLIENT, broker_order_id=BROKER_ORDER,
        confirmed_filled_qty=5, expected_quantity=28,
        event_type="test", evidence="partial", now=NOW) is None
    assert _status(conn) == "ACCEPTED"


@pytest.mark.parametrize("status", ["CANCELLED", "REJECTED"])
def test_existing_terminal_projections_are_never_rewritten(conn, status):
    _projection(conn, status=status)
    _closed_confirmed(conn)

    assert sell_projection.reconcile_closed_sell_projections(
        conn, snapshot=_snapshot(0), now=NOW) == []
    assert _status(conn) == status
