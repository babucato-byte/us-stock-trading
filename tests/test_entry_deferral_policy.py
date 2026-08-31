"""When a pending exit should stop a new entry, and when it should not.

What happened
-------------
The rule deferred every entry whenever ANY S6 position had an exit
submitted OR pending. The stated reason is real: entry and exit share
`s6_exec.lock`, so an entry cycle holding it delays the exit behind it,
and an exit is a position already at risk while an entry is only an
opportunity.

That reasoning holds while the exit can actually run. RIG latched
EXIT_PENDING on Friday at 19:52 and its route was unavailable for the
whole weekend, so the check returned True continuously for three days
and deferred every entry -- protecting an exit cycle that was never
going to start. Nothing was being contended for, and S6 simply stopped
trading.

So the distinction is whether the exit CAN run, not whether one is
pending.
"""

import importlib.util
import inspect
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location(
    "run_live_buy_entry", REPO_ROOT / "scripts" / "run_live_buy_entry.py")
entry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(entry)

from s6_live import position_store as ps  # noqa: E402

NOW = datetime(2026, 8, 28, 19, 52, tzinfo=timezone.utc)


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def _open(conn, symbol="RIG"):
    pid = ps.record_submission(conn, symbol=symbol, variant="S6-R",
                               entry_session="REGULAR",
                               client_order_id=f"kislive-{symbol}-1", now=NOW)
    ps.open_from_fill(conn, pid, quantity=3, average_fill_price=5.85,
                      venue="NYSE", now=NOW)
    return pid


def _capability(monkeypatch, *, exit_supported):
    from config import session_capability

    class _Cap:
        pass

    cap = _Cap()
    cap.exit_supported = exit_supported
    monkeypatch.setattr(session_capability, "capability_at",
                        lambda *a, **k: cap)


class TestAnOrderAlreadyAtTheBrokerAlwaysDefers:
    """Something is live; a second order must not race it, whatever the
    session says."""

    def test_exit_submitted_defers_even_when_sell_is_impossible(
            self, conn, monkeypatch):
        pid = _open(conn)
        ps.latch_pending_exit(conn, pid, "EMA_STRUCTURE_FAILURE", now=NOW)
        ps.mark_exit_submitted(conn, pid, "EMA_STRUCTURE_FAILURE", now=NOW)
        _capability(monkeypatch, exit_supported=False)
        assert entry._exit_in_flight(NOW) is True

    def test_exit_submitted_defers_when_sell_is_possible(self, conn,
                                                         monkeypatch):
        pid = _open(conn)
        ps.latch_pending_exit(conn, pid, "EMA_STRUCTURE_FAILURE", now=NOW)
        ps.mark_exit_submitted(conn, pid, "EMA_STRUCTURE_FAILURE", now=NOW)
        _capability(monkeypatch, exit_supported=True)
        assert entry._exit_in_flight(NOW) is True


class TestALatchedExitDefersOnlyWhenItCanActuallyRun:
    def test_it_defers_while_the_exit_is_sell_capable(self, conn, monkeypatch):
        """The original rationale: the exit runtime is about to want the
        lock, and the position is already at risk."""
        pid = _open(conn)
        ps.latch_pending_exit(conn, pid, "EMA_STRUCTURE_FAILURE", now=NOW)
        _capability(monkeypatch, exit_supported=True)
        assert entry._exit_in_flight(NOW) is True

    def test_it_does_not_defer_when_the_exit_cannot_be_submitted(
            self, conn, monkeypatch):
        """RIG's weekend, exactly. The exit is not competing for the
        lock, so blocking on it stops trading for a reason that no
        longer exists."""
        pid = _open(conn)
        ps.latch_pending_exit(conn, pid, "EMA_STRUCTURE_FAILURE", now=NOW)
        _capability(monkeypatch, exit_supported=False)
        assert entry._exit_in_flight(NOW) is False

    def test_the_latched_reason_is_untouched_either_way(self, conn,
                                                        monkeypatch):
        """Not deferring must not become a way of dropping the exit."""
        pid = _open(conn)
        ps.latch_pending_exit(conn, pid, "EMA_STRUCTURE_FAILURE", now=NOW)
        _capability(monkeypatch, exit_supported=False)
        entry._exit_in_flight(NOW)
        row = ps.load(conn, pid)
        assert row["status"] == ps.EXIT_PENDING
        assert row["pending_exit_reason"] == "EMA_STRUCTURE_FAILURE"
        assert row["quantity"] == 3


class TestNoPendingExitDoesNotDefer:
    def test_a_plain_open_position_does_not_defer(self, conn, monkeypatch):
        _open(conn)
        _capability(monkeypatch, exit_supported=True)
        assert entry._exit_in_flight(NOW) is False

    def test_an_empty_book_does_not_defer(self, conn, monkeypatch):
        _capability(monkeypatch, exit_supported=True)
        assert entry._exit_in_flight(NOW) is False


class TestAmbiguityDefers:
    def test_an_undeterminable_capability_defers(self, conn, monkeypatch):
        """An exit may well be about to run, and deferring the entry is
        the cheaper mistake."""
        from config import session_capability

        pid = _open(conn)
        ps.latch_pending_exit(conn, pid, "EMA_STRUCTURE_FAILURE", now=NOW)
        monkeypatch.setattr(
            session_capability, "capability_at",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert entry._exit_in_flight(NOW) is True

    def test_an_unreadable_store_does_not_stop_trading(self, monkeypatch):
        """The gate and the runtime have their own, stronger refusals;
        failing the tick over a diagnostic stops trading for the wrong
        reason."""
        from state_store import db as state_db

        monkeypatch.setattr(
            state_db, "open_db",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
        assert entry._exit_in_flight(NOW) is False


class TestSystemicBlockersAreUnchanged:
    """A position-specific exit is not a systemic fault. These remain
    account-wide refusals and none of them were touched."""

    def test_the_refusal_reasons_still_exist(self):
        import inspect

        source = inspect.getsource(entry)
        for reason in ("ENTRY_DEFERRED_KIS_BUSY", "ENTRY_DEFERRED_S1_STALE",
                       "ENTRY_DEFERRED_EXIT_PENDING"):
            assert reason in source

    def test_reconciliation_still_gates_entries(self):
        """Checked in the cycle, not here -- but it must still be the
        thing that blocks on a mismatch."""
        import kis_live_trading

        source = inspect.getsource(kis_live_trading.run_live_buy_entry_cycle) \
            if hasattr(kis_live_trading, "run_live_buy_entry_cycle") else ""
        assert "reconciliation" in source


