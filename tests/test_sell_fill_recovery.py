"""Collecting a SELL fill when the intent never recorded its order id.

The failure this exists for
---------------------------
S6 sold DT. The order reached KIS, was accepted as 0030785946, and
filled. The position then sat in EXIT_SUBMITTED indefinitely and every
runtime tick logged:

    S6 sell fill inquiry unusable for DT: no broker order id was
    recorded; there is nothing to look up at KIS

Two independent defects, either of which alone would have stalled it:

1. `_submit_sell` called `mark_submitted(conn, intent_id)` and dropped
   the broker order id, though the accepted response carried it and
   `mark_submitted` has taken a `broker_order_id` all along. The intent
   was SUBMITTED with a NULL id.

2. `_order_id_for`'s sell branch read that NULL and gave up. The buy
   branch, for the identical situation, falls back to the durable order
   ledger keyed on the client id -- which is why the BUY reconciled
   itself and the SELL could not.

The order id was in `kis_order_idempotency` the entire time, under the
same client id the intent was reserved with.

Nothing here places an order.
"""

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from state_store import exit_intent_ledger as eil  # noqa: E402

NOW = datetime(2026, 8, 26, 17, 4, tzinfo=timezone.utc)
ODNO = "0030785946"
CLIENT = "s6exit-DT-14131f0f1c62"
POSITION = "s6pos_61e91675e66742c6"


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def _ledger(conn, *, internal_order_id=CLIENT, broker_order_id=ODNO,
            side="sell", symbol="DT"):
    """The durable record the submitter writes before anything else."""
    conn.execute(
        "INSERT INTO kis_order_idempotency (internal_order_id, signal_id, "
        "symbol, side, trading_date, broker_order_id, status, created_at, "
        "updated_at, requested_quantity, version, strategy_id) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?)",
        (internal_order_id, "sig", symbol, side, "2026-08-26",
         broker_order_id, "ACCEPTED", NOW.isoformat(), NOW.isoformat(),
         1.0, 1, "S6_ORB_BREAKOUT_V1"))
    conn.commit()


def _intent(conn, *, broker_order_id=None, client_order_id=CLIENT,
            position_id=POSITION):
    intent_id = eil.reserve(conn, position_id, "RANGE_REENTRY", 1.0,
                            client_order_id)
    eil.mark_submitted(conn, intent_id, broker_order_id=broker_order_id)
    return intent_id


def _row(position_id=POSITION, symbol="DT"):
    return {"position_id": position_id, "symbol": symbol, "quantity": 1,
            "updated_at": NOW.isoformat()}


class TestTheSellOrderIdIsFoundEvenWhenTheIntentLostIt:
    def test_the_exact_DT_case(self, conn):
        """Intent SUBMITTED with a NULL broker id; the ledger has it."""
        from scripts.run_s6_runtime import _order_id_for

        _ledger(conn)
        _intent(conn, broker_order_id=None)

        order_id, _since = _order_id_for(conn, _row(), side="sell")
        assert order_id == ODNO

    def test_the_intents_own_id_still_wins_when_it_has_one(self, conn):
        """The fallback must not override a recorded id."""
        from scripts.run_s6_runtime import _order_id_for

        _ledger(conn, broker_order_id="0000000000")
        _intent(conn, broker_order_id=ODNO)

        order_id, _since = _order_id_for(conn, _row(), side="sell")
        assert order_id == ODNO

    def test_no_intent_and_no_ledger_is_still_None(self, conn):
        """Absence must stay absence -- inventing an id would inquire
        about somebody else's order."""
        from scripts.run_s6_runtime import _order_id_for

        order_id, _since = _order_id_for(conn, _row(), side="sell")
        assert order_id is None

    def test_it_does_not_reach_across_positions(self, conn):
        """Another position's SELL is not this position's SELL."""
        from scripts.run_s6_runtime import _order_id_for

        _ledger(conn)
        _intent(conn, broker_order_id=None)

        order_id, _since = _order_id_for(
            conn, _row(position_id="s6pos_other", symbol="TX"), side="sell")
        assert order_id is None

    def test_the_buy_branch_is_unchanged(self, conn):
        """The buy fallback already worked; it must keep working."""
        from scripts.run_s6_runtime import _order_id_for

        _ledger(conn, internal_order_id="kislive-DT-65372efeb3d2",
                broker_order_id="0030740200", side="buy")
        row = dict(_row(), client_order_id="kislive-DT-65372efeb3d2",
                   entry_order_id=None, submitted_at=NOW.isoformat())
        order_id, _since = _order_id_for(conn, row, side="buy")
        assert order_id == "0030740200"


