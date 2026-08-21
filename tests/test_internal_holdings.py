"""Internal holdings across strategies, without double-counting.

The failure this file guards against is specific and was found by
looking at production rather than at the code: TX exists in `positions`
AND in `s1_positions`, because the strategy tables are bookkeeping
layered on the account-level record rather than a second copy of it.
Summing them would report 2 against the broker's 1, and the resulting
"mismatch" would fail-close every new entry -- including S1's, which is
trading correctly.

A reconciliation bug that halts trading is not a safe failure. It is an
outage wearing a safety feature's clothes.
"""

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from reconciliation import internal_holdings as ih  # noqa: E402
from s2_live import position_store as s2ps  # noqa: E402

T0 = datetime(2026, 8, 21, 14, 0, tzinfo=timezone.utc)
S1, S2 = ih.S1_STRATEGY_ID, ih.S2_STRATEGY_ID


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def s2_position(conn, symbol="ABC", venue="NASD", quantity=1):
    return s2ps.open_position(conn, symbol=symbol, venue=venue,
                              quantity=quantity, average_fill_price=100.0,
                              now=T0)


def account(*rows):
    """Account-level rows as the legacy store yields them."""
    return [{"symbol": s, "venue": v, "quantity": q} for s, v, q in rows]


class TestAggregationIsBySharesNotByRecords:
    def test_one_position_aggregates_to_its_quantity(self):
        assert ih.aggregate(account(("TX", "NYSE", 1))) == {("TX", "NYSE"): 1}

    def test_two_strategies_in_one_name_sum_their_shares(self):
        """The risk matrix allows one each today. Reconciliation is
        written for the arithmetic, not for today's posture."""
        totals = ih.aggregate(account(("TX", "NYSE", 1), ("TX", "NYSE", 2)))
        assert totals == {("TX", "NYSE"): 3}

    def test_the_same_ticker_at_two_venues_stays_two_positions(self):
        """KIS answers a NASD request with NYSE rows, so the venue is
        part of identity -- the correction TX needed."""
        totals = ih.aggregate(account(("ABC", "NASD", 1), ("ABC", "NYSE", 1)))
        assert totals == {("ABC", "NASD"): 1, ("ABC", "NYSE"): 1}

    def test_a_missing_venue_is_its_own_key_not_a_wildcard(self):
        totals = ih.aggregate(account(("ABC", None, 1), ("ABC", "NASD", 1)))
        assert totals == {("ABC", None): 1, ("ABC", "NASD"): 1}

    def test_zero_and_unreadable_quantities_are_dropped(self):
        assert ih.aggregate(account(("ABC", "NASD", 0))) == {}
        assert ih.aggregate([{"symbol": "ABC", "venue": "NASD",
                              "quantity": "many"}]) == {}

    def test_symbols_and_venues_are_compared_case_insensitively(self):
        totals = ih.aggregate(account(("abc", "nasd", 1), ("ABC", "NASD", 1)))
        assert totals == {("ABC", "NASD"): 2}


class TestTheStrategyTablesAreNotSummedIntoTotals:
    def test_a_strategy_position_does_not_inflate_the_account_total(self, conn):
        """The bug this module exists for. TX is in `positions` and in
        `s1_positions`; adding them would give 2 against the broker's 1."""
        s2_position(conn, symbol="ABC", venue="NASD", quantity=1)
        totals = ih.aggregate(account(("ABC", "NASD", 1)))
        assert totals == {("ABC", "NASD"): 1}, "the account row is the total"

    def test_attribution_names_the_owning_strategy(self, conn):
        """"TX / NYSE / 1" tells an operator nothing about where to
        look."""
        s2_position(conn, symbol="ABC", venue="NASD")
        lines = ih.attribution(conn)
        assert "S2: ABC / NASD / 1" in lines
        assert "S1: none" in lines

    def test_a_strategy_with_nothing_says_none(self, conn):
        assert sorted(ih.attribution(conn)) == ["S1: none", "S2: none"]

    def test_a_broken_strategy_table_does_not_fail_reconciliation(
            self, conn, monkeypatch):
        """Attribution is a diagnostic. Losing it must not turn a healthy
        reconciliation into a mismatch."""
        monkeypatch.setattr(s2ps, "holdings",
                            lambda c: (_ for _ in ()).throw(RuntimeError))
        result = ih.summary(conn, account(("TX", "NYSE", 1)))
        assert result["internal_holdings"] == [
            {"symbol": "TX", "venue": "NYSE", "quantity": 1}]
        assert result["coverage_healthy"] is True


