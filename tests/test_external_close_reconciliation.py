"""Retiring a canonical position the broker no longer holds.

On 2026-08-31 the operator sold the remaining S1 holding (TX) by hand.
The broker went flat -- 0 positions, 0 open orders -- and the canonical
row stayed OPEN qty 1 for hours, because `sync_fills` adopts positions
the broker reports and has no opinion about one it has stopped
reporting.

That is not harmless: the S1 watchdog counted the phantom as a held
position and armed itself on it, which is how a strategy holding nothing
can still reach for a kill switch.

The hard part is that a missing broker position is weak evidence on its
own -- equally consistent with a fill that has not landed, a submission
in flight, a cancel mid-flight, or a partial read. So these tests are
mostly about what must STOP a retirement.
"""

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from reconciliation import external_close as ec  # noqa: E402
from s1_live import position_store as s1ps  # noqa: E402

NOW = datetime(2026, 8, 31, 17, 30, tzinfo=timezone.utc)
S1 = "S1_HMA_EARLY_TREND_V1"


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


class _Broker:
    def __init__(self, positions=(), orders=(), fail=False):
        self._positions = list(positions)
        self._orders = list(orders)
        self._fail = fail

    def get_positions(self):
        if self._fail:
            raise RuntimeError("KIS unreachable")
        return [{"symbol": s} for s in self._positions]

    def get_open_orders(self):
        if self._fail:
            raise RuntimeError("KIS unreachable")
        return [{"symbol": s} for s in self._orders]


def _open_tx(conn, symbol="TX", quantity=1):
    return s1ps.open_position(conn, symbol=symbol, strategy_id=S1,
                              signal_id=f"s1-{symbol}", entry_price=53.68,
                              quantity=quantity, now=NOW)


def _run(conn, broker, apply=True):
    return ec.retire_externally_closed(conn, broker, strategy_id=S1,
                                       store=s1ps, now=NOW, apply=apply)


class TestTheTXCase:
    def test_a_broker_flat_account_retires_the_row(self, conn):
        pid = _open_tx(conn)
        out = _run(conn, _Broker())
        assert out[0]["outcome"] == ec.RETIRED
        assert s1ps.holdings(conn) == []
        row = conn.execute("SELECT status, exit_reason, exit_price FROM "
                           "s1_positions WHERE position_id = ?", (pid,)).fetchone()
        assert row["status"] == "CLOSED"
        assert row["exit_reason"] == ec.EXTERNALLY_CLOSED

    def test_no_exit_price_is_invented(self, conn):
        """This system did not sell these shares and does not know what
        they fetched. A plausible number would be a fabricated trade in
        the performance record."""
        pid = _open_tx(conn)
        _run(conn, _Broker())
        row = conn.execute("SELECT exit_price FROM s1_positions WHERE "
                           "position_id = ?", (pid,)).fetchone()
        assert row["exit_price"] is None

    def test_it_is_not_recorded_as_a_normal_trade(self, conn):
        _open_tx(conn)
        out = _run(conn, _Broker())
        assert out[0]["source"] == ec.EXTERNALLY_CLOSED
        assert out[0]["previous_status"] == "OPEN"
        assert out[0]["previous_quantity"] == 1

    def test_the_evidence_is_preserved(self, conn):
        """So the judgement can be argued with afterwards rather than
        taken on trust."""
        _open_tx(conn)
        out = _run(conn, _Broker(positions=["OTHER"], orders=[]))
        record = out[0]
        assert record["broker_positions_seen"] == ["OTHER"]
        assert record["broker_open_orders_seen"] == []
        assert record["reconciled_at"] == NOW.isoformat()
        assert record["strategy_id"] == S1

    def test_dry_run_changes_nothing(self, conn):
        """How this should be run the first time against a live account."""
        _open_tx(conn)
        out = _run(conn, _Broker(), apply=False)
        assert out[0]["outcome"] == ec.RETIRED
        assert [s for s, _v, _q in s1ps.holdings(conn)] == ["TX"]


