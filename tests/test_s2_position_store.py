"""S2 position persistence: only what cannot be recomputed.

Two guarantees carry the weight.

The volume peak ratchets UP. A peak that followed volume down would hold
the decay ratio at 1.0 forever, so S2 would never exit on the condition
it is built around -- and nothing would look wrong, because a signal that
CAN never fire and one that HAS not fired produce the same empty log.

Entry price is the broker's average fill, never the intended limit. The
catastrophic cap is measured from entry, so an intended-price entry puts
the stop wrong by exactly the slippage, in the direction that loosens it.
"""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s2_live import position_store as ps  # noqa: E402

T0 = datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def opened(conn, **kw):
    kw.setdefault("symbol", "ABC")
    kw.setdefault("quantity", 1)
    kw.setdefault("average_fill_price", 100.0)
    kw.setdefault("now", T0)
    return ps.open_position(conn, **kw)


class TestEntryPriceIsTheActualFill:
    def test_a_filled_entry_is_stored(self, conn):
        pid = opened(conn, average_fill_price=53.68, venue="NASD",
                     entry_session="REGULAR")
        row = ps.load_by_symbol(conn, "ABC")
        assert row["entry_price"] == 53.68
        assert row["venue"] == "NASD"
        assert row["entry_session"] == "REGULAR"
        assert row["position_id"] == pid
        assert row["strategy_id"] == ps.STRATEGY_ID

    @pytest.mark.parametrize("bad", [None, 0, -1.0, float("nan"),
                                     float("inf"), "not a price"])
    def test_an_unusable_fill_price_is_refused(self, conn, bad):
        """Worse than no position, because it looks correct."""
        with pytest.raises(ps.S2PositionError, match="every stop is measured"):
            opened(conn, average_fill_price=bad)
        assert ps.load_by_symbol(conn, "ABC") is None

    @pytest.mark.parametrize("bad", [0, -1, None, "two"])
    def test_an_unusable_quantity_is_refused(self, conn, bad):
        with pytest.raises(ps.S2PositionError):
            opened(conn, quantity=bad)

    def test_the_stops_in_force_at_entry_are_stored(self, conn):
        """Recomputed later they would answer with today's config about
        yesterday's position, and the config is expected to change."""
        opened(conn, effective_stop=97.0, hard_stop=97.0)
        row = ps.load_by_symbol(conn, "ABC")
        assert row["effective_stop"] == 97.0
        assert row["hard_stop"] == 97.0

    def test_the_entry_multiple_seeds_the_peak(self, conn):
        """Leaving it NULL would let the first observation set a peak
        from a later, lower reading and call the position undecayed
        forever."""
        opened(conn, entry_volume_multiple=6.0)
        assert ps.load_by_symbol(conn, "ABC")["peak_volume_multiple"] == 6.0


class TestThePeakRatchetsUpOnly:
    def test_a_higher_reading_raises_the_peak(self, conn):
        pid = opened(conn, entry_volume_multiple=6.0)
        ps.observe(conn, pid, volume_multiple=8.0, price=112.0, now=T0)
        row = ps.load_by_symbol(conn, "ABC")
        assert row["peak_volume_multiple"] == 8.0
        assert row["price_at_volume_peak"] == 112.0

    def test_a_lower_reading_is_decay_not_a_new_peak(self, conn):
        pid = opened(conn, entry_volume_multiple=6.0)
        ps.observe(conn, pid, volume_multiple=8.0, price=112.0, now=T0)
        ps.observe(conn, pid, volume_multiple=3.0, price=104.0, now=T0)
        row = ps.load_by_symbol(conn, "ABC")
        assert row["peak_volume_multiple"] == 8.0
        assert row["price_at_volume_peak"] == 112.0, "the peak's price stays"

    def test_the_decay_window_starts_and_restarts(self, conn):
        pid = opened(conn, entry_volume_multiple=6.0)
        ps.observe(conn, pid, volume_multiple=3.0, decayed=True, now=T0)
        assert ps.load_by_symbol(conn, "ABC")["decay_since"] is not None

        ps.observe(conn, pid, volume_multiple=6.0, decayed=False,
                   now=T0 + timedelta(minutes=5))
        assert ps.load_by_symbol(conn, "ABC")["decay_since"] is None

    def test_an_unknown_position_is_a_no_op(self, conn):
        ps.observe(conn, "s2pos_missing", volume_multiple=9.0)


class TestOnePositionPerSymbol:
    def test_a_second_open_in_the_same_name_is_impossible(self, conn):
        """At the storage layer, not merely unlikely at the gate."""
        import sqlite3

        opened(conn)
        with pytest.raises(sqlite3.IntegrityError):
            opened(conn)

    def test_a_closed_position_frees_the_name(self, conn):
        pid = opened(conn)
        ps.close_position(conn, pid, reason="VOLUME_DECAY", now=T0)
        assert opened(conn, average_fill_price=101.0)


