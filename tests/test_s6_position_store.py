"""S6 positions: SUBMITTED is a state, not a gap.

S1 and S2 record a position only once a fill arrives, so a BUY that was
sent and never confirmed exists nowhere -- reconciliation sees a broker
position it cannot attribute and the strategy sees nothing at all. That
is the recoverable-versus-lost distinction this store exists for, and
most of these tests are about the ambiguous and restart cases rather
than the happy path.
"""

import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s6_live import position_store as ps  # noqa: E402

T0 = datetime(2026, 8, 21, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def submitted(conn, **kw):
    kw.setdefault("symbol", "ABC")
    kw.setdefault("variant", "S6-R")
    kw.setdefault("entry_session", "REGULAR")
    kw.setdefault("range_high", 99.5)
    kw.setdefault("range_low", 99.0)
    kw.setdefault("entry_volume_expansion", 2.0)
    kw.setdefault("now", T0)
    return ps.record_submission(conn, **kw)


class TestSubmissionIsRecordedBeforeTheFill:
    def test_a_sent_order_leaves_a_row(self):
        pass  # covered below with a connection

    def test_the_row_holds_no_shares_yet(self, conn):
        pid = submitted(conn)
        row = ps.load(conn, pid)
        assert row["status"] == ps.SUBMITTED
        assert row["entry_price"] is None
        assert row["quantity"] is None

    def test_the_breakout_context_is_stored_at_submission(self, conn):
        pid = submitted(conn, entry_vwap=100.1, entry_ema9=100.2,
                        entry_ema21=100.0, range_minutes=15)
        row = ps.load(conn, pid)
        assert row["variant"] == "S6-R"
        assert row["entry_session"] == "REGULAR"
        assert (row["range_high"], row["range_low"]) == (99.5, 99.0)
        assert row["entry_vwap"] == 100.1
        assert row["range_minutes"] == 15

    def test_the_entry_expansion_seeds_the_peak(self, conn):
        pid = submitted(conn, entry_volume_expansion=2.0)
        assert ps.load(conn, pid)["peak_volume_expansion"] == 2.0

    def test_an_unfilled_order_is_not_a_holding(self, conn):
        submitted(conn)
        assert ps.holdings(conn) == []

    def test_but_it_does_count_against_the_limit(self, conn):
        """An order in flight may become a position at any moment;
        excluding it would let a second entry through in exactly the
        window where the first is unconfirmed."""
        submitted(conn)
        assert ps.open_count(conn) == 1

    def test_a_restart_can_find_it(self, conn):
        pid = submitted(conn)
        assert [r["position_id"] for r in ps.load_unconfirmed(conn)] == [pid]

    def test_a_second_buy_into_the_same_name_is_refused(self, conn):
        """The duplicate an ambiguous submission invites, refused at the
        storage layer rather than by a gate that may be mid-restart."""
        submitted(conn)
        with pytest.raises(sqlite3.IntegrityError):
            submitted(conn)


class TestOpenRequiresARealFill:
    def test_a_fill_promotes_the_row(self, conn):
        pid = submitted(conn)
        assert ps.open_from_fill(conn, pid, quantity=1,
                                 average_fill_price=100.85, venue="AMEX",
                                 now=T0) is True
        row = ps.load(conn, pid)
        assert row["status"] == ps.OPEN
        assert row["entry_price"] == 100.85
        assert row["venue"] == "AMEX"
        assert row["peak_price"] == 100.85

    @pytest.mark.parametrize("bad", [None, 0, -1.0, float("nan"), "price"])
    def test_an_unusable_fill_price_is_refused(self, conn, bad):
        pid = submitted(conn)
        with pytest.raises(ps.S6PositionError, match="structural stop"):
            ps.open_from_fill(conn, pid, quantity=1, average_fill_price=bad)
        assert ps.load(conn, pid)["status"] == ps.SUBMITTED

    def test_an_unusable_quantity_is_refused(self, conn):
        pid = submitted(conn)
        with pytest.raises(ps.S6PositionError):
            ps.open_from_fill(conn, pid, quantity=0,
                              average_fill_price=100.0)

    def test_the_schema_refuses_an_open_row_without_a_price(self, conn):
        """Not merely the helper -- the storage layer itself."""
        pid = submitted(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE s6_positions SET status = 'OPEN' "
                         "WHERE position_id = ?", (pid,))
            conn.commit()

    def test_only_a_submitted_row_can_be_opened(self, conn):
        pid = submitted(conn)
        ps.open_from_fill(conn, pid, quantity=1, average_fill_price=100.0,
                          now=T0)
        assert ps.open_from_fill(conn, pid, quantity=1,
                                 average_fill_price=999.0, now=T0) is False
        assert ps.load(conn, pid)["entry_price"] == 100.0


class TestFillApplication:
    def test_a_partial_fill_opens_at_what_filled(self, conn):
        """A position of one share is a real position."""
        pid = submitted(conn)
        assert ps.apply_fill(conn, pid, filled_quantity=1,
                             average_fill_price=100.0, now=T0) is True
        row = ps.load(conn, pid)
        assert row["status"] == ps.OPEN and row["quantity"] == 1

    def test_a_later_fill_raises_the_quantity(self, conn):
        pid = submitted(conn)
        ps.apply_fill(conn, pid, filled_quantity=1, average_fill_price=100.0,
                      now=T0)
        assert ps.apply_fill(conn, pid, filled_quantity=2,
                             average_fill_price=100.5, now=T0) is True
        row = ps.load(conn, pid)
        assert row["quantity"] == 2 and row["entry_price"] == 100.5

    def test_the_same_fill_applied_twice_changes_nothing(self, conn):
        """Cumulative quantity is compared, not a delta -- the failure a
        retried sync invites."""
        pid = submitted(conn)
        ps.apply_fill(conn, pid, filled_quantity=1, average_fill_price=100.0,
                      now=T0)
        assert ps.apply_fill(conn, pid, filled_quantity=1,
                             average_fill_price=100.0, now=T0) is False
        assert ps.load(conn, pid)["quantity"] == 1

    def test_a_stale_smaller_fill_is_ignored(self, conn):
        pid = submitted(conn)
        ps.apply_fill(conn, pid, filled_quantity=2, average_fill_price=100.0,
                      now=T0)
        assert ps.apply_fill(conn, pid, filled_quantity=1,
                             average_fill_price=90.0, now=T0) is False
        assert ps.load(conn, pid)["quantity"] == 2

    def test_a_fill_for_an_unknown_position_is_ignored(self, conn):
        assert ps.apply_fill(conn, "s6pos_missing", filled_quantity=1,
                             average_fill_price=100.0) is False


class TestThePeaksRatchetUp:
    def test_a_higher_price_raises_the_peak(self, conn):
        pid = submitted(conn)
        ps.open_from_fill(conn, pid, quantity=1, average_fill_price=100.0,
                          now=T0)
        ps.observe(conn, pid, price=102.0, volume_expansion=3.0, now=T0)
        row = ps.load(conn, pid)
        assert row["peak_price"] == 102.0
        assert row["peak_volume_expansion"] == 3.0

    def test_a_lower_reading_does_not_lower_the_peak(self, conn):
        """A peak that followed the market down would hold the decay
        ratio at 1.0 forever and the give-back check would never fire --
        both failing silently."""
        pid = submitted(conn)
        ps.open_from_fill(conn, pid, quantity=1, average_fill_price=100.0,
                          now=T0)
        ps.observe(conn, pid, price=102.0, volume_expansion=3.0, now=T0)
        ps.observe(conn, pid, price=99.0, volume_expansion=1.1, now=T0)
        row = ps.load(conn, pid)
        assert row["peak_price"] == 102.0
        assert row["peak_volume_expansion"] == 3.0


class TestExitLatchingAndClosing:
    def opened(self, conn):
        pid = submitted(conn)
        ps.open_from_fill(conn, pid, quantity=1, average_fill_price=100.0,
                          now=T0)
        return pid

    def test_the_first_reason_wins(self, conn):
        pid = self.opened(conn)
        assert ps.latch_pending_exit(conn, pid, "RANGE_REENTRY", now=T0)
        assert ps.latch_pending_exit(conn, pid, "SESSION_EXIT", now=T0) is False
        assert ps.load(conn, pid)["pending_exit_reason"] == "RANGE_REENTRY"

    def test_exit_submitted_is_one_way(self, conn):
        pid = self.opened(conn)
        assert ps.mark_exit_submitted(conn, pid, "RANGE_REENTRY", now=T0)
        assert ps.mark_exit_submitted(conn, pid, "SESSION_EXIT", now=T0) is False
        assert ps.load(conn, pid)["exit_submitted"] == 1

    def test_a_submitted_exit_makes_the_policy_hold(self, conn):
        from s6_live import exit_policy

        pid = self.opened(conn)
        ps.mark_exit_submitted(conn, pid, "RANGE_REENTRY", now=T0)
        state = ps.to_state(ps.load(conn, pid))
        assert exit_policy.decide(state, current_price=1.0).action == \
            exit_policy.HOLD

    def test_closing_frees_the_symbol(self, conn):
        pid = self.opened(conn)
        assert ps.close_position(conn, pid, reason="RANGE_REENTRY", now=T0)
        assert ps.load_by_symbol(conn, "ABC") is None
        assert submitted(conn)

    def test_an_unfilled_order_is_abandoned_not_closed(self, conn):
        """One ends a position; the other records there never was one."""
        pid = submitted(conn)
        assert ps.abandon_submission(conn, pid, reason="NEVER_FILLED", now=T0)
        row = ps.load(conn, pid)
        assert row["status"] == ps.CLOSED
        assert row["exit_reason"] == "NEVER_FILLED"
        assert row["entry_price"] is None


class TestWhatEachQuestionCounts:
    def test_live_positions_exclude_unfilled_orders(self, conn):
        """There is nothing to exit from an order that has not filled,
        and evaluating one would compare a stop against an entry price
        that does not exist."""
        submitted(conn, symbol="PENDING")
        pid = submitted(conn, symbol="HELD")
        ps.open_from_fill(conn, pid, quantity=1, average_fill_price=100.0,
                          now=T0)
        assert [s for _p, s in
                [(p, r["symbol"]) for p, r in ps.load_live(conn)]] == ["HELD"]

    def test_pending_exits_lead(self, conn):
        first = submitted(conn, symbol="AAA")
        ps.open_from_fill(conn, first, quantity=1, average_fill_price=100.0,
                          now=T0)
        second = submitted(conn, symbol="BBB")
        ps.open_from_fill(conn, second, quantity=1, average_fill_price=100.0,
                          now=T0)
        ps.latch_pending_exit(conn, second, "RANGE_REENTRY", now=T0)
        assert [p for p, _r in ps.load_live(conn)][0] == second

    def test_holdings_carry_the_venue(self, conn):
        pid = submitted(conn)
        ps.open_from_fill(conn, pid, quantity=1, average_fill_price=100.0,
                          venue="AMEX", now=T0)
        assert ps.holdings(conn) == [("ABC", "AMEX", 1)]

    def test_the_limit_and_reconciliation_count_differently(self, conn):
        """"Could another position appear" and "what do we hold" have
        different answers while an order is in flight."""
        submitted(conn)
        assert ps.open_count(conn) == 1
        assert ps.holdings(conn) == []


class TestTheStoreDecidesNothing:
    def test_it_imports_no_broker_or_policy(self):
        import ast

        banned = {"brokers", "kis_broker", "execution_engine", "order_gate",
                  "kis_live_trading", "position_limits", "s6_exit_v0"}
        source = (REPO_ROOT / "s6_live" / "position_store.py").read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [str(getattr(node, "module", "") or "")]
                names += [a.name for a in node.names]
                for name in names:
                    for segment in name.split("."):
                        assert segment not in banned, name