class TestEverySourceMustAgree:
    """A missing broker position alone is weak evidence."""

    def test_a_broker_position_stops_it(self, conn):
        _open_tx(conn)
        out = _run(conn, _Broker(positions=["TX"]))
        assert out[0]["outcome"] == ec.HELD_BROKER_POSITION
        assert [s for s, _v, _q in s1ps.holdings(conn)] == ["TX"]

    def test_an_open_broker_order_stops_it(self, conn):
        """A sell mid-flight is not a closed position."""
        _open_tx(conn)
        out = _run(conn, _Broker(orders=["TX"]))
        assert out[0]["outcome"] == ec.HELD_OPEN_ORDER
        assert [s for s, _v, _q in s1ps.holdings(conn)] == ["TX"]

    def test_an_unresolved_ledger_order_stops_it(self, conn):
        _open_tx(conn)
        conn.execute(
            "INSERT INTO kis_order_idempotency (internal_order_id, signal_id, "
            "symbol, side, trading_date, status, created_at, updated_at, "
            "requested_quantity, version, strategy_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("kislive-TX-1", "sig", "TX", "sell", "2026-08-31", "ACCEPTED",
             NOW.isoformat(), NOW.isoformat(), 1.0, 1, S1))
        conn.commit()
        out = _run(conn, _Broker())
        assert out[0]["outcome"] == ec.HELD_UNRESOLVED_ORDER
        assert [s for s, _v, _q in s1ps.holdings(conn)] == ["TX"]

    def test_an_unresolved_exit_intent_stops_it(self, conn):
        from state_store import exit_intent_ledger as ledger

        pid = _open_tx(conn)
        intent = ledger.reserve(conn, pid, "STOP", 1, "s1exit-TX-1")
        ledger.mark_submission_unknown(conn, intent)
        out = _run(conn, _Broker())
        assert out[0]["outcome"] == ec.HELD_UNRESOLVED_INTENT
        assert [s for s, _v, _q in s1ps.holdings(conn)] == ["TX"]

    def test_an_exit_in_flight_stops_it(self, conn):
        pid = _open_tx(conn)
        s1ps.mark_exit_submitted(conn, pid, "STOP", now=NOW)
        out = _run(conn, _Broker())
        assert out[0]["outcome"] == ec.HELD_EXIT_IN_FLIGHT

    def test_an_unreadable_broker_retires_nothing(self, conn):
        """An unreadable broker is not a flat one. Refusing costs a stale
        row; guessing loses a real position while the shares exist."""
        _open_tx(conn)
        out = _run(conn, _Broker(fail=True))
        assert out[0]["outcome"] == ec.HELD_BROKER_UNREADABLE
        assert [s for s, _v, _q in s1ps.holdings(conn)] == ["TX"]


class TestItTouchesOnlyWhatItShould:
    def test_another_symbol_the_broker_holds_is_untouched(self, conn):
        _open_tx(conn, symbol="TX")
        _open_tx(conn, symbol="AAPL")
        _run(conn, _Broker(positions=["AAPL"]))
        assert [s for s, _v, _q in s1ps.holdings(conn)] == ["AAPL"]

    def test_it_places_no_order(self):
        source = (REPO_ROOT / "reconciliation" / "external_close.py").read_text()
        for forbidden in ("submit_order", "submit_buy", "submit_sell",
                          "cancel_order"):
            assert forbidden not in source, forbidden

    def test_it_writes_no_status_directly(self):
        """The row goes through the store's own transition, so the change
        is auditable rather than a row that silently mutated."""
        source = (REPO_ROOT / "reconciliation" / "external_close.py").read_text()
        assert "UPDATE s1_positions" not in source
        assert "close_position" in source

    def test_it_handles_both_store_keyword_conventions(self):
        import inspect

        from s2_live import position_store as s2ps
        from s6_live import position_store as s6ps

        for store in (s1ps, s2ps, s6ps):
            parameters = inspect.signature(store.close_position).parameters
            assert "exit_reason" in parameters or "reason" in parameters


