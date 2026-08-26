"""One broker position, one owning strategy.

S6 placed a real BUY for DT (KIS order 0030740200) and recorded it in
`s6_positions`. Eight minutes later S1's `sync_fills` saw a DT position at
the broker, found nothing claiming it in S1's OWN book, and adopted it.
One share, two owners, two exit engines each believing they had to sell
it.

Nothing crashed and both books were internally consistent -- which is the
point. The only thing preventing a double SELL was an unrelated
reconciliation mismatch that happened to be blocking both engines, and
"fix" that mismatch by summing the two claims into one holding and the
totals agree with the broker while the double ownership becomes
invisible.
"""

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from reconciliation import ownership  # noqa: E402
from s1_live import position_store as s1ps  # noqa: E402
from s6_live import position_store as s6ps  # noqa: E402

NOW = datetime(2026, 8, 26, 16, 0, tzinfo=timezone.utc)
S1 = "S1_HMA_EARLY_TREND_V1"
S6 = "S6_ORB_BREAKOUT_V1"


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def _s6_open(conn, symbol="DT", quantity=1, price=50.79):
    pid = s6ps.record_submission(conn, symbol=symbol, variant="S6-R",
                                 entry_session="REGULAR",
                                 client_order_id=f"kislive-{symbol}-1", now=NOW)
    s6ps.open_from_fill(conn, pid, quantity=quantity, average_fill_price=price,
                        venue="NYSE", now=NOW)
    return pid


def _ledger(conn, symbol, strategy_id, *, status="ACCEPTED",
            internal_order_id=None):
    conn.execute(
        "INSERT INTO kis_order_idempotency (internal_order_id, signal_id, "
        "symbol, side, trading_date, broker_order_id, status, created_at, "
        "updated_at, requested_quantity, version, strategy_id) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?)",
        (internal_order_id or f"kislive-{symbol}-1", "sig", symbol, "buy",
         "2026-08-26", "0030740200", status, NOW.isoformat(), NOW.isoformat(),
         1.0, 1, strategy_id))
    conn.commit()


class TestOwnershipComesFromProvenanceFirst:
    def test_the_ledger_names_the_strategy_that_bought_it(self, conn):
        _ledger(conn, "DT", S6)
        assert ownership.claimant_from_ledger(conn, "DT") == S6

    def test_a_rejected_order_never_produced_shares(self, conn):
        """A rejected order cannot be the provenance of a holding."""
        _ledger(conn, "DT", S6, status="REJECTED")
        assert ownership.claimant_from_ledger(conn, "DT") is None

    def test_a_cancelled_order_never_produced_shares(self, conn):
        _ledger(conn, "DT", S6, status="CANCELLED")
        assert ownership.claimant_from_ledger(conn, "DT") is None

    def test_an_unknown_symbol_has_no_claimant(self, conn):
        assert ownership.claimant_from_ledger(conn, "NOPE") is None
        assert ownership.claimant_from_ledger(conn, "") is None


class TestS1MayNotAdoptAnotherStrategysFill:
    def test_the_exact_DT_case(self, conn):
        """The one that happened. S6 owns DT by both provenance and its
        position book; S1 must refuse it."""
        _ledger(conn, "DT", S6)
        _s6_open(conn, "DT")
        permitted, why = ownership.may_adopt(conn, "DT", strategy_id=S1)
        assert permitted is False
        assert "S6" in why

    def test_the_ledger_alone_is_enough_to_refuse(self, conn):
        """Even before the other strategy's book has a row -- the fill
        descends from an order, and the order was signed."""
        _ledger(conn, "DT", S6)
        permitted, why = ownership.may_adopt(conn, "DT", strategy_id=S1)
        assert permitted is False
        assert "ledger" in why

    def test_the_position_book_alone_is_enough_to_refuse(self, conn):
        """The fallback, for a holding whose order has aged out."""
        _s6_open(conn, "DT")
        permitted, why = ownership.may_adopt(conn, "DT", strategy_id=S1)
        assert permitted is False
        assert "claimed by" in why

    def test_a_strategy_may_adopt_its_own(self, conn):
        _ledger(conn, "DT", S1)
        permitted, _why = ownership.may_adopt(conn, "DT", strategy_id=S1)
        assert permitted is True

    def test_a_genuinely_unclaimed_symbol_may_be_adopted(self, conn):
        permitted, why = ownership.may_adopt(conn, "TX", strategy_id=S1)
        assert permitted is True
        assert why == "unclaimed"

    def test_the_guard_is_wired_into_sync_fills(self):
        """It has to be at the adoption point, not merely available."""
        import inspect

        from s1_live import executor

        source = inspect.getsource(executor.sync_fills)
        assert "ownership.may_adopt" in source
        assert "will not adopt" in source


