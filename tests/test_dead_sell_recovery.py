"""A SELL that died, on shares the account still holds.

FLS, 2026-09-02. SELL 0030759096 was accepted at 19:59:07, filled ZERO,
and disappeared from KIS's open-order book. The account still held the
share. The row sat EXIT_SUBMITTED for hours and nothing could move it:

  * `retry_latched_exits`         skips anything not EXIT_PENDING, and
                                  skips anything already submitted
  * `reconcile_unconfirmed_exits` only retires what the broker no longer
                                  holds -- and the broker held it
  * `sync_sell_fills`             logged SELL_FILL_REPORTS_ZERO, correctly
                                  refusing to invent a fill, and continued
  * reconciliation                reported CLEAN, because nothing was
                                  inconsistent

Nothing was wrong. Nothing was going to happen either. `exit_submitted`
is deliberately one-way -- that is the duplicate-SELL defence -- so the
only way out was a transition that did not exist.

The second half of the same lifecycle bug is HBAN, where the SELL WAS
about to be confirmed and the position was retired 101 seconds after
submission, throwing away an exit price that appeared 4 minutes later.
Absence is not resolution in either direction.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from brokers import kis_fill_inquiry as fq  # noqa: E402
from s6_live import exit_runtime, position_store  # noqa: E402
from state_store import exit_intent_ledger as eil  # noqa: E402

NOW = datetime(2026, 9, 2, 23, 30, tzinfo=timezone.utc)
SUBMITTED_AT = datetime(2026, 9, 2, 19, 59, tzinfo=timezone.utc)


class _P:
    def __init__(self, symbol, quantity):
        self.symbol, self.quantity = symbol, quantity


class _Broker:
    def __init__(self, positions=(), fail=False):
        self._p, self._fail = list(positions), fail

    def get_positions(self):
        if self._fail:
            raise RuntimeError("KIS unreachable")
        return self._p


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADING_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("POSITION_STORE_FILE", str(tmp_path / "pos.json"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def _exit_submitted(conn, symbol="FLS", qty=1, price=77.86,
                    reason="RANGE_REENTRY"):
    """A row in exactly FLS's state: OPEN from a real fill, then exited."""
    pid = position_store.record_submission(
        conn, symbol=symbol, variant="S6-R", entry_session="REGULAR",
        client_order_id=f"cid-{symbol}", now=SUBMITTED_AT)
    position_store.open_from_fill(conn, pid, quantity=qty,
                                  average_fill_price=price, now=SUBMITTED_AT)
    position_store.mark_exit_submitted(conn, pid, reason, now=SUBMITTED_AT)
    return pid


def _fill(filled=0, terminal=True, avg=None, order_id="0030759096"):
    return {"filled_quantity": filled, "average_fill_price": avg,
            "venue": "NASD", "order_id": order_id, "terminal": terminal,
            "status": "NO_FILL" if not filled else "FILLED"}


class TestTheGapItself:
    def test_the_latch_is_one_way_without_the_release(self, conn):
        """Why the position could not move: nothing clears the latch."""
        pid = _exit_submitted(conn)
        assert position_store.mark_exit_submitted(
            conn, pid, "SESSION_EXIT", now=NOW) is False, (
            "mark_exit_submitted must stay one-way -- it is the "
            "duplicate-SELL defence")

    def test_retry_latched_exits_cannot_see_an_exit_submitted_row(self, conn):
        _exit_submitted(conn)
        out = exit_runtime.retry_latched_exits(
            conn, broker_adapter=object(), now=NOW, orders_allowed=True)
        assert out == [], "this is the skip that stranded FLS"


