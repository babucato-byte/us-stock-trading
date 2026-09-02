"""The 2026-09-02 HBAN sequence, end to end, and the signal that would
have caught it.

The sequence:

  14:55  BUY 7 HBAN accepted, order 0030708837
  ~14:58 it FILLS. KIS removes it from the open-order book immediately
         and does not publish the execution rows until 15:02.
  15:00  a runtime tick finally wins the execution lock. `sync_buy_fills`
         asks about the order: gone from the book, no fill rows. The old
         rule called that terminal, so the position was closed
         BUY_NEVER_FILLED.
  15:03  reconciliation: "position exists at KIS but not tracked
         internally: internal=0 KIS=7".

Nothing downstream could repair that, and no signal existed to say the
monitor had been starved for the twenty-four minutes that caused it.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from brokers import kis_fill_inquiry as fq  # noqa: E402
from s6_live import exit_runtime, monitor_health, position_store  # noqa: E402

FILL_AT = datetime(2026, 9, 2, 14, 58, tzinfo=timezone.utc)
TICK = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)
SUBMITTED_AT = datetime(2026, 9, 2, 14, 55, tzinfo=timezone.utc)


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADING_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "pos.json"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


class _Broker:
    """KIS in the gap: the order has left the book, the fill is not out."""

    def __init__(self, fills=(), open_orders=()):
        self._fills = list(fills)
        self._open = list(open_orders)

    def get_fills(self, *, start_date, end_date):
        return self._fills

    def get_open_orders(self):
        return self._open


class TestTheFillPublicationGap:
    def test_a_filled_order_in_the_gap_is_not_called_terminal(self):
        report = fq.inquire(
            _Broker(fills=[], open_orders=[]), broker_order_id="0030708837",
            ordered_quantity=7, now=TICK, since=SUBMITTED_AT)
        assert report.terminal is False, (
            "two minutes after the fill, absence from the book is not "
            "evidence the order never filled")
        assert report.as_store_fill() is None

    def test_sync_buy_fills_leaves_the_position_alone_in_the_gap(self, conn):
        pid = position_store.record_submission(
            conn, symbol="HBAN", variant="S6-R", entry_session="REGULAR",
            client_order_id="cid-hban", now=SUBMITTED_AT)

        def lookup(row):
            return fq.inquire(
                _Broker(fills=[], open_orders=[]),
                broker_order_id="0030708837", ordered_quantity=7,
                now=TICK, since=SUBMITTED_AT).as_store_fill()

        out = exit_runtime.sync_buy_fills(conn, fills_for=lookup, now=TICK)
        assert [r.get("status") for r in out] == ["STILL_UNCONFIRMED"]

        row = position_store.load_by_symbol(conn, "HBAN")
        assert row is not None, "the row must survive for the fill to land on"
        assert row["status"] == position_store.SUBMITTED, (
            "this is the abandonment that orphaned seven HBAN shares")

    def test_the_row_still_dies_once_the_window_has_passed(self, conn):
        """A genuinely dead order must not hold the slot forever."""
        position_store.record_submission(
            conn, symbol="HBAN", variant="S6-R", entry_session="REGULAR",
            client_order_id="cid-hban", now=SUBMITTED_AT)
        late = SUBMITTED_AT + timedelta(
            seconds=fq.NO_FILL_CONFIRMATION_GRACE_SECONDS + 120)

        def lookup(row):
            return fq.inquire(
                _Broker(fills=[], open_orders=[]),
                broker_order_id="0030708837", ordered_quantity=7,
                now=late, since=SUBMITTED_AT).as_store_fill()

        out = exit_runtime.sync_buy_fills(conn, fills_for=lookup, now=late)
        assert [r.get("status") for r in out] == ["ABANDONED"]


class TestTheMonitorHealthSignal:
    def test_flat_is_not_stale(self, conn, tmp_path):
        out = monitor_health.check(conn, now=TICK,
                                   path=str(tmp_path / "hb.json"))
        assert out["status"] == monitor_health.STATUS_FLAT, (
            "silence with nothing held is correct, not a fault")

    def test_a_held_position_never_evaluated_is_unknown(self, conn, tmp_path):
        _open_position(conn)
        out = monitor_health.check(conn, now=TICK,
                                   path=str(tmp_path / "hb.json"))
        assert out["status"] == monitor_health.STATUS_UNKNOWN

    def test_a_recently_evaluated_position_is_ok(self, conn, tmp_path):
        _open_position(conn)
        path = str(tmp_path / "hb.json")
        monitor_health.record_evaluation(held_count=1,
                                         now=TICK - timedelta(seconds=30),
                                         path=path)
        out = monitor_health.check(conn, now=TICK, path=path)
        assert out["status"] == monitor_health.STATUS_OK

    def test_the_starvation_would_have_been_reported(self, conn, tmp_path):
        """24 minutes of a held position going unevaluated."""
        _open_position(conn)
        path = str(tmp_path / "hb.json")
        monitor_health.record_evaluation(held_count=1,
                                         now=TICK - timedelta(minutes=24),
                                         path=path)
        out = monitor_health.check(conn, now=TICK, path=path)
        assert out["status"] == monitor_health.STATUS_STALE
        assert out["symbols"] == ["HBAN"]
        assert out["age_seconds"] == pytest.approx(24 * 60, abs=1)

    def test_the_health_check_places_no_orders_and_changes_no_row(self, conn, tmp_path):
        """It reports. That is all it is allowed to do."""
        _open_position(conn)
        before = dict(position_store.load_by_symbol(conn, "HBAN"))
        path = str(tmp_path / "hb.json")
        monitor_health.record_evaluation(held_count=1,
                                         now=TICK - timedelta(minutes=30),
                                         path=path)
        monitor_health.check(conn, now=TICK, path=path)
        after = dict(position_store.load_by_symbol(conn, "HBAN"))
        assert after == before

    def test_an_unwritable_heartbeat_does_not_raise(self, tmp_path):
        assert monitor_health.record_evaluation(
            held_count=1, now=TICK,
            path=str(tmp_path / "no" / "such" / "dir" / "..." / "hb.json")
        ) in (True, False)


def _open_position(conn, symbol="HBAN", qty=7, price=17.01):
    pid = position_store.record_submission(
        conn, symbol=symbol, variant="S6-R", entry_session="REGULAR",
        client_order_id=f"cid-{symbol}", now=SUBMITTED_AT)
    position_store.open_from_fill(conn, pid, quantity=qty,
                                  average_fill_price=price, now=FILL_AT)
    return pid


RUNTIME_SOURCE = (REPO_ROOT / "scripts/run_s6_runtime.py").read_text()


class TestTheRuntimeCanReachTheRepair:
    def test_adoption_runs_before_the_no_positions_early_return(self):
        """The orphan state IS the no-rows state.

        `run_once` returns NO_S6_POSITIONS when the store has no live or
        unconfirmed rows -- which is exactly the state seven untracked
        HBAN shares produced. Adoption placed after that check could
        never run in the one case it exists for.
        """
        adopt = RUNTIME_SOURCE.index("_adopt_untracked_when_flat(")
        early = RUNTIME_SOURCE.index('report["status"] = "NO_S6_POSITIONS"')
        assert adopt < early, (
            "adoption must be attempted before the tick concludes there "
            "is nothing to do")

    def test_adoption_runs_before_the_entry_timeout(self):
        """An adopted row must be visible to the timeout, or the timeout
        will close the position that was just recovered."""
        adopted = RUNTIME_SOURCE.index('("adopted_fills"')
        timeouts = RUNTIME_SOURCE.index('("entry_timeouts"')
        assert adopted < timeouts

    def test_the_fill_sync_still_runs_before_adoption(self):
        """Adoption is the repair for a MISSED fill. A fill the normal
        path can still apply must go through the normal path."""
        fills = RUNTIME_SOURCE.index('("buy_fills"')
        adopted = RUNTIME_SOURCE.index('("adopted_fills"')
        assert fills < adopted

    def test_the_heartbeat_is_only_stamped_after_the_stages_ran(self):
        """A tick that could not take the lock never reaches the stages,
        and must not claim to have evaluated anything."""
        stages_done = RUNTIME_SOURCE.index('report["status"] = "ERROR" if')
        stamped = RUNTIME_SOURCE.index("monitor_health.record_evaluation(")
        assert stages_done < stamped
