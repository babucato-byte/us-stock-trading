"""An S6 BUY does not rest indefinitely, and never cancels on a guess.

The first NORMAL_S6 order rested ACCEPTED and unfilled for thirty
minutes. Nothing in the system was willing to decide anything about it:
the fill sync reported STILL_UNCONFIRMED on every tick, forever, because
"no fill yet" and "this will never fill" look identical to it. Meanwhile
it held S6's only slot and went on representing a breakout the scanner
may already have stopped emitting.

Two failure directions matter here and they pull opposite ways. Leaving a
dead order resting is the one that prompted this. Cancelling a LIVE order
-- one that filled while we were deciding, or one whose candidate we
merely could not read -- is the more expensive one, because it can cancel
a position out from under itself or throw away a good entry on ignorance.
So most of what follows is about refusing to act on "do not know".
"""

import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s6_live import entry_timeout as et  # noqa: E402
from s6_live import position_store as ps  # noqa: E402

NOW = datetime(2026, 8, 26, 14, 30, tzinfo=timezone.utc)
ACCOUNT = "12345678"


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


class Broker:
    """Enough of KISBroker for these decisions: an open-order book."""

    def __init__(self, open_symbols=("DT",), raises=False):
        self._open = list(open_symbols)
        self._raises = raises
        self.config = type("C", (), {"account_no": ACCOUNT})()

    def get_open_orders(self):
        if self._raises:
            raise RuntimeError("KIS unreachable")
        # Shaped like the real KIS open-order row: the resting price is
        # where the cancel's intent gets its limit from, because an
        # unfilled position row has no entry price of its own.
        return [{"pdno": s, "odno": "0030740200", "ft_ord_qty": "1",
                 "ft_ccld_qty": "0", "nccs_qty": "1",
                 "ft_ord_unpr3": "50.79000000"} for s in self._open]


class Source:
    def __init__(self, symbols=("DT",), refusal=None, running=False,
                 detectable=True):
        self._symbols = list(symbols)
        self._refusal = refusal
        self._state = {"running": running, "detectable": detectable}

    def symbols(self):
        return list(self._symbols)

    def describe(self):
        return {"refusal": self._refusal, "scan_state": self._state}


def _submitted(conn, *, symbol="DT", age_seconds=0, client_order_id="kislive-DT-1",
               quantity=1.0):
    submitted_at = NOW - timedelta(seconds=age_seconds)
    pid = ps.record_submission(
        conn, symbol=symbol, variant="S6-R", entry_session="REGULAR",
        client_order_id=client_order_id, now=submitted_at)
    conn.execute(
        "INSERT INTO kis_order_idempotency (internal_order_id, signal_id, "
        "symbol, side, trading_date, broker_order_id, status, created_at, "
        "updated_at, requested_quantity, version, strategy_id) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?)",
        (client_order_id, "sig-1", symbol, "buy", "2026-08-26", "0030740200",
         "ACCEPTED", submitted_at.isoformat(), submitted_at.isoformat(),
         quantity, 1, ps.STRATEGY_ID))
    conn.commit()
    return pid


class TestTheClockDecidesOnlyWhenItHasRunOut:
    def test_an_order_inside_the_ttl_is_held(self, conn):
        _submitted(conn, age_seconds=120)
        out = et.evaluate(conn, broker=Broker(), account_id=ACCOUNT,
                          source=Source(), now=NOW)
        assert [o["action"] for o in out] == [et.ACTION_HELD]
        assert out[0]["age_seconds"] == pytest.approx(120, abs=2)

    def test_an_order_past_the_ttl_is_cancelled(self, conn, monkeypatch):
        pid = _submitted(conn, age_seconds=300)
        sent = []
        _stub_engine(monkeypatch, sent)
        out = et.evaluate(conn, broker=Broker(), account_id=ACCOUNT,
                          source=Source(), now=NOW)
        assert out[0]["action"] == et.ACTION_CANCEL_REQUESTED
        assert out[0]["reason"] == et.REASON_TTL
        assert len(sent) == 1
        assert ps.load(conn, pid)["status"] != ps.SUBMITTED

    def test_the_ttl_is_three_minutes(self):
        """A trading decision, not a technical timeout: S6's edge decays
        with the move it is entering."""
        assert et.BUY_FILL_TTL_SECONDS == 180

    def test_exactly_at_the_ttl_it_cancels(self, conn, monkeypatch):
        _submitted(conn, age_seconds=180)
        _stub_engine(monkeypatch, [])
        out = et.evaluate(conn, broker=Broker(), account_id=ACCOUNT,
                          source=Source(), now=NOW)
        assert out[0]["action"] == et.ACTION_CANCEL_REQUESTED

    def test_an_unreadable_timestamp_does_not_time_out(self, conn, monkeypatch):
        """A stamp that cannot be parsed is not an old order."""
        _submitted(conn, age_seconds=300)
        conn.execute("UPDATE s6_positions SET submitted_at='not-a-time', "
                     "created_at='not-a-time'")
        conn.commit()
        out = et.evaluate(conn, broker=Broker(), account_id=ACCOUNT,
                          source=Source(), now=NOW)
        assert out[0]["action"] == et.ACTION_HELD


