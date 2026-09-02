"""An exit the broker no longer backs, and cannot be confirmed either.

2026-09-01. MTCH sat EXIT_SUBMITTED qty 2 for five and a half hours:

  * broker held no MTCH, and had no open order
  * our ledger said the sell settled FILLED 2.0 of 2.0
  * KIS's execution history contained NO such sell -- while returning
    full detail for all ten other S6 sells in the same window
  * `sync_sell_fills` hit `if sold <= 0: continue` on every tick, which
    produced no result, no log and no escalation
  * every S6 entry deferred behind the stale row

Two failures. The silent branch, which made a permanent stall look like
nothing happening. And the absence of any way to retire a position whose
shares are provably gone but whose execution cannot be corroborated.

The second is resolved with a deliberately weaker claim than a fill: no
exit price, therefore no derived PnL, and a reason that says only what
survives scrutiny.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s6_live import exit_runtime, position_store  # noqa: E402

NOW = datetime(2026, 9, 1, 22, 0, tzinfo=timezone.utc)


class _Broker:
    def __init__(self, positions=(), orders=(), fail=False):
        self._p, self._o, self._fail = list(positions), list(orders), fail

    def get_positions(self):
        if self._fail:
            raise RuntimeError("KIS unreachable")
        return [type("P", (), {"symbol": s})() for s in self._p]

    def get_open_orders(self):
        if self._fail:
            raise RuntimeError("KIS unreachable")
        return [{"symbol": s} for s in self._o]


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADING_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "pos.json"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def _exit_submitted(conn, symbol="MTCH", qty=2):
    """A row in exactly MTCH's state: OPEN from a real fill, then exited."""
    pid = position_store.record_submission(
        conn, symbol=symbol, variant="S6-R", entry_session="REGULAR",
        client_order_id=f"cid-{symbol}", now=NOW)
    position_store.open_from_fill(
        conn, pid, quantity=qty, average_fill_price=41.3675,
        venue="NASD", entry_order_id="0030447726", now=NOW)
    position_store.mark_exit_submitted(conn, pid, "EMA_STRUCTURE_FAILURE",
                                       now=NOW)
    return pid


class TestTheSilentBranchIsGone:
    def test_a_zero_fill_inquiry_produces_a_visible_result(self, conn):
        _exit_submitted(conn)
        out = exit_runtime.sync_sell_fills(
            conn, fills_for=lambda row: {"filled_quantity": 0,
                                         "status": "NO_FILL",
                                         "order_id": "0030471295"},
            session="REGULAR", now=NOW)

        assert [r["status"] for r in out] == [exit_runtime.SELL_FILL_REPORTS_ZERO]
        assert out[0]["inquiry_status"] == "NO_FILL"
        assert out[0]["broker_order_id"] == "0030471295"

    def test_a_zero_fill_inquiry_does_not_close_anything(self, conn):
        pid = _exit_submitted(conn)
        exit_runtime.sync_sell_fills(
            conn, fills_for=lambda row: {"filled_quantity": 0,
                                         "status": "NO_FILL"},
            session="REGULAR", now=NOW)
        assert position_store.load(conn, pid)["status"] == \
            position_store.EXIT_SUBMITTED


def _dead_sell(order_id="0030447726"):
    """What an inquiry returns for MTCH's actual order.

    Not `None`. None means the inquiry could not resolve -- UNKNOWN, or
    inside the fill-publication window -- and retiring on that is how
    HBAN lost its exit price on 2026-09-02, 101 seconds after its SELL
    was sent and four minutes before the fill appeared.

    MTCH's order was five and a half hours old with no execution rows
    anywhere, so the inquiry reports it TERMINAL with zero filled. That
    is evidence; None is the absence of it.
    """
    return {"filled_quantity": 0, "average_fill_price": None,
            "venue": None, "order_id": order_id, "terminal": True,
            "status": "NO_FILL"}