class TestAConflictIsNotDeduped:
    def test_two_claims_on_one_symbol_are_reported(self, conn):
        _s6_open(conn, "DT")
        s1ps.open_position(conn, symbol="DT", strategy_id=S1,
                           signal_id="s1-fill-DT", entry_price=50.79,
                           quantity=1, now=NOW)
        assert ownership.conflicts(conn) == [("DT", sorted([S1, S6]))]

    def test_a_conflict_fails_the_position_comparison(self, conn):
        """Even though the totals would agree with the broker -- summing
        the two claims gives 1, the broker holds 1, and the disagreement
        that permits a double SELL disappears."""
        from reconciliation import snapshot

        _s6_open(conn, "DT")
        s1ps.open_position(conn, symbol="DT", strategy_id=S1,
                           signal_id="s1-fill-DT", entry_price=50.79,
                           quantity=1, now=NOW)

        held = snapshot.load_internal_positions(now=NOW, conn=conn)
        assert [(p.symbol, p.quantity) for p in held] == [("DT", 1)]

        detail = snapshot.ownership_conflicts(conn)
        assert detail and ownership.OWNERSHIP_CONFLICT in detail[0]

    def test_one_owner_is_not_a_conflict(self, conn):
        _s6_open(conn, "DT")
        assert ownership.conflicts(conn) == []
        from reconciliation import snapshot

        assert snapshot.ownership_conflicts(conn) == []

    def test_a_symbol_in_one_strategys_two_books_is_not_a_conflict(self, conn):
        """S1 writes to the general store AND its own. That is one
        strategy in two books, not two strategies -- deduplication is
        correct there and only there."""
        s1ps.open_position(conn, symbol="TX", strategy_id=S1,
                           signal_id="s1-TX", entry_price=53.68, quantity=1,
                           now=NOW)
        assert ownership.conflicts(conn) == []


class TestAmbiguityFailsClosed:
    def test_an_unreadable_ledger_does_not_permit_adoption(self, conn, monkeypatch):
        class _Boom:
            def execute(self, *a, **k):
                raise RuntimeError("db gone")

        assert ownership.claimant_from_ledger(_Boom(), "DT") is None

    def test_unreadable_books_report_no_conflicts_rather_than_crashing(
            self, conn, monkeypatch):
        """Losing the diagnostic must not manufacture a blocking
        disagreement -- but it must not hide one either, which is why the
        adoption guard refuses separately."""
        monkeypatch.setattr(
            "reconciliation.internal_holdings.strategy_holdings",
            lambda _c: (_ for _ in ()).throw(RuntimeError("unreadable")))
        assert ownership.claims_by_symbol(conn) == {}

    def test_an_empty_symbol_is_refused(self, conn):
        permitted, _why = ownership.may_adopt(conn, "", strategy_id=S1)
        assert permitted is False


