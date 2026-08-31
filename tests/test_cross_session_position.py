"""One position, one strategy, however many sessions it lives through.

Session is context, not identity. A position opened in PREMARKET and
sold in AFTER_HOURS is one S6 trade, not two, and nothing at a session
boundary may reset, re-key or discard it. The record has to carry which
session each leg happened in so the trade can be studied by session
later -- while the position itself stays the same row throughout.
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

DAY = datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def _open_in(conn, session, symbol="AAA", now=DAY, quantity=2):
    pid = ps.record_submission(conn, symbol=symbol, variant="S6-R",
                               entry_session=session,
                               client_order_id=f"kislive-{symbol}-1", now=now)
    ps.open_from_fill(conn, pid, quantity=quantity, average_fill_price=10.0,
                      venue="NYSE", now=now)
    return pid


class TestAPositionSurvivesEverySessionBoundary:
    @pytest.mark.parametrize("entry,exit_session", [
        ("OVERNIGHT_DAYTIME", "PREMARKET"),
        ("PREMARKET", "REGULAR"),
        ("PREMARKET", "AFTER_HOURS"),
        ("REGULAR", "AFTER_HOURS"),
        ("AFTER_HOURS", "OVERNIGHT_DAYTIME"),
    ])
    def test_it_is_the_same_position_from_entry_to_exit(self, conn, entry,
                                                        exit_session):
        pid = _open_in(conn, entry)
        ps.close_position(conn, pid, reason="SESSION_EXIT", exit_price=10.5,
                          exit_session=exit_session, now=DAY + timedelta(hours=6))
        row = ps.load(conn, pid)
        assert row["status"] == "CLOSED"
        assert row["entry_session"] == entry
        assert row["exit_session"] == exit_session
        assert row["quantity"] == 2

    def test_the_entry_session_is_never_rewritten_by_the_exit(self, conn):
        pid = _open_in(conn, "PREMARKET")
        ps.close_position(conn, pid, reason="SESSION_EXIT", exit_price=10.5,
                          exit_session="AFTER_HOURS", now=DAY + timedelta(hours=8))
        assert ps.load(conn, pid)["entry_session"] == "PREMARKET"

    def test_a_position_held_across_a_boundary_is_still_live(self, conn):
        """Nothing at a boundary retires an open position."""
        pid = _open_in(conn, "PREMARKET")
        live = [p for p, _r in ps.load_live(conn)]
        assert pid in live

    def test_it_keeps_one_strategy_identity(self, conn):
        """Four sessions, one strategy -- not four strategies."""
        from config import s6_sessions

        pid = _open_in(conn, "OVERNIGHT_DAYTIME")
        assert ps.load(conn, pid)["symbol"] == "AAA"
        assert s6_sessions.STRATEGY_ID == "S6_ORB_BREAKOUT_V1"
        for session in ("OVERNIGHT_DAYTIME", "PREMARKET", "REGULAR",
                        "AFTER_HOURS"):
            assert s6_sessions.variant_for(session)


class TestALatchedExitCrossesSessionsToo:
    def test_a_latch_in_one_session_sells_in_the_next(self, conn):
        """The RIG shape: latched when orders were not permitted, sold
        when a sell-capable session came back -- on the stored reason."""
        from s6_live import exit_runtime

        pid = _open_in(conn, "AFTER_HOURS", quantity=3)
        ps.latch_pending_exit(conn, pid, "EMA_STRUCTURE_FAILURE", now=DAY)

        seen = {}

        class _Adapter:
            def submit_order(self, symbol, quantity, *, side, client_order_id):
                seen["quantity"] = quantity

                class _R:
                    status_code = 200

                    def json(self):
                        return {"output": {"ODNO": "0000009999"}}

                return _R()

        exit_runtime.retry_latched_exits(
            conn, broker_adapter=_Adapter(), session="OVERNIGHT_DAYTIME",
            now=DAY + timedelta(hours=12), orders_allowed=True)
        assert seen["quantity"] == 3
        row = ps.load(conn, pid)
        assert (row["exit_reason"] or row["pending_exit_reason"]) \
            == "EMA_STRUCTURE_FAILURE"

    def test_the_latch_survives_the_boundary_untouched(self, conn):
        pid = _open_in(conn, "REGULAR", quantity=3)
        ps.latch_pending_exit(conn, pid, "VWAP_FAILURE", now=DAY)
        # A later session's evaluation must not relabel it.
        ps.latch_pending_exit(conn, pid, "SESSION_EXIT",
                              now=DAY + timedelta(hours=10))
        assert ps.load(conn, pid)["pending_exit_reason"] == "VWAP_FAILURE"