class TestTheSubmitterRecordsTheOrderId:
    def test_an_accepted_response_puts_its_id_on_the_intent(self, conn):
        """The origin defect: the id was in hand and thrown away."""
        from s1_live.exit_runtime import _broker_order_id

        class Response:
            data = {"id": ODNO, "status": "accepted"}

        assert _broker_order_id(Response()) == ODNO

    @pytest.mark.parametrize("response", [
        type("R", (), {"data": None})(),
        type("R", (), {"data": {}})(),
        type("R", (), {"data": {"id": None}})(),
        type("R", (), {})(),
    ])
    def test_a_missing_id_is_None_not_a_crash(self, response):
        """The order is already accepted by this point. Failing to parse
        the id must not undo recording the submission."""
        from s1_live.exit_runtime import _broker_order_id

        assert _broker_order_id(response) is None

    def test_it_is_actually_passed_to_mark_submitted(self):
        """Available is not wired. The id has to reach the ledger."""
        import inspect

        from s1_live import exit_runtime

        source = inspect.getsource(exit_runtime._submit_sell)
        assert "_broker_order_id(response)" in source

    def test_mark_submitted_persists_it(self, conn):
        intent_id = _intent(conn, broker_order_id=ODNO)
        row = eil.get_by_id(conn, intent_id)
        assert row["broker_order_id"] == ODNO
        assert row["state"] == eil.STATE_SUBMITTED


class TestTheMonitorKeepsWatchingAPositionMidExit:
    """The guard that went quiet at the worst possible moment."""

    WRAPPER = REPO_ROOT / "deploy" / "cron" / "s6_exit_monitor.sh"

    def test_it_does_not_hand_copy_the_status_list(self):
        """Checked against the statement it runs, not the file: the
        drifted list is quoted in a comment there deliberately."""
        text = self.WRAPPER.read_text(encoding="utf-8")
        sql = [ln for ln in text.splitlines()
               if "s6_positions" in ln and not ln.strip().startswith("#")]
        assert sql, "the guard's query disappeared"
        for line in sql:
            assert "'OPEN'" not in line and "'EXIT_PENDING'" not in line
        assert "from s6_live.position_store import LIVE_STATUSES" in text

    def test_the_canonical_list_covers_an_exit_in_flight(self):
        """EXIT_SUBMITTED is when an order is live at the broker and the
        fill still has to be collected -- the least safe moment to stop
        looking."""
        from s6_live.position_store import (EXIT_PENDING, EXIT_SUBMITTED,
                                            LIVE_STATUSES, OPEN, SUBMITTED)

        for status in (SUBMITTED, OPEN, EXIT_PENDING, EXIT_SUBMITTED):
            assert status in LIVE_STATUSES


class TestReleasingAMisattributedRowIsAudited:
    def test_the_audit_event_is_actually_written(self, conn, monkeypatch):
        """It was called with `detail=`, which record_event does not
        accept -- every release raised TypeError and was swallowed, so
        the repair happened with no trail."""
        from s1_live import position_store as s1ps
        from s6_live import position_store as s6ps
        from reconciliation import ownership

        recorded = []
        import shadow_audit
        monkeypatch.setattr(shadow_audit, "record_event",
                            lambda **kw: recorded.append(kw))

        _ledger(conn, internal_order_id="kislive-DT-1",
                broker_order_id="0030740200", side="buy")
        pid = s6ps.record_submission(conn, symbol="DT", variant="S6-R",
                                     entry_session="REGULAR",
                                     client_order_id="kislive-DT-1", now=NOW)
        s6ps.open_from_fill(conn, pid, quantity=1, average_fill_price=50.79,
                            venue="NYSE", now=NOW)
        s1ps.open_position(conn, symbol="DT",
                           strategy_id="S1_HMA_EARLY_TREND_V1",
                           signal_id="s1-fill-DT", entry_price=50.79,
                           quantity=1, now=NOW)

        out = ownership.release_misattributed(
            conn, symbol="DT", strategy_id="S1_HMA_EARLY_TREND_V1", now=NOW)
        assert out["released"] is True
        assert len(recorded) == 1
        assert recorded[0]["reason_code"] == ownership.OWNERSHIP_CONFLICT
        assert "detail" not in recorded[0]