class TestReleasingAMisattributedRow:
    """Repair, not deletion. §14 forbids a manual overwrite, so the row
    goes through the store's own transition and leaves a trail."""

    def test_it_releases_the_row_that_does_not_own_the_symbol(self, conn):
        _ledger(conn, "DT", S6)
        _s6_open(conn, "DT")
        s1ps.open_position(conn, symbol="DT", strategy_id=S1,
                           signal_id="s1-fill-DT", entry_price=50.79,
                           quantity=1, now=NOW)
        assert ownership.conflicts(conn)

        out = ownership.release_misattributed(
            conn, symbol="DT", strategy_id=S1, now=NOW, audit=False)
        assert out["released"] is True
        assert out["owner"] == S6
        # The conflict is gone and S6 still owns the position.
        assert ownership.conflicts(conn) == []
        assert [s for s, _v, _q in s6ps.holdings(conn)] == ["DT"]
        assert s1ps.holdings(conn) == []

    def test_the_released_row_is_not_recorded_as_a_trade(self, conn):
        """A normal CLOSED would put a trade in S1's realized record that
        it never made, and the entry price would make it a scratch."""
        _ledger(conn, "DT", S6)
        _s6_open(conn, "DT")
        s1ps.open_position(conn, symbol="DT", strategy_id=S1,
                           signal_id="s1-fill-DT", entry_price=50.79,
                           quantity=1, now=NOW)
        ownership.release_misattributed(conn, symbol="DT", strategy_id=S1,
                                        now=NOW, audit=False)
        row = conn.execute(
            "SELECT status, exit_reason FROM s1_positions WHERE symbol='DT'"
        ).fetchone()
        assert row["status"] == "CLOSED"
        assert row["exit_reason"] == ownership.RELEASED_WRONGLY_ATTRIBUTED

    def test_it_refuses_when_ownership_is_not_established_elsewhere(self, conn):
        """It must not be usable to take a position from its real owner."""
        s1ps.open_position(conn, symbol="TX", strategy_id=S1,
                           signal_id="s1-TX", entry_price=53.68, quantity=1,
                           now=NOW)
        out = ownership.release_misattributed(
            conn, symbol="TX", strategy_id=S1, now=NOW, audit=False)
        assert out["released"] is False
        assert "could not be established" in out["reason"]
        assert [s for s, _v, _q in s1ps.holdings(conn)] == ["TX"]

    def test_it_refuses_to_take_a_symbol_from_its_ledger_owner(self, conn):
        _ledger(conn, "TX", S1)
        s1ps.open_position(conn, symbol="TX", strategy_id=S1,
                           signal_id="s1-TX", entry_price=53.68, quantity=1,
                           now=NOW)
        out = ownership.release_misattributed(
            conn, symbol="TX", strategy_id=S1, now=NOW, audit=False)
        assert out["released"] is False

    def test_it_refuses_a_row_with_an_exit_in_flight(self, conn):
        """Retiring a row being acted on is how an exit gets orphaned."""
        _ledger(conn, "DT", S6)
        _s6_open(conn, "DT")
        pid = s1ps.open_position(conn, symbol="DT", strategy_id=S1,
                                 signal_id="s1-fill-DT", entry_price=50.79,
                                 quantity=1, now=NOW)
        s1ps.mark_exit_submitted(conn, pid, "STOP", now=NOW)
        out = ownership.release_misattributed(
            conn, symbol="DT", strategy_id=S1, now=NOW, audit=False)
        assert out["released"] is False
        assert "exit in flight" in out["reason"]

    def test_TX_is_never_touched_by_a_DT_release(self, conn):
        """The blast radius has to be one symbol."""
        _ledger(conn, "DT", S6)
        _s6_open(conn, "DT")
        s1ps.open_position(conn, symbol="DT", strategy_id=S1,
                           signal_id="s1-fill-DT", entry_price=50.79,
                           quantity=1, now=NOW)
        s1ps.open_position(conn, symbol="TX", strategy_id=S1,
                           signal_id="s1-TX", entry_price=53.68, quantity=1,
                           now=NOW)
        ownership.release_misattributed(conn, symbol="DT", strategy_id=S1,
                                        now=NOW, audit=False)
        assert [s for s, _v, _q in s1ps.holdings(conn)] == ["TX"]