class TestItRefusesToActOnIgnorance:
    """The expensive direction. Every one of these could cancel a good
    order on information the system does not actually have."""

    def test_a_scan_in_progress_never_invalidates(self, conn):
        """"The candidate list is empty because a scan is running" is
        ignorance, not invalidation."""
        _submitted(conn, age_seconds=10)
        source = Source(symbols=(), running=True)
        assert et.candidate_still_valid(source, "DT") is None
        out = et.evaluate(conn, broker=Broker(), account_id=ACCOUNT,
                          source=source, now=NOW)
        assert out[0]["action"] == et.ACTION_HELD

    def test_a_refusing_source_never_invalidates(self, conn):
        source = Source(symbols=(), refusal="no S6 scan ran for this session")
        assert et.candidate_still_valid(source, "DT") is None
        _submitted(conn, age_seconds=10)
        out = et.evaluate(conn, broker=Broker(), account_id=ACCOUNT,
                          source=source, now=NOW)
        assert out[0]["action"] == et.ACTION_HELD

    def test_no_source_at_all_never_invalidates(self, conn):
        assert et.candidate_still_valid(None, "DT") is None

    def test_an_undetectable_scan_state_never_invalidates(self, conn):
        assert et.candidate_still_valid(
            Source(symbols=(), detectable=False), "DT") is None

    def test_an_unreadable_open_order_book_does_not_cancel(self, conn, monkeypatch):
        """Unreadable is not "open". Cancelling on a failed read is how a
        filled order gets cancelled out from under its own position."""
        _submitted(conn, age_seconds=300)
        sent = []
        _stub_engine(monkeypatch, sent)
        out = et.evaluate(conn, broker=Broker(raises=True), account_id=ACCOUNT,
                          source=Source(), now=NOW)
        assert out[0]["action"] == et.ACTION_SKIPPED
        assert sent == []

    def test_an_order_no_longer_open_is_not_cancelled(self, conn, monkeypatch):
        """It filled while we were deciding. The race this closes."""
        _submitted(conn, age_seconds=300)
        sent = []
        _stub_engine(monkeypatch, sent)
        out = et.evaluate(conn, broker=Broker(open_symbols=()),
                          account_id=ACCOUNT, source=Source(), now=NOW)
        assert out[0]["action"] == et.ACTION_SKIPPED
        assert "no longer open" in out[0]["detail"]
        assert sent == []


class TestPositiveInvalidationCancelsEarly:
    def test_a_candidate_that_dropped_out_cancels_before_the_ttl(
            self, conn, monkeypatch):
        _submitted(conn, age_seconds=10)
        _stub_engine(monkeypatch, [])
        out = et.evaluate(conn, broker=Broker(), account_id=ACCOUNT,
                          source=Source(symbols=("WMB", "CTVA")), now=NOW)
        assert out[0]["action"] == et.ACTION_CANCEL_REQUESTED
        assert out[0]["reason"] == et.REASON_CANDIDATE_GONE

    def test_a_session_that_can_no_longer_order_cancels(self, conn, monkeypatch):
        _submitted(conn, age_seconds=10)
        _stub_engine(monkeypatch, [])
        out = et.evaluate(conn, broker=Broker(), account_id=ACCOUNT,
                          source=Source(), now=NOW, session_orderable=False)
        assert out[0]["action"] == et.ACTION_CANCEL_REQUESTED
        assert out[0]["reason"] == et.REASON_SESSION_NOT_ORDERABLE


