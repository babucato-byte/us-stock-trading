"""The first exit reason wins, and later ticks do not relabel it.

The forensic that prompted this
-------------------------------
RIG latched EXIT_PENDING on 2026-08-28 at 19:52 with
EMA_STRUCTURE_FAILURE. A runtime report later showed RANGE_REENTRY as
the latched reason, which looked like the stored value had been
overwritten.

It had not: `pending_exit_reason` was still EMA_STRUCTURE_FAILURE. What
the report showed was the CURRENT tick's evaluation, not the latch. The
distinction matters because the stored reason is what the Monday retry
submits on, and a reason that drifted would mean selling for a
condition that is no longer the one that triggered the exit.
"""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s6_live import position_store as ps  # noqa: E402

LATCH = datetime(2026, 8, 28, 19, 52, 5, tzinfo=timezone.utc)


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def _open_rig(conn):
    pid = ps.record_submission(conn, symbol="RIG", variant="S6-R",
                               entry_session="REGULAR",
                               client_order_id="kislive-RIG-1",
                               now=LATCH - timedelta(hours=2))
    ps.open_from_fill(conn, pid, quantity=3, average_fill_price=5.85,
                      venue="NYSE", now=LATCH - timedelta(hours=2))
    return pid


class TestTheFirstReasonWins:
    def test_a_later_condition_does_not_relabel_the_exit(self, conn):
        """RIG's exact case: EMA_STRUCTURE_FAILURE latched, RANGE_REENTRY
        fires later."""
        pid = _open_rig(conn)
        assert ps.latch_pending_exit(conn, pid, "EMA_STRUCTURE_FAILURE",
                                     now=LATCH) is True
        ps.latch_pending_exit(conn, pid, "RANGE_REENTRY",
                              now=LATCH + timedelta(minutes=15))
        row = ps.load(conn, pid)
        assert row["pending_exit_reason"] == "EMA_STRUCTURE_FAILURE"

    def test_the_second_latch_reports_that_it_changed_nothing(self, conn):
        pid = _open_rig(conn)
        ps.latch_pending_exit(conn, pid, "EMA_STRUCTURE_FAILURE", now=LATCH)
        assert ps.latch_pending_exit(conn, pid, "VWAP_FAILURE",
                                     now=LATCH + timedelta(minutes=5)) is False

    def test_the_latch_timestamp_is_also_the_first_one(self, conn):
        """Otherwise "how long has this been pending" resets every tick."""
        pid = _open_rig(conn)
        ps.latch_pending_exit(conn, pid, "EMA_STRUCTURE_FAILURE", now=LATCH)
        first = ps.load(conn, pid)["pending_exit_since"]
        ps.latch_pending_exit(conn, pid, "RANGE_REENTRY",
                              now=LATCH + timedelta(hours=3))
        assert ps.load(conn, pid)["pending_exit_since"] == first

    def test_many_later_conditions_change_nothing(self, conn):
        pid = _open_rig(conn)
        ps.latch_pending_exit(conn, pid, "EMA_STRUCTURE_FAILURE", now=LATCH)
        for index, reason in enumerate(("RANGE_REENTRY", "VWAP_FAILURE",
                                        "VOLUME_DECAY", "SESSION_EXIT")):
            ps.latch_pending_exit(conn, pid, reason,
                                  now=LATCH + timedelta(minutes=index + 1))
        assert ps.load(conn, pid)["pending_exit_reason"] \
            == "EMA_STRUCTURE_FAILURE"


class TestTheGuardIsStructural:
    def test_only_an_OPEN_row_can_be_latched(self, conn):
        """Immutability comes from the status predicate, not from a
        caller remembering to check first."""
        import inspect

        source = inspect.getsource(ps.latch_pending_exit)
        assert "status = ?" in source
        assert "OPEN" in source

    def test_the_intent_is_written_down(self, conn):
        import inspect

        assert "FIRST reason wins" in inspect.getdoc(ps.latch_pending_exit)


class TestWhatTheRetryActuallySubmits:
    def test_the_stored_reason_is_what_reaches_the_broker(self, conn):
        """The point of immutability: the Monday retry sells on the
        condition that triggered the exit, not on whatever fired last."""
        from s6_live import exit_runtime

        pid = _open_rig(conn)
        ps.latch_pending_exit(conn, pid, "EMA_STRUCTURE_FAILURE", now=LATCH)
        ps.latch_pending_exit(conn, pid, "RANGE_REENTRY",
                              now=LATCH + timedelta(hours=1))

        seen = {}

        class _Adapter:
            def submit_order(self, symbol, quantity, *, side, client_order_id):
                seen["symbol"] = symbol
                seen["quantity"] = quantity

                class _R:
                    status_code = 200
                    body = {"output": {"ODNO": "0030999999"}}

                    def json(self):
                        return self.body

                return _R()

        exit_runtime.retry_latched_exits(
            conn, broker_adapter=_Adapter(),
            now=LATCH + timedelta(days=3), orders_allowed=True)
        assert seen["symbol"] == "RIG"
        assert seen["quantity"] == 3
        row = ps.load(conn, pid)
        assert (row["exit_reason"] or row["pending_exit_reason"]) \
            == "EMA_STRUCTURE_FAILURE"