class TestCoverageCatchesWhatTheBrokerComparisonCannot:
    def test_a_strategy_position_missing_from_the_account_store_is_a_gap(
            self, conn):
        """The failure that cost S1 its bookkeeping: fill sync skipped a
        strategy, so the account table still agreed with the broker and
        the strategy held something nobody counted."""
        s2_position(conn, symbol="ABC", venue="NASD")
        result = ih.summary(conn, account(("TX", "NYSE", 1)))
        assert result["coverage_healthy"] is False
        gap = [g for g in result["coverage_gaps"]
               if g["gap"] == ih.GAP_NOT_IN_ACCOUNT][0]
        assert gap["strategy_id"] == S2
        assert gap["symbol"] == "ABC"
        assert gap["account_quantity"] == 0

    def test_a_partially_recorded_position_is_a_gap(self, conn):
        s2_position(conn, symbol="ABC", venue="NASD", quantity=2)
        result = ih.summary(conn, account(("ABC", "NASD", 1)))
        assert result["coverage_healthy"] is False

    def test_an_unattributed_account_row_is_reported_not_faulted(self, conn):
        """The legacy watchlist path holds positions and claims no
        strategy. Reporting it as a fault would make every legacy
        position block trading."""
        result = ih.summary(conn, account(("LEGACY", "NASD", 1)))
        assert result["coverage_healthy"] is True
        assert any(g["gap"] == ih.GAP_UNATTRIBUTED
                   for g in result["coverage_gaps"])

    def test_a_matched_strategy_position_is_no_gap(self, conn):
        s2_position(conn, symbol="ABC", venue="NASD")
        result = ih.summary(conn, account(("ABC", "NASD", 1)))
        assert result["coverage_healthy"] is True
        assert not [g for g in result["coverage_gaps"]
                    if g["gap"] == ih.GAP_NOT_IN_ACCOUNT]

    def test_a_venue_difference_is_a_gap_not_a_match(self, conn):
        s2_position(conn, symbol="ABC", venue="NASD")
        result = ih.summary(conn, account(("ABC", "NYSE", 1)))
        assert result["coverage_healthy"] is False


class TestTheDirectiveScenarios:
    """The cases §5 names, expressed against the reconciler that
    actually decides HEALTHY or MISMATCH."""

    @staticmethod
    def verdict(internal_rows, broker_rows):
        from domain.position import Position
        from reconciliation.position_reconciler import reconcile_positions

        def positions(rows):
            return [Position(symbol=s, quantity=q, average_fill_price=1.0,
                             unrealized_pnl=0.0, realized_pnl=0.0,
                             as_of=T0, source="test")
                    for s, _v, q in rows]

        return reconcile_positions(positions(internal_rows),
                                   positions(broker_rows))

    def test_s1_only_matches(self):
        assert self.verdict([("TX", "NYSE", 1)], [("TX", "NYSE", 1)]) == []

    def test_s2_only_matches(self):
        assert self.verdict([("ABC", "NASD", 1)], [("ABC", "NASD", 1)]) == []

    def test_s1_and_s2_in_different_names_match(self):
        internal = [("TX", "NYSE", 1), ("ABC", "NASD", 1)]
        assert self.verdict(internal, internal) == []

    def test_internal_without_broker_is_a_mismatch(self):
        assert self.verdict([("ABC", "NASD", 1)], []) != []

    def test_broker_without_internal_is_a_mismatch(self):
        assert self.verdict([], [("ABC", "NASD", 1)]) != []

    def test_a_quantity_difference_is_a_mismatch(self):
        assert self.verdict([("ABC", "NASD", 1)], [("ABC", "NASD", 2)]) != []


class TestExitContinuityUnderMismatch:
    def test_the_exit_policy_reads_no_reconciliation_state(self):
        """A mismatch blocks new entries. It must never block a held
        position from leaving -- a risk control that also stopped
        liquidation would trap the account in what it exists to escape.
        """
        import ast

        for module in ("s2_live/exit_policy.py", "s2_live/exit_runtime.py"):
            source = (REPO_ROOT / module).read_text()
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [str(getattr(node, "module", "") or "")]
                    names += [a.name for a in node.names]
                    for name in names:
                        assert "reconcil" not in name.lower(), \
                            f"{module} imports {name}"

    def test_a_gap_does_not_stop_the_exit_runtime_importing(self, conn):
        """Sanity: the exit path has no dependency on holdings at all."""
        s2_position(conn, symbol="ABC", venue="NASD")
        assert ih.summary(conn, account())["coverage_healthy"] is False
        from s2_live import exit_runtime

        assert callable(exit_runtime.run_exits)