class TestUncorroboratedRetirement:
    def test_broker_flat_and_no_fill_evidence_retires_without_a_price(self, conn):
        pid = _exit_submitted(conn)
        out = exit_runtime.reconcile_unconfirmed_exits(
            conn, broker=_Broker(), fills_for=lambda row: _dead_sell(),
            session="REGULAR", now=NOW)

        assert out[0]["status"] == exit_runtime.EXTERNALLY_CLOSED_SELL_UNCONFIRMED
        row = position_store.load(conn, pid)
        assert row["status"] == position_store.CLOSED
        assert row["exit_price"] is None
        assert row["exit_reason"] == exit_runtime.EXTERNALLY_CLOSED_SELL_UNCONFIRMED

    def test_no_realised_pnl_can_be_derived(self, conn):
        """A NULL exit price is an absent result, never a breakeven one."""
        pid = _exit_submitted(conn)
        exit_runtime.reconcile_unconfirmed_exits(
            conn, broker=_Broker(), fills_for=lambda row: _dead_sell(), now=NOW)
        row = position_store.load(conn, pid)
        assert row["exit_price"] is None
        assert row["entry_price"] is not None

    def test_a_still_held_position_is_never_retired(self, conn):
        pid = _exit_submitted(conn)
        out = exit_runtime.reconcile_unconfirmed_exits(
            conn, broker=_Broker(positions=["MTCH"]),
            fills_for=lambda row: None, now=NOW)

        assert out[0]["status"] == "BROKER_STILL_HOLDS"
        assert position_store.load(conn, pid)["status"] == \
            position_store.EXIT_SUBMITTED

    def test_an_open_order_stops_the_retirement(self, conn):
        _exit_submitted(conn)
        out = exit_runtime.reconcile_unconfirmed_exits(
            conn, broker=_Broker(orders=["MTCH"]),
            fills_for=lambda row: None, now=NOW)
        assert out[0]["status"] == "BROKER_HAS_OPEN_ORDER"

    def test_a_usable_fill_belongs_to_the_normal_priced_path(self, conn):
        """If the price is knowable, the weaker claim must not be used."""
        pid = _exit_submitted(conn)
        out = exit_runtime.reconcile_unconfirmed_exits(
            conn, broker=_Broker(),
            fills_for=lambda row: {"filled_quantity": 2,
                                   "average_fill_price": 41.95},
            now=NOW)

        assert out[0]["status"] == "FILL_AVAILABLE_NORMAL_PATH"
        assert position_store.load(conn, pid)["status"] == \
            position_store.EXIT_SUBMITTED

    def test_an_unreadable_broker_retires_nothing(self, conn):
        pid = _exit_submitted(conn)
        out = exit_runtime.reconcile_unconfirmed_exits(
            conn, broker=_Broker(fail=True), fills_for=lambda row: None,
            now=NOW)

        assert out[0]["status"] == "BROKER_UNREADABLE"
        assert position_store.load(conn, pid)["status"] == \
            position_store.EXIT_SUBMITTED

    def test_retirement_is_idempotent(self, conn):
        pid = _exit_submitted(conn)
        for _ in range(3):
            exit_runtime.reconcile_unconfirmed_exits(
                conn, broker=_Broker(), fills_for=lambda row: _dead_sell(),
                now=NOW)
        row = position_store.load(conn, pid)
        assert row["status"] == position_store.CLOSED
        assert row["exit_price"] is None


class TestTheNormalPathIsUnchanged:
    def test_a_real_fill_still_closes_with_its_real_price(self, conn):
        """JBS/XP/NU/PEGA/VALE behaviour must not move."""
        pid = _exit_submitted(conn)
        out = exit_runtime.sync_sell_fills(
            conn, fills_for=lambda row: {"filled_quantity": 2,
                                         "average_fill_price": 41.95},
            session="REGULAR", now=NOW)

        assert out[0]["status"] == "CLOSED"
        row = position_store.load(conn, pid)
        assert row["status"] == position_store.CLOSED
        assert row["exit_price"] == pytest.approx(41.95)


def test_reconciliation_submits_no_order(conn):
    """Nothing in this path may reach the broker's order side."""
    source = (REPO_ROOT / "s6_live" / "exit_runtime.py").read_text()
    block = source[source.index("def reconcile_unconfirmed_exits"):]
    for forbidden in ("submit_order", "submit_sell_order", "submit_buy_order"):
        assert forbidden not in block