class TestTerminalZeroFillIsRecovered:
    def test_a_dead_sell_returns_the_position_to_retryable(self, conn):
        pid = _exit_submitted(conn)
        out = exit_runtime.recover_dead_exits(
            conn, broker=_Broker([_P("FLS", 1)]),
            fills_for=lambda row: _fill(filled=0, terminal=True), now=NOW)
        assert [o["status"] for o in out] == [exit_runtime.DEAD_SELL_RELEASED]

        row = position_store.load_by_symbol(conn, "FLS")
        assert row["status"] == position_store.EXIT_PENDING
        assert not row["exit_submitted"]

    def test_the_retry_path_now_owns_it(self, conn):
        pid = _exit_submitted(conn)
        exit_runtime.recover_dead_exits(
            conn, broker=_Broker([_P("FLS", 1)]),
            fills_for=lambda row: _fill(), now=NOW)
        rows = [r for _pid, r in position_store.load_live(conn)
                if r["status"] == position_store.EXIT_PENDING
                and not r["exit_submitted"]]
        assert len(rows) == 1, (
            "retry_latched_exits selects exactly this shape")

    def test_the_original_exit_reason_survives(self, conn):
        _exit_submitted(conn, reason="RANGE_REENTRY")
        exit_runtime.recover_dead_exits(
            conn, broker=_Broker([_P("FLS", 1)]),
            fills_for=lambda row: _fill(), now=NOW)
        row = position_store.load_by_symbol(conn, "FLS")
        assert row["pending_exit_reason"] == "RANGE_REENTRY", (
            "the condition that fired still fired")

    def test_entry_economics_are_untouched(self, conn):
        _exit_submitted(conn)
        exit_runtime.recover_dead_exits(
            conn, broker=_Broker([_P("FLS", 1)]),
            fills_for=lambda row: _fill(), now=NOW)
        row = position_store.load_by_symbol(conn, "FLS")
        assert row["entry_price"] == pytest.approx(77.86)
        assert row["quantity"] == 1
        assert row["exit_price"] is None, "no exit price may be invented"
        assert row["closed_at"] is None, "recovery must not close anything"

    def test_the_dead_order_stays_in_history(self, conn):
        """Never overwrite broker evidence."""
        _exit_submitted(conn)
        out = exit_runtime.recover_dead_exits(
            conn, broker=_Broker([_P("FLS", 1)]),
            fills_for=lambda row: _fill(order_id="0030759096"), now=NOW)
        assert out[0]["dead_broker_order_id"] == "0030759096"

    def test_the_stale_intent_is_ended_so_a_retry_can_reserve(self, conn):
        pid = _exit_submitted(conn)
        eil.reserve(conn, pid, "RANGE_REENTRY", 1, "s6exit-FLS-old")
        exit_runtime.recover_dead_exits(
            conn, broker=_Broker([_P("FLS", 1)]),
            fills_for=lambda row: _fill(), now=NOW)
        assert eil.get_active_intent(conn, pid) is None, (
            "a live intent would block the retry with DuplicateExitIntent")


class TestItRefusesWithoutProof:
    def test_a_non_terminal_sell_is_left_alone(self, conn):
        _exit_submitted(conn)
        out = exit_runtime.recover_dead_exits(
            conn, broker=_Broker([_P("FLS", 1)]),
            fills_for=lambda row: _fill(terminal=False), now=NOW)
        assert [o["status"] for o in out] == ["SELL_NOT_TERMINAL"]
        assert position_store.load_by_symbol(
            conn, "FLS")["status"] == position_store.EXIT_SUBMITTED

    def test_an_unresolved_inquiry_is_left_alone(self, conn):
        """None = inside the publication window, or UNKNOWN."""
        _exit_submitted(conn)
        out = exit_runtime.recover_dead_exits(
            conn, broker=_Broker([_P("FLS", 1)]),
            fills_for=lambda row: None, now=NOW)
        assert [o["status"] for o in out] == ["SELL_NOT_TERMINAL"]

    def test_a_fully_filled_sell_belongs_to_the_normal_path(self, conn):
        _exit_submitted(conn)
        out = exit_runtime.recover_dead_exits(
            conn, broker=_Broker([_P("FLS", 1)]),
            fills_for=lambda row: _fill(filled=1, avg=78.10), now=NOW)
        assert [o["status"] for o in out] == ["FILL_AVAILABLE_NORMAL_PATH"]
        assert position_store.load_by_symbol(
            conn, "FLS")["status"] == position_store.EXIT_SUBMITTED

    def test_an_unreadable_broker_releases_nothing(self, conn):
        _exit_submitted(conn)
        out = exit_runtime.recover_dead_exits(
            conn, broker=_Broker(fail=True),
            fills_for=lambda row: _fill(), now=NOW)
        assert out == [{"status": "BROKER_UNREADABLE"}]
        assert position_store.load_by_symbol(
            conn, "FLS")["status"] == position_store.EXIT_SUBMITTED

    def test_a_broker_holding_less_than_the_row_is_refused(self, conn):
        """Oversell protection: never retry more than the account holds."""
        _exit_submitted(conn, qty=10)
        out = exit_runtime.recover_dead_exits(
            conn, broker=_Broker([_P("FLS", 4)]),
            fills_for=lambda row: _fill(), now=NOW)
        assert [o["status"] for o in out] == [
            exit_runtime.DEAD_SELL_QUANTITY_MISMATCH]
        assert position_store.load_by_symbol(
            conn, "FLS")["status"] == position_store.EXIT_SUBMITTED

    def test_a_broker_holding_none_is_left_to_the_retirement_path(self, conn):
        _exit_submitted(conn)
        out = exit_runtime.recover_dead_exits(
            conn, broker=_Broker([]), fills_for=lambda row: _fill(), now=NOW)
        assert [o["status"] for o in out] == [
            exit_runtime.DEAD_SELL_QUANTITY_MISMATCH]


