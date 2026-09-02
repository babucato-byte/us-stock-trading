"""Shares S6's own order produced, that no S6 row was left tracking.

2026-09-02, HBAN. Order 0030708837 filled 7 shares at 17.01. The
execution lock was held by the entry cycle for 24 of 25 minutes, so
`sync_buy_fills` never ran; when a tick finally got in, KIS had dropped
the order from the open-order book but had not yet published the fill.
The tick read "gone, nothing filled", called it terminal, and closed the
position BUY_NEVER_FILLED.

The account then held 7 shares that no position mentioned. Nothing could
repair it: `sync_buy_fills` never revisits a CLOSED row, reconciliation
can only report the mismatch, and `ownership.may_adopt` -- which exists
for exactly this -- was called only by S1.

Adoption is deliberately hard to satisfy. Ownership, a FILLED order in
the ledger, and a real fill price from the broker: all three, or the
mismatch stands for a human to look at.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s6_live import fill_adoption, position_store  # noqa: E402

NOW = datetime(2026, 9, 2, 15, 10, tzinfo=timezone.utc)
S6 = position_store.STRATEGY_ID


class _Position:
    def __init__(self, symbol, quantity, average_fill_price):
        self.symbol = symbol
        self.quantity = quantity
        self.average_fill_price = average_fill_price


class _Broker:
    def __init__(self, positions=(), fail=False):
        self._positions = list(positions)
        self._fail = fail

    def get_positions(self):
        if self._fail:
            raise RuntimeError("KIS unreachable")
        return self._positions


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADING_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "pos.json"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def _ledger(conn, *, symbol="HBAN", status="FILLED", strategy_id=S6,
            order_id="0030708837", quantity=7):
    conn.execute(
        "INSERT INTO kis_order_idempotency (internal_order_id, signal_id, "
        "symbol, side, trading_date, broker_order_id, status, created_at, "
        "updated_at, requested_quantity, strategy_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (f"iid-{symbol}-{order_id}", f"sig-{symbol}", symbol, "buy",
         "2026-09-02", order_id, status, NOW.isoformat(), NOW.isoformat(),
         quantity, strategy_id))
    conn.commit()


def _held(symbol="HBAN", quantity=7, price=17.01):
    return _Broker([_Position(symbol, quantity, price)])


class TestTheHbanCase:
    def test_a_confirmed_fill_nothing_tracks_is_adopted(self, conn):
        _ledger(conn)
        out = fill_adoption.adopt_untracked_fills(conn, broker=_held(), now=NOW)
        assert [r["action"] for r in out] == [fill_adoption.ADOPTED]

        live = position_store.load_live(conn)
        assert len(live) == 1
        _pid, row = live[0]
        assert row["symbol"] == "HBAN"
        assert row["status"] == position_store.OPEN
        assert row["quantity"] == 7

    def test_it_is_recorded_at_the_real_fill_price(self, conn):
        _ledger(conn)
        fill_adoption.adopt_untracked_fills(conn, broker=_held(), now=NOW)
        _pid, row = position_store.load_live(conn)[0]
        assert row["entry_price"] == pytest.approx(17.01), (
            "the structural stop is measured from this; it may only ever "
            "be the broker's own number")

    def test_the_broker_order_id_is_carried_onto_the_row(self, conn):
        _ledger(conn)
        fill_adoption.adopt_untracked_fills(conn, broker=_held(), now=NOW)
        _pid, row = position_store.load_live(conn)[0]
        assert row["entry_order_id"] == "0030708837"

    def test_the_exit_runtime_can_now_see_it(self, conn):
        """The entire point: an adopted row is a managed row."""
        _ledger(conn)
        assert not position_store.load_live(conn)
        fill_adoption.adopt_untracked_fills(conn, broker=_held(), now=NOW)
        assert position_store.load_live(conn), (
            "an adopted position must appear to the exit monitor")


class TestItRefusesWithoutEvidence:
    def test_no_filled_order_in_the_ledger_refuses(self, conn):
        _ledger(conn, status="ACCEPTED")  # still resting, not proof of shares
        out = fill_adoption.adopt_untracked_fills(conn, broker=_held(), now=NOW)
        assert [r["action"] for r in out] == [fill_adoption.SKIPPED_NO_FILLED_ORDER]
        assert not position_store.load_live(conn)

    def test_no_ledger_row_at_all_refuses(self, conn):
        out = fill_adoption.adopt_untracked_fills(conn, broker=_held(), now=NOW)
        assert [r["action"] for r in out] == [fill_adoption.SKIPPED_NO_FILLED_ORDER]

    def test_an_unusable_fill_price_refuses(self, conn):
        _ledger(conn)
        broker = _Broker([_Position("HBAN", 7, None)])
        out = fill_adoption.adopt_untracked_fills(conn, broker=broker, now=NOW)
        assert [r["action"] for r in out] == [fill_adoption.SKIPPED_NO_FILL_PRICE]
        assert not position_store.load_live(conn), (
            "a position opened at a guessed price looks correct and is not")

    def test_a_zero_fill_price_refuses(self, conn):
        _ledger(conn)
        broker = _Broker([_Position("HBAN", 7, 0.0)])
        out = fill_adoption.adopt_untracked_fills(conn, broker=broker, now=NOW)
        assert [r["action"] for r in out] == [fill_adoption.SKIPPED_NO_FILL_PRICE]

    def test_another_strategys_holding_is_refused(self, conn):
        _ledger(conn, strategy_id="S1_MOMENTUM_V1")
        out = fill_adoption.adopt_untracked_fills(conn, broker=_held(), now=NOW)
        assert out and out[0]["action"] in (
            fill_adoption.SKIPPED_NOT_OURS, fill_adoption.SKIPPED_NO_FILLED_ORDER)
        assert not position_store.load_live(conn), (
            "adopting on doubt is how one position gets two exit engines")

    def test_an_unreadable_broker_adopts_nothing(self, conn):
        _ledger(conn)
        out = fill_adoption.adopt_untracked_fills(
            conn, broker=_Broker(fail=True), now=NOW)
        assert out == []


class TestItDoesNotDisturbWhatIsAlreadyTracked:
    def test_a_tracked_position_is_left_alone(self, conn):
        _ledger(conn)
        pid = position_store.record_submission(
            conn, symbol="HBAN", variant="S6-R", entry_session="REGULAR",
            client_order_id="cid", now=NOW)
        position_store.open_from_fill(conn, pid, quantity=7,
                                      average_fill_price=17.01, now=NOW)
        out = fill_adoption.adopt_untracked_fills(conn, broker=_held(), now=NOW)
        assert out == []
        assert len(position_store.load_live(conn)) == 1

    def test_a_submitted_row_is_not_duplicated(self, conn):
        _ledger(conn)
        position_store.record_submission(
            conn, symbol="HBAN", variant="S6-R", entry_session="REGULAR",
            client_order_id="cid", now=NOW)
        out = fill_adoption.adopt_untracked_fills(conn, broker=_held(), now=NOW)
        assert out == [], "the fill sync owns a SUBMITTED row, not adoption"

    def test_running_twice_adopts_once(self, conn):
        _ledger(conn)
        fill_adoption.adopt_untracked_fills(conn, broker=_held(), now=NOW)
        second = fill_adoption.adopt_untracked_fills(conn, broker=_held(), now=NOW)
        assert second == []
        assert len(position_store.load_live(conn)) == 1


class TestStrategyContextIsCarriedForward:
    def test_the_range_and_vwap_of_the_wrongly_closed_row_are_kept(self, conn):
        """The exit rules need them, and they must not be re-derived from
        today's market -- that would silently move the stop."""
        _ledger(conn)
        pid = position_store.record_submission(
            conn, symbol="HBAN", variant="S6-R", entry_session="REGULAR",
            client_order_id="cid", range_minutes=15, range_high=17.2,
            range_low=16.8, entry_vwap=17.0, entry_ema9=16.95,
            entry_ema21=16.9, entry_volume_expansion=2.4, now=NOW)
        position_store.abandon_submission(conn, pid, reason="BUY_NEVER_FILLED",
                                          now=NOW)
        fill_adoption.adopt_untracked_fills(conn, broker=_held(), now=NOW)

        _pid, row = position_store.load_live(conn)[0]
        assert row["range_high"] == pytest.approx(17.2)
        assert row["range_low"] == pytest.approx(16.8)
        assert row["entry_vwap"] == pytest.approx(17.0)
        assert row["variant"] == "S6-R"

    def test_adoption_still_works_with_no_prior_context(self, conn):
        _ledger(conn)
        fill_adoption.adopt_untracked_fills(conn, broker=_held(), now=NOW)
        assert len(position_store.load_live(conn)) == 1, (
            "a position with no range is still worth managing on its stop")