class TestTheCancelIsSentOnceAndNeverChases:
    def test_an_ambiguous_cancel_is_not_retried(self, conn, monkeypatch):
        _submitted(conn, age_seconds=300)
        calls = []

        def _boom(**kwargs):
            calls.append(kwargs)
            raise RuntimeError("cancel response timed out")

        monkeypatch.setattr("execution.execution_engine.submit_cancel", _boom)
        out = et.evaluate(conn, broker=Broker(), account_id=ACCOUNT,
                          source=Source(), now=NOW)
        assert out[0]["action"] == et.ACTION_CANCEL_UNKNOWN
        assert len(calls) == 1, "a cancel must be sent exactly once"

    def test_an_unknown_cancel_leaves_the_row_unconfirmed(self, conn, monkeypatch):
        """Fail closed: the slot is not released until reconciliation
        settles what the broker actually holds."""
        pid = _submitted(conn, age_seconds=300)

        def _boom(**kwargs):
            raise RuntimeError("timeout")

        monkeypatch.setattr("execution.execution_engine.submit_cancel", _boom)
        et.evaluate(conn, broker=Broker(), account_id=ACCOUNT,
                    source=Source(), now=NOW)
        assert ps.load(conn, pid)["status"] == ps.SUBMITTED

    def test_nothing_here_adjusts_a_price(self):
        """A price that did not fill is information about the candidate,
        not a number to raise until it works."""
        import inspect

        # The executable lines, not the prose. The module docstring says
        # out loud that it never makes an order marketable, so scanning
        # the whole file would match its own explanation.
        source = inspect.getsource(et)
        body = "\n".join(line for line in source.splitlines()
                         if not line.lstrip().startswith("#"))
        for forbidden in ("limit_price +", "limit_price *", "ask +",
                          "* 1.0", "marketable="):
            assert forbidden not in body, forbidden


class TestTheCancelAddressesTheOrdersOwnSession:
    def test_the_intent_carries_the_entry_session(self, conn):
        """An order placed through the daytime family lives on the
        daytime endpoint and can only be cancelled there. Resolving the
        route from the current clock would address a daytime order's
        cancel to the regular endpoint."""
        ps.record_submission(conn, symbol="SLF", variant="S6-O",
                             entry_session="OVERNIGHT_DAYTIME",
                             client_order_id="kislive-SLF-1", now=NOW)
        conn.execute(
            "INSERT INTO kis_order_idempotency (internal_order_id, signal_id, "
            "symbol, side, trading_date, broker_order_id, status, created_at, "
            "updated_at, requested_quantity, version, strategy_id) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("kislive-SLF-1", "sig", "SLF", "buy", "2026-08-26", "1", "ACCEPTED",
             NOW.isoformat(), NOW.isoformat(), 1.0, 1, ps.STRATEGY_ID))
        conn.commit()
        row = [r for r in ps.load_unconfirmed(conn) if r["symbol"] == "SLF"][0]
        intent, _instrument, _oid = et._reconstruct_intent(
            conn, row, open_order={"ft_ord_unpr3": "79.53000000"})
        assert intent.session == "OVERNIGHT_DAYTIME"

        from brokers.kis_broker import cancel_route_for

        assert cancel_route_for(intent.session, "live")[1] == "TTTS6038U"

    def test_the_intent_reuses_the_original_order_id(self, conn):
        """A cancel is not a new order attempt and must not register as
        one -- it transitions the row the ledger already tracks."""
        _submitted(conn, client_order_id="kislive-DT-abc")
        row = list(ps.load_unconfirmed(conn))[0]
        intent, _i, broker_order_id = et._reconstruct_intent(
            conn, row, open_order={"ft_ord_unpr3": "50.79000000"})
        assert intent.internal_order_id == "kislive-DT-abc"
        assert broker_order_id == "0030740200"


class TestTheNextBuyWaitsForTerminality:
    def test_an_unconfirmed_entry_blocks_the_next_buy(self, conn):
        _submitted(conn, age_seconds=10)
        blocked = et.entry_is_blocked(conn, broker=Broker(open_symbols=()))
        assert blocked and "unconfirmed" in blocked

    def test_a_live_position_blocks_the_next_buy(self, conn):
        pid = _submitted(conn, age_seconds=10)
        ps.open_from_fill(conn, pid, quantity=1, average_fill_price=50.0)
        blocked = et.entry_is_blocked(conn, broker=Broker(open_symbols=()))
        assert blocked and "already holds" in blocked

    def test_an_open_broker_order_blocks_the_next_buy(self, conn):
        """Even with the store clean: two live orders for one slot is the
        duplicate this lifecycle exists to prevent."""
        assert et.entry_is_blocked(conn, broker=Broker(open_symbols=("DT",)))

    def test_an_unreadable_book_blocks_the_next_buy(self, conn):
        blocked = et.entry_is_blocked(conn, broker=Broker(raises=True))
        assert blocked and "unreadable" in blocked

    def test_a_clean_state_permits_the_next_buy(self, conn):
        assert et.entry_is_blocked(conn, broker=Broker(open_symbols=())) is None


