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
        """Every live strategy is listed, including the empty ones -- an
        absent line and a strategy holding nothing look identical
        otherwise."""
        assert sorted(ih.attribution(conn)) == ["S1: none", "S2: none",
                                                "S6: none"]

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


class TestTheAccountStoreRecordsNoVenue:
    """`positions` has no venue column, and inventing a fault out of a
    field it does not record would fire on every strategy position.

    Venue identity is enforced where it can be: against the broker's own
    rows. Here the question is only whether fill sync recorded the
    position at all.
    """

    def test_a_venueless_account_row_covers_a_venued_strategy_position(
            self, conn):
        s2_position(conn, symbol="ABC", venue="NASD")
        result = ih.summary(conn, [{"symbol": "ABC", "venue": None,
                                    "quantity": 1}])
        assert result["coverage_healthy"] is True
        assert result["coverage_gaps"] == []

    def test_it_still_catches_a_genuinely_missing_position(self, conn):
        s2_position(conn, symbol="ABC", venue="NASD")
        result = ih.summary(conn, [{"symbol": "OTHER", "venue": None,
                                    "quantity": 1}])
        assert result["coverage_healthy"] is False

    def test_it_still_catches_a_short_quantity(self, conn):
        s2_position(conn, symbol="ABC", venue="NASD", quantity=2)
        result = ih.summary(conn, [{"symbol": "ABC", "venue": None,
                                    "quantity": 1}])
        assert result["coverage_healthy"] is False

    def test_an_exact_venue_match_is_preferred_over_the_venueless_row(
            self, conn):
        s2_position(conn, symbol="ABC", venue="NASD", quantity=1)
        result = ih.summary(conn, [{"symbol": "ABC", "venue": "NASD",
                                    "quantity": 1},
                                   {"symbol": "ABC", "venue": None,
                                    "quantity": 5}])
        assert result["coverage_healthy"] is True


class TestAttributionCoversBothStrategies:
    """`strategy_holdings` looks the function up with `hasattr`, so a
    store without one answers "none" instead of failing. That is how
    attribution reported "S1: none" while TX was plainly held."""

    def test_s1s_store_exposes_holdings(self):
        from s1_live import position_store as s1ps

        assert hasattr(s1ps, "holdings"), \
            "a missing holdings() is silently an empty attribution"

    def test_an_s1_position_is_attributed(self, conn):
        from s1_live import position_store as s1ps

        s1ps.open_position(conn, symbol="TX", quantity=1, entry_price=53.68,
                           strategy_id=S1, signal_id="sig", now=T0)
        assert "S1: TX / - / 1" in ih.attribution(conn)

    def test_both_strategies_appear_together(self, conn):
        from s1_live import position_store as s1ps

        s1ps.open_position(conn, symbol="TX", quantity=1, entry_price=53.68,
                           strategy_id=S1, signal_id="sig", now=T0)
        s2_position(conn, symbol="ABC", venue="NASD")
        lines = ih.attribution(conn)
        assert "S1: TX / - / 1" in lines
        assert "S2: ABC / NASD / 1" in lines


class TestS6JoinsAttribution:
    """S6's positions are attribution, not additional quantity.

    The rule that made this safe for S2 applies unchanged: the account
    table is the total, and adding a strategy table on top would report
    two against the broker's one and fail-close every entry -- including
    the strategy trading correctly.
    """

    def s6_position(self, conn, symbol="ABC", venue="NASD", quantity=1):
        from s6_live import position_store as s6ps

        pid = s6ps.record_submission(conn, symbol=symbol, variant="S6-R",
                                     range_high=99.5, range_low=99.0, now=T0)
        s6ps.open_from_fill(conn, pid, quantity=quantity,
                            average_fill_price=100.0, venue=venue, now=T0)
        return pid

    def test_s6_appears_in_attribution(self, conn):
        self.s6_position(conn)
        assert "S6: ABC / NASD / 1" in ih.attribution(conn)

    def test_all_three_strategies_are_attributed_together(self, conn):
        from s1_live import position_store as s1ps

        s1ps.open_position(conn, symbol="TX", quantity=1, entry_price=53.68,
                           strategy_id=S1, signal_id="sig", now=T0)
        s2_position(conn, symbol="S2SYM", venue="NASD")
        self.s6_position(conn, symbol="S6SYM")
        lines = ih.attribution(conn)
        assert "S1: TX / - / 1" in lines
        assert "S2: S2SYM / NASD / 1" in lines
        assert "S6: S6SYM / NASD / 1" in lines

    def test_an_s6_position_does_not_inflate_the_account_total(self, conn):
        """The bug the module exists for, now with a third strategy."""
        self.s6_position(conn, symbol="ABC")
        totals = ih.aggregate(account(("ABC", "NASD", 1)))
        assert totals == {("ABC", "NASD"): 1}

    def test_broker_holding_with_no_s6_row_is_reported_unattributed(self, conn):
        result = ih.summary(conn, account(("ORPHAN", None, 1)))
        assert result["coverage_healthy"] is True
        assert any(g["gap"] == ih.GAP_UNATTRIBUTED
                   for g in result["coverage_gaps"])

    def test_an_s6_position_missing_from_the_account_store_is_a_gap(self, conn):
        """Fill sync did not record it -- the failure the broker
        comparison structurally cannot see."""
        self.s6_position(conn, symbol="ABC")
        result = ih.summary(conn, account(("OTHER", None, 1)))
        assert result["coverage_healthy"] is False
        gap = [g for g in result["coverage_gaps"]
               if g["gap"] == ih.GAP_NOT_IN_ACCOUNT][0]
        assert gap["strategy_id"] == ih.S6_STRATEGY_ID

    def test_a_matched_s6_position_is_healthy(self, conn):
        self.s6_position(conn, symbol="ABC")
        assert ih.summary(conn, account(("ABC", None, 1)))["coverage_healthy"]

    def test_an_unfilled_s6_order_is_not_counted_as_a_holding(self, conn):
        """SUBMITTED is a yes to "could a position appear" and a no to
        "what do we hold". Counting it here would report shares the
        account does not have."""
        from s6_live import position_store as s6ps

        s6ps.record_submission(conn, symbol="PENDING", range_high=99.5,
                               range_low=99.0, now=T0)
        result = ih.summary(conn, account())
        assert result["coverage_healthy"] is True
        assert "S6: none" in ih.attribution(conn)


class TestStructuralRiskIsRecordedNotApplied:
    def test_it_is_computed_from_the_entry_and_the_range_low(self):
        from scanners.publish import eligibility as el

        metrics = el.derived_metrics({"price": 100.0, "range_high": 99.5,
                                      "range_low": 99.0})
        assert metrics["structural_risk_pct"] == pytest.approx(1.0)

    def test_it_is_none_without_a_range(self):
        from scanners.publish import eligibility as el

        assert el.derived_metrics(
            {"price": 100.0})["structural_risk_pct"] is None

    def test_no_threshold_uses_it(self):
        """§5: recorded for the shadow study, not applied as a rule."""
        import ast

        for module in ("s6_live/exit_policy.py", "config/s6_exit_v0.py",
                       "s6_live/candidate_source.py"):
            source = (REPO_ROOT / module).read_text()
            assert "structural_risk_pct" not in source, module