class TestTheExitIntentIsClosedOutWithThePosition:
    """The position book and the intent ledger are two records of one
    exit. Closing only the first leaves an exit permanently 'in flight'
    on a position whose shares are already sold."""

    def _position(self, conn, quantity=1):
        from s6_live import position_store as s6ps

        pid = s6ps.record_submission(conn, symbol="DT", variant="S6-R",
                                     entry_session="REGULAR",
                                     client_order_id="kislive-DT-1", now=NOW)
        s6ps.open_from_fill(conn, pid, quantity=quantity,
                            average_fill_price=50.79, venue="NYSE", now=NOW)
        s6ps.latch_pending_exit(conn, pid, "RANGE_REENTRY", now=NOW)
        s6ps.mark_exit_submitted(conn, pid, "RANGE_REENTRY", now=NOW)
        return pid

    def test_a_full_fill_confirms_the_intent(self, conn):
        from s6_live import exit_runtime

        pid = self._position(conn)
        intent_id = _intent(conn, broker_order_id=ODNO, position_id=pid)

        out = exit_runtime.sync_sell_fills(
            conn, fills_for=lambda row: {"filled_quantity": 1,
                                         "average_fill_price": 50.87},
            now=NOW)
        assert out[0]["status"] == "CLOSED"
        assert out[0]["exit_price"] == 50.87

        row = eil.get_by_id(conn, intent_id)
        assert row["state"] == eil.STATE_CONFIRMED
        assert row["confirmed_filled_qty"] == 1
        assert eil.get_active_intent(conn, pid) is None

    def test_a_partial_fill_records_progress_and_stays_open(self, conn):
        """The remainder is still live at the broker -- confirming the
        intent there would abandon it."""
        from s6_live import exit_runtime

        pid = self._position(conn, quantity=3)
        intent_id = _intent(conn, broker_order_id=ODNO, position_id=pid)

        out = exit_runtime.sync_sell_fills(
            conn, fills_for=lambda row: {"filled_quantity": 1,
                                         "average_fill_price": 50.87},
            now=NOW)
        assert out[0]["status"] == "PARTIALLY_SOLD"
        assert out[0]["remaining"] == 2

        row = eil.get_by_id(conn, intent_id)
        assert row["state"] == eil.STATE_SUBMITTED
        assert row["confirmed_filled_qty"] == 1

    def test_a_missing_intent_does_not_break_the_close(self, conn):
        """A closed position must not depend on its ledger row."""
        from s6_live import exit_runtime
        from s6_live import position_store as s6ps

        pid = self._position(conn)
        out = exit_runtime.sync_sell_fills(
            conn, fills_for=lambda row: {"filled_quantity": 1,
                                         "average_fill_price": 50.87},
            now=NOW)
        assert out[0]["status"] == "CLOSED"
        assert s6ps.load(conn, pid)["status"] == "CLOSED"

    def test_an_unwritable_ledger_does_not_undo_the_close(self, conn, monkeypatch):
        from s6_live import exit_runtime
        from s6_live import position_store as s6ps
        from state_store import exit_intent_ledger

        pid = self._position(conn)
        _intent(conn, broker_order_id=ODNO, position_id=pid)
        monkeypatch.setattr(
            exit_intent_ledger, "mark_confirmed",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ledger down")))

        out = exit_runtime.sync_sell_fills(
            conn, fills_for=lambda row: {"filled_quantity": 1,
                                         "average_fill_price": 50.87},
            now=NOW)
        assert out[0]["status"] == "CLOSED"
        assert s6ps.load(conn, pid)["status"] == "CLOSED"