class TestPartialFills:
    def test_the_filled_quantity_is_kept_and_the_remainder_cancelled(
            self, conn, monkeypatch):
        """`sync_buy_fills` has already applied the partial by the time
        this runs, so the position is OPEN for what filled; what is left
        is to stop holding the slot for shares nobody intends to buy."""
        pid = _submitted(conn, age_seconds=300, quantity=3.0)
        ps.apply_fill(conn, pid, filled_quantity=1, average_fill_price=50.0)
        assert ps.load(conn, pid)["quantity"] == 1
        _stub_engine(monkeypatch, [])
        # An OPEN row is no longer "unconfirmed", so the remainder is
        # pulled by the same cancel path against the live order.
        row = dict(ps.load(conn, pid))
        out = et.cancel_unfilled(conn, broker=Broker(), row=row,
                                 reason=et.REASON_TTL, account_id=ACCOUNT,
                                 now=NOW)
        assert out["action"] == et.ACTION_CANCEL_REQUESTED
        assert ps.load(conn, pid)["quantity"] == 1, "the fill must survive"


def _stub_engine(monkeypatch, sent):
    def _submit_cancel(**kwargs):
        sent.append(kwargs)
        return None

    monkeypatch.setattr("execution.execution_engine.submit_cancel",
                        _submit_cancel)


class TestAStrategyPositionIsVisibleToReconciliation:
    """A position the reconciler cannot see blocks its own exit.

    `load_internal_positions` read only the general lifecycle store,
    which was correct while every live position was written there. It
    stopped being correct when S6 began recording into `s6_positions` and
    nowhere else -- the right call, since POSITION_TABLES maps S6 there
    and writing to both would put two exit engines on one position.

    The reconciler was never told. So a filled S6 position read as
    "exists at KIS but not tracked internally", and that mismatch blocked
    the SELL: the position could be opened and then not closed. Invisible
    is worse than unattributed -- unattributed costs capacity, invisible
    costs the ability to leave.
    """

    def test_an_s6_position_is_counted_as_internally_held(self, conn):
        from reconciliation import snapshot

        pid = _submitted(conn)
        ps.open_from_fill(conn, pid, quantity=1, average_fill_price=50.79,
                          venue="NYSE")
        held = snapshot.load_internal_positions(now=NOW, conn=conn)
        assert [(p.symbol, p.quantity) for p in held] == [("DT", 1)]

    def test_an_exiting_position_is_still_counted(self, conn):
        """EXIT_PENDING still holds the shares -- that is exactly when
        the comparison matters, because a blocked exit is the failure."""
        from reconciliation import snapshot

        pid = _submitted(conn)
        ps.open_from_fill(conn, pid, quantity=1, average_fill_price=50.79)
        ps.latch_pending_exit(conn, pid, "RANGE_REENTRY", now=NOW)
        assert ps.load(conn, pid)["status"] == ps.EXIT_PENDING
        held = snapshot.load_internal_positions(now=NOW, conn=conn)
        assert [p.symbol for p in held] == ["DT"]

    def test_an_unfilled_submission_is_not_counted_as_held(self, conn):
        """The other direction. An unfilled order is a yes to "could
        another position appear" and a no to "what do we hold";
        reconciliation asks the second, and counting a submission would
        report shares the account does not have."""
        from reconciliation import snapshot

        _submitted(conn)
        assert snapshot.load_internal_positions(now=NOW, conn=conn) == []

    def test_without_a_connection_it_falls_back_rather_than_guessing(self, conn):
        from reconciliation import snapshot

        pid = _submitted(conn)
        ps.open_from_fill(conn, pid, quantity=1, average_fill_price=50.79)
        assert snapshot.load_internal_positions(now=NOW) == []

    def test_a_symbol_in_both_books_is_not_double_counted(self, conn, monkeypatch):
        """S1 writes to the general store AND its own. Counting it twice
        would invent shares the account does not hold -- a mismatch in
        the opposite direction."""
        from reconciliation import snapshot

        pid = _submitted(conn)
        ps.open_from_fill(conn, pid, quantity=1, average_fill_price=50.79)

        class _Record(dict):
            pass

        monkeypatch.setattr(
            "positions.store.load_non_terminal",
            lambda: {"p1": {"symbol": "DT", "remaining_qty": 1,
                            "average_fill_price": 50.79}})
        held = snapshot.load_internal_positions(now=NOW, conn=conn)
        assert [(p.symbol, p.quantity) for p in held] == [("DT", 1)]