class TestPartialFillSafety:
    """requested 10, filled 4, terminal, broker holds 6."""

    def test_sync_reduces_the_row_to_the_unsold_remainder(self, conn):
        _exit_submitted(conn, qty=10)
        out = exit_runtime.sync_sell_fills(
            conn, fills_for=lambda row: _fill(filled=4, avg=78.0), now=NOW)
        assert out[0]["status"] == "PARTIALLY_SOLD"
        assert out[0]["remaining"] == 6
        assert position_store.load_by_symbol(conn, "FLS")["quantity"] == 6

    def test_only_the_remainder_becomes_retryable(self, conn):
        _exit_submitted(conn, qty=10)
        exit_runtime.sync_sell_fills(
            conn, fills_for=lambda row: _fill(filled=4, avg=78.0), now=NOW)
        out = exit_runtime.recover_dead_exits(
            conn, broker=_Broker([_P("FLS", 6)]),
            fills_for=lambda row: _fill(filled=4, avg=78.0), now=NOW)
        assert out[0]["status"] == exit_runtime.DEAD_SELL_RELEASED
        assert out[0]["retryable_quantity"] == 6
        assert out[0]["previously_filled"] == 4

        row = position_store.load_by_symbol(conn, "FLS")
        assert row["quantity"] == 6, "the 4 already sold are never resold"
        assert row["status"] == position_store.EXIT_PENDING

    def test_the_already_filled_shares_are_not_closed_twice(self, conn):
        _exit_submitted(conn, qty=10)
        exit_runtime.sync_sell_fills(
            conn, fills_for=lambda row: _fill(filled=4, avg=78.0), now=NOW)
        exit_runtime.recover_dead_exits(
            conn, broker=_Broker([_P("FLS", 6)]),
            fills_for=lambda row: _fill(filled=4, avg=78.0), now=NOW)
        rows = conn.execute(
            "SELECT COUNT(*) FROM s6_positions WHERE symbol='FLS'").fetchone()[0]
        assert rows == 1, "recovery must not fork the position"


class TestIdempotence:
    def test_running_recovery_twice_releases_once(self, conn):
        _exit_submitted(conn)
        first = exit_runtime.recover_dead_exits(
            conn, broker=_Broker([_P("FLS", 1)]),
            fills_for=lambda row: _fill(), now=NOW)
        second = exit_runtime.recover_dead_exits(
            conn, broker=_Broker([_P("FLS", 1)]),
            fills_for=lambda row: _fill(), now=NOW)
        assert first[0]["status"] == exit_runtime.DEAD_SELL_RELEASED
        assert second == [], "the row is EXIT_PENDING now, not EXIT_SUBMITTED"

    def test_release_is_guarded_against_a_second_application(self, conn):
        pid = _exit_submitted(conn)
        assert position_store.release_dead_exit(conn, pid, now=NOW) is True
        assert position_store.release_dead_exit(conn, pid, now=NOW) is False


class TestSellPublicationGrace:
    """The HBAN half: absence must not be read as resolution."""

    def test_an_unresolved_sell_is_not_retired(self, conn):
        _exit_submitted(conn)
        out = exit_runtime.reconcile_unconfirmed_exits(
            conn, broker=_Broker([]), positions=[], open_orders=[],
            fills_for=lambda row: None, now=NOW)
        assert [o["status"] for o in out] == ["SELL_FILL_UNRESOLVED"]
        row = position_store.load_by_symbol(conn, "FLS")
        assert row is not None, "HBAN was retired in exactly this state"
        assert row["status"] == position_store.EXIT_SUBMITTED

    def test_a_non_terminal_sell_is_not_retired(self, conn):
        _exit_submitted(conn)
        out = exit_runtime.reconcile_unconfirmed_exits(
            conn, broker=_Broker([]), positions=[], open_orders=[],
            fills_for=lambda row: _fill(terminal=False), now=NOW)
        assert [o["status"] for o in out] == ["SELL_FILL_UNRESOLVED"]

    def test_a_late_fill_still_closes_with_real_economics(self, conn):
        """The fill that HBAN's retirement threw away."""
        _exit_submitted(conn)
        exit_runtime.reconcile_unconfirmed_exits(
            conn, broker=_Broker([]), positions=[], open_orders=[],
            fills_for=lambda row: None, now=NOW)
        out = exit_runtime.sync_sell_fills(
            conn, fills_for=lambda row: _fill(filled=1, avg=78.42), now=NOW)
        assert out[0]["status"] == "CLOSED"
        row = conn.execute(
            "SELECT exit_price, exit_reason FROM s6_positions "
            "WHERE symbol='FLS'").fetchone()
        assert row["exit_price"] == pytest.approx(78.42)
        assert row["exit_reason"] == "RANGE_REENTRY"

    def test_a_genuinely_terminal_unfilled_sell_still_retires(self, conn):
        """The MTCH case must keep working: broker flat, order finished."""
        _exit_submitted(conn)
        out = exit_runtime.reconcile_unconfirmed_exits(
            conn, broker=_Broker([]), positions=[], open_orders=[],
            fills_for=lambda row: _fill(filled=0, terminal=True), now=NOW)
        assert [o["status"] for o in out] == [
            exit_runtime.EXTERNALLY_CLOSED_SELL_UNCONFIRMED]
        row = conn.execute(
            "SELECT status, exit_price FROM s6_positions "
            "WHERE symbol='FLS'").fetchone()
        assert row["status"] == position_store.CLOSED
        assert row["exit_price"] is None, "no PnL may be invented"