class TestExitLatching:
    def test_the_first_reason_wins(self, conn):
        """Relabelling on a later tick would make the exit study measure
        whichever condition happened to be evaluated last."""
        pid = opened(conn)
        assert ps.latch_pending_exit(conn, pid, "VWAP_FAILURE", now=T0) is True
        assert ps.latch_pending_exit(conn, pid, "HARD_STOP", now=T0) is False
        assert ps.load_by_symbol(conn, "ABC")["pending_exit_reason"] == \
            "VWAP_FAILURE"

    def test_exit_submitted_is_one_way(self, conn):
        pid = opened(conn)
        assert ps.mark_exit_submitted(conn, pid, "VOLUME_DECAY", now=T0) is True
        assert ps.mark_exit_submitted(conn, pid, "HARD_STOP", now=T0) is False
        row = ps.load_by_symbol(conn, "ABC")
        assert row["exit_submitted"] == 1
        assert row["status"] == ps.EXIT_SUBMITTED

    def test_a_submitted_exit_makes_the_policy_hold_forever(self, conn):
        from s2_live import exit_policy

        pid = opened(conn)
        ps.mark_exit_submitted(conn, pid, "VOLUME_DECAY", now=T0)
        state = ps.to_state(ps.load_by_symbol(conn, "ABC"))
        decision = exit_policy.decide(state, current_price=1.0)
        assert decision.action == exit_policy.HOLD
        assert decision.reason == exit_policy.REASON_ALREADY_SUBMITTED

    def test_closing_records_how_it_ended(self, conn):
        pid = opened(conn)
        assert ps.close_position(conn, pid, reason="SESSION_EXIT", now=T0)
        row = conn.execute("SELECT * FROM s2_positions WHERE position_id = ?",
                           (pid,)).fetchone()
        assert row["status"] == ps.CLOSED
        assert row["exit_reason"] == "SESSION_EXIT"
        assert row["closed_at"]
        assert ps.load_by_symbol(conn, "ABC") is None


class TestLoadingForTheTick:
    def test_pending_exits_lead(self, conn):
        """The position a tick must not run out of time before reaching."""
        first = opened(conn, symbol="AAA")
        second = opened(conn, symbol="BBB")
        ps.latch_pending_exit(conn, second, "VWAP_FAILURE", now=T0)
        assert [pid for pid, _ in ps.load_live(conn)][0] == second
        assert first in [pid for pid, _ in ps.load_live(conn)]

    def test_open_count_drives_the_position_limit(self, conn):
        assert ps.open_count(conn) == 0
        pid = opened(conn)
        assert ps.open_count(conn) == 1
        ps.close_position(conn, pid, now=T0)
        assert ps.open_count(conn) == 0

    def test_a_stored_row_becomes_the_pure_policy_state(self, conn):
        pid = opened(conn, entry_volume_multiple=6.0, baseline_volume=1_000_000)
        ps.observe(conn, pid, volume_multiple=8.0, price=112.0, decayed=True,
                   now=T0)
        state = ps.to_state(ps.load_by_symbol(conn, "ABC"))
        assert state.peak_volume_multiple == 8.0
        assert state.price_at_volume_peak == 112.0
        assert state.baseline_volume == 1_000_000
        assert isinstance(state.decay_since, datetime)


class TestReconciliationShape:
    def test_holdings_carry_the_venue(self, conn):
        """KIS answers a NASD request with NYSE rows, so the symbol alone
        is not an identity -- the correction TX needed."""
        opened(conn, symbol="AAA", venue="NASD", quantity=1)
        opened(conn, symbol="BBB", venue="NYSE", quantity=1)
        assert sorted(ps.holdings(conn)) == [("AAA", "NASD", 1),
                                             ("BBB", "NYSE", 1)]

    def test_closed_positions_are_not_held(self, conn):
        pid = opened(conn)
        ps.close_position(conn, pid, now=T0)
        assert ps.holdings(conn) == []

    def test_the_shape_matches_what_s1_reports(self, conn):
        """So a UNION answers "what do we hold" without a refactor."""
        opened(conn, venue="NASD")
        for symbol, venue, qty in ps.holdings(conn):
            assert isinstance(symbol, str) and isinstance(qty, int)
            assert venue is None or isinstance(venue, str)


class TestTheStoreDecidesNothing:
    def test_it_imports_no_policy_and_no_broker(self):
        import ast

        banned = {"brokers", "kis_broker", "execution_engine", "order_gate",
                  "kis_live_trading", "position_limits", "s2_exit_v0"}
        source = (REPO_ROOT / "s2_live" / "position_store.py").read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [str(getattr(node, "module", "") or "")]
                names += [a.name for a in node.names]
                for name in names:
                    for segment in name.split("."):
                        assert segment not in banned, f"imports {name}"
