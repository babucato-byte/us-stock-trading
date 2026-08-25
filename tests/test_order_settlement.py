"""An ACCEPTED order that filled must stop being ACCEPTED.

The incident this pins
----------------------
KIS order 0030469882 bought 1 TX at 53.68 on 2026-08-18. The fill was
seen: the position projection recorded FILLED and armed a stop. The
`kis_order_idempotency` row was never advanced, because nothing in the
codebase could advance it -- `reconcile_unknown_order` only ever looked
at UNKNOWN rows.

Two consequences, both of which actually happened:

  * `execution/entry_limits.py` counts a non-terminal row as an entry in
    flight, so the row held a position slot indefinitely.
  * `reconciliation/snapshot.py` compared internal live orders against
    KIS's open orders and TODAY's fills. A fill from a previous session
    is in neither, so the snapshot reported "recorded internally as live
    but KIS reports neither an open order nor any fill for it" on every
    pass, was permanently dirty, and blocked every BUY for every
    strategy. 1,040 entry attempts were rejected before anyone looked.

So there are two defects and two fixes, and this file pins both: the
settlement path that can move the row, and the fill window that lets the
evidence be seen at all.
"""

from datetime import datetime, timedelta, timezone

import pytest

from reconciliation import fill_window
from reconciliation.order_reconciler import settle_live_order
from state_store import db as state_db

NOW = datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc)


def _fill(order_id, qty="1"):
    return {"odno": order_id, "ft_ccld_qty": qty}


class TestSettlement:
    def test_a_full_fill_settles_to_filled(self):
        outcome = settle_live_order(
            "int-1", "0030469882", "ACCEPTED", [], [_fill("0030469882")],
            requested_quantity=1)
        assert outcome.resolved
        assert outcome.confirmed_status == "FILLED"

    def test_a_partial_fill_settles_to_partially_filled(self):
        outcome = settle_live_order(
            "int-1", "X1", "ACCEPTED", [], [_fill("X1", "1")],
            requested_quantity=3)
        assert outcome.resolved
        assert outcome.confirmed_status == "PARTIALLY_FILLED"

    def test_an_order_still_open_at_kis_is_left_alone(self):
        outcome = settle_live_order(
            "int-1", "X1", "ACCEPTED", [{"odno": "X1"}], [_fill("X1")],
            requested_quantity=1)
        assert not outcome.resolved
        assert "still lists" in outcome.reason

    def test_neither_open_nor_filled_is_never_guessed_closed(self):
        """The mismatch that must keep blocking.

        An order KIS has no record of is exactly what a human has to
        look at. Resolving it to CANCELLED here would clear the alarm
        rather than the fault.
        """
        outcome = settle_live_order(
            "int-1", "GONE", "ACCEPTED", [], [], requested_quantity=1)
        assert not outcome.resolved
        assert outcome.confirmed_status is None

    def test_more_filled_than_requested_refuses_to_resolve(self):
        outcome = settle_live_order(
            "int-1", "X1", "ACCEPTED", [], [_fill("X1", "5")],
            requested_quantity=1)
        assert not outcome.resolved
        assert "exceeds" in outcome.reason

    def test_an_unknown_requested_quantity_is_never_assumed_full(self):
        outcome = settle_live_order(
            "int-1", "X1", "ACCEPTED", [], [_fill("X1", "1")],
            requested_quantity=None)
        assert not outcome.resolved

    def test_an_illegal_transition_is_refused_not_forced(self):
        """The ledger's own state machine has the last word: a terminal
        row is not rewritten because KIS still lists a fill for it."""
        outcome = settle_live_order(
            "int-1", "X1", "FILLED", [], [_fill("X1")], requested_quantity=1)
        assert not outcome.resolved
        assert "cannot move there" in outcome.reason

    def test_no_broker_order_id_cannot_be_looked_up(self):
        outcome = settle_live_order(
            "int-1", None, "ACCEPTED", [], [_fill("X1")], requested_quantity=1)
        assert not outcome.resolved


class TestFillWindow:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STATE_STORE_DB_FILE", str(tmp_path / "S.db"))
        state_db.open_db().close()
        yield

    def _row(self, conn, *, order_id, status, trading_date):
        conn.execute(
            "INSERT INTO kis_order_idempotency (internal_order_id, signal_id, "
            "symbol, side, trading_date, broker_order_id, status, created_at, "
            "updated_at, requested_quantity, version) "
            "VALUES (?, ?, ?, 'buy', ?, ?, ?, ?, ?, 1, 0)",
            (f"int-{order_id}", f"sig-{order_id}", "TX", trading_date, order_id,
             status, NOW.isoformat(), NOW.isoformat()))
        conn.commit()

    def test_no_live_orders_reads_today_only(self):
        conn = state_db.open_db()
        try:
            assert fill_window.window(conn, now=NOW) == ("20260825", "20260825")
        finally:
            conn.close()

    def test_a_live_order_from_a_previous_day_widens_the_window(self):
        """The whole fix. A today-only window could never see the fill
        that would clear this row."""
        conn = state_db.open_db()
        try:
            self._row(conn, order_id="0030469882", status="ACCEPTED",
                      trading_date="2026-08-18")
            assert fill_window.window(conn, now=NOW) == ("20260818", "20260825")
        finally:
            conn.close()

    def test_terminal_rows_do_not_widen_it(self):
        conn = state_db.open_db()
        try:
            self._row(conn, order_id="OLD", status="FILLED",
                      trading_date="2026-01-02")
            assert fill_window.window(conn, now=NOW) == ("20260825", "20260825")
        finally:
            conn.close()

    def test_a_row_without_a_broker_id_does_not_widen_it(self):
        """It was never matched against KIS by the open-order check
        either -- its ambiguity is the UNKNOWN check's business."""
        conn = state_db.open_db()
        try:
            conn.execute(
                "INSERT INTO kis_order_idempotency (internal_order_id, signal_id, "
                "symbol, side, trading_date, broker_order_id, status, created_at, "
                "updated_at, requested_quantity, version) "
                "VALUES ('i', 's', 'TX', 'buy', '2026-08-18', NULL, "
                "'SUBMITTING', ?, ?, 1, 0)", (NOW.isoformat(), NOW.isoformat()))
            conn.commit()
            assert fill_window.window(conn, now=NOW) == ("20260825", "20260825")
        finally:
            conn.close()

    def test_the_window_is_clamped(self):
        """A single corrupt trading_date must not turn one read into an
        unbounded one."""
        conn = state_db.open_db()
        try:
            self._row(conn, order_id="ANCIENT", status="ACCEPTED",
                      trading_date="1990-01-01")
            start, end = fill_window.window(conn, now=NOW)
            floor = (NOW.date() - timedelta(days=fill_window.MAX_LOOKBACK_DAYS))
            assert start == floor.strftime("%Y%m%d")
            assert end == "20260825"
        finally:
            conn.close()