class TestTheInquiryGraceIsShared:
    def test_buy_and_sell_use_the_same_publication_window(self):
        assert fq.NO_FILL_CONFIRMATION_GRACE_SECONDS > 0
        broker = type("B", (), {
            "get_fills": lambda self, **k: [],
            "get_open_orders": lambda self: []})()
        early = fq.inquire(broker, broker_order_id="X", side="sell",
                           ordered_quantity=1, now=SUBMITTED_AT + timedelta(seconds=60),
                           since=SUBMITTED_AT)
        late = fq.inquire(
            broker, broker_order_id="X", side="sell", ordered_quantity=1,
            now=SUBMITTED_AT + timedelta(
                seconds=fq.NO_FILL_CONFIRMATION_GRACE_SECONDS + 60),
            since=SUBMITTED_AT)
        assert early.terminal is False, "inside the window: not final"
        assert late.terminal is True, "past it: a dead order must be usable"


class _Adapter:
    def __init__(self, status=200):
        self.calls, self._status = [], status

    def submit_order(self, symbol, quantity, *, side, client_order_id=None,
                     **kwargs):
        self.calls.append({"symbol": symbol, "quantity": quantity,
                           "side": side, "client_order_id": client_order_id})
        return type("R", (), {"status_code": self._status, "text": "ok"})()


class TestTheRetryThatFollows:
    """Recovery hands the row to the EXISTING retry path. One SELL only."""

    def test_recovery_then_retry_submits_exactly_one_sell(self, conn):
        _exit_submitted(conn)
        exit_runtime.recover_dead_exits(
            conn, broker=_Broker([_P("FLS", 1)]),
            fills_for=lambda row: _fill(), now=NOW)

        adapter = _Adapter()
        out = exit_runtime.retry_latched_exits(
            conn, broker_adapter=adapter, now=NOW, orders_allowed=True)
        assert len(adapter.calls) == 1, "exactly one new SELL"
        assert adapter.calls[0]["side"] == "sell"
        assert adapter.calls[0]["quantity"] == 1
        assert out and out[0]["action"] == exit_runtime.ACTION_SOLD

    def test_a_second_retry_tick_does_not_send_another(self, conn):
        _exit_submitted(conn)
        exit_runtime.recover_dead_exits(
            conn, broker=_Broker([_P("FLS", 1)]),
            fills_for=lambda row: _fill(), now=NOW)
        adapter = _Adapter()
        exit_runtime.retry_latched_exits(
            conn, broker_adapter=adapter, now=NOW, orders_allowed=True)
        exit_runtime.retry_latched_exits(
            conn, broker_adapter=adapter, now=NOW, orders_allowed=True)
        assert len(adapter.calls) == 1, (
            "the re-latched row is EXIT_SUBMITTED again -- one SELL per life")

    def test_the_retry_sells_only_the_partial_remainder(self, conn):
        _exit_submitted(conn, qty=10)
        exit_runtime.sync_sell_fills(
            conn, fills_for=lambda row: _fill(filled=4, avg=78.0), now=NOW)
        exit_runtime.recover_dead_exits(
            conn, broker=_Broker([_P("FLS", 6)]),
            fills_for=lambda row: _fill(filled=4, avg=78.0), now=NOW)
        adapter = _Adapter()
        exit_runtime.retry_latched_exits(
            conn, broker_adapter=adapter, now=NOW, orders_allowed=True)
        assert len(adapter.calls) == 1
        assert adapter.calls[0]["quantity"] == 6, (
            "never resell the 4 that already filled")

    def test_no_retry_while_orders_are_not_allowed(self, conn):
        _exit_submitted(conn)
        exit_runtime.recover_dead_exits(
            conn, broker=_Broker([_P("FLS", 1)]),
            fills_for=lambda row: _fill(), now=NOW)
        adapter = _Adapter()
        out = exit_runtime.retry_latched_exits(
            conn, broker_adapter=adapter, now=NOW, orders_allowed=False)
        assert out == [] and adapter.calls == []