class TestGeneralLifecycleBook:
    """The second book -- `positions/store.py`.

    Retiring only the per-strategy row left TX visible to
    `reconciliation.snapshot.load_internal_positions`, which reads both
    books, so the account still reconciled as "internal=1 KIS=0" with
    nothing left to point at. This is a validated state machine, so the
    retirement has to go through a real transition rather than a status
    write.
    """

    @pytest.fixture
    def book(self, conn, monkeypatch, tmp_path):
        monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "positions.json"))
        from positions import states, store

        def _held(symbol="TX", qty=1, state=states.STOP_ACTIVE):
            record = store.create_position(S1, "v1", symbol, f"cid-{symbol}", qty)
            pid = record["position_id"]
            walk = [states.ARMED, states.ENTRY_RESERVED, states.ENTRY_SUBMITTED,
                    states.FILLED]
            if state != states.FILLED:
                walk.append(states.STOP_ACTIVE)
            if state not in (states.FILLED, states.STOP_ACTIVE):
                walk.append(state)
            for step in walk:
                with store.locked_position(pid) as locked:
                    states.validate_transition(locked["state"], step)
                    locked["state"] = step
                    locked["state_history"].append(
                        {"state": step, "at": NOW.isoformat(), "reason": "test"})
                    if step == states.FILLED:
                        locked["filled_qty"] = qty
                        locked["remaining_qty"] = qty
                        locked["average_fill_price"] = 53.68
            return pid

        return _held

    def test_a_held_row_the_broker_no_longer_reports_is_retired(self, conn, book):
        from positions import states, store

        pid = book()
        out = ec.retire_general_store(_Broker(), conn, now=NOW)

        assert [r["outcome"] for r in out] == [ec.RETIRED]
        assert store.load_position(pid)["state"] == states.EXTERNALLY_CLOSED
        assert pid not in store.load_non_terminal()

    def test_the_retirement_invents_no_exit_price_and_no_pnl(self, conn, book):
        pid = book()
        ec.retire_general_store(_Broker(), conn, now=NOW)

        from positions import store

        record = store.load_position(pid)
        assert not record.get("exit_price")
        assert not record.get("realized_pnl")
        last = record["state_history"][-1]
        assert "closed outside this system" in last["reason"]

    def test_a_position_the_broker_still_reports_is_left_alone(self, conn, book):
        from positions import states, store

        pid = book()
        out = ec.retire_general_store(_Broker(positions=["TX"]), conn, now=NOW)

        assert [r["outcome"] for r in out] == [ec.HELD_BROKER_POSITION]
        assert store.load_position(pid)["state"] == states.STOP_ACTIVE

    def test_an_open_broker_order_stops_the_retirement(self, conn, book):
        book()
        out = ec.retire_general_store(_Broker(orders=["TX"]), conn, now=NOW)
        assert [r["outcome"] for r in out] == [ec.HELD_OPEN_ORDER]

    def test_an_exit_already_submitted_is_not_externally_closed(self, conn, book):
        """It settles to CLOSED with a real price; this must not pre-empt it."""
        from positions import states, store

        pid = book(state=states.EXIT_SUBMITTED)
        out = ec.retire_general_store(_Broker(), conn, now=NOW)

        assert [r["outcome"] for r in out] == [ec.HELD_EXIT_IN_FLIGHT]
        assert store.load_position(pid)["state"] == states.EXIT_SUBMITTED

    def test_a_state_under_human_adjudication_is_not_retired(self, conn, book):
        from positions import states, store

        pid = book(state=states.MANUAL_REVIEW)
        out = ec.retire_general_store(_Broker(), conn, now=NOW)

        assert [r["outcome"] for r in out] == [ec.HELD_STATE_NOT_RETIRABLE]
        assert store.load_position(pid)["state"] == states.MANUAL_REVIEW

    def test_a_dry_run_changes_nothing(self, conn, book):
        from positions import states, store

        pid = book()
        out = ec.retire_general_store(_Broker(), conn, now=NOW, apply=False)

        assert [r["outcome"] for r in out] == [ec.RETIRED]
        assert store.load_position(pid)["state"] == states.STOP_ACTIVE

    def test_an_unreadable_broker_retires_nothing(self, conn, book):
        from positions import states, store

        pid = book()
        out = ec.retire_general_store(_Broker(fail=True), conn, now=NOW)

        assert [r["outcome"] for r in out] == [ec.HELD_BROKER_UNREADABLE]
        assert store.load_position(pid)["state"] == states.STOP_ACTIVE

    def test_the_retired_row_leaves_the_reconciler_s_internal_view(self, conn, book):
        """The whole point: internal=1 KIS=0 must stop being reported."""
        from reconciliation.snapshot import load_internal_positions

        book()
        before = load_internal_positions(now=NOW, conn=conn)
        assert [p.symbol for p in before] == ["TX"]

        ec.retire_general_store(_Broker(), conn, now=NOW)

        after = load_internal_positions(now=NOW, conn=conn)
        assert [p.symbol for p in after] == []
