"""Many symbols at once, but never the same one twice in a day.

What changed
------------
LIMITED_LIVE pinned the account to one S6 position and two overall so
the first real orders could be counted by hand. Those numbers were
scaffolding for a test, and leaving them in place would have made the
test's shape the permanent risk model. They are gone; capacity is now
bounded by things that are actually about risk -- orderable cash, one
position per symbol, the same-day re-entry block, ownership and
reconciliation -- none of which can be configured off.

The trade this file exists for
------------------------------
DT, 2026-08-26. S6 bought at 50.79, sold at 50.87 on RANGE_REENTRY --
its own rule saying the breakout had failed -- and ninety-five minutes
later the same scanner ranked DT fourth again and S6 bought it back at
52.75. Every gate passed. The system had no opinion about buying back
what it had just decided to leave, and now it does.

Nothing here places an order.
"""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from execution import entry_limits, reentry_policy  # noqa: E402
from execution.entry_limits import EntryLimitState  # noqa: E402
from execution.order_gate import OrderGateBlockedError, evaluate_buy_gate  # noqa: E402
from s6_live import position_store as s6ps  # noqa: E402

from tests import test_order_gate as gate_fixtures  # noqa: E402

S6 = "S6_ORB_BREAKOUT_V1"
S1 = "S1_HMA_EARLY_TREND_V1"
# 16:00 ET on a Wednesday -- inside the US trading day the block is
# scoped to, so `us_trading_day` and `closed_at` agree.
NOW = datetime(2026, 8, 26, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def _limits(**overrides):
    """Operating posture: no count caps, symbol locks enforced."""
    kwargs = dict(
        max_open_positions=None,
        max_daily_entries=None,
        max_positions_per_strategy=None,
        open_position_symbols=frozenset(),
        pending_entry_symbols=frozenset(),
        daily_entry_count=0,
        trading_day="2026-08-26",
        strategy_symbols={},
        unattributed_symbols=frozenset(),
        same_day_exits={},
    )
    kwargs.update(overrides)
    return EntryLimitState(**kwargs)


def _ctx(symbol="AAPL", strategy_id=S6, **overrides):
    kwargs = dict(
        order_intent=gate_fixtures._order_intent(
            symbol=symbol, strategy_id=strategy_id),
        signal=gate_fixtures._signal(symbol=symbol, strategy_id=strategy_id),
        allowed_symbols=frozenset({symbol}),
        instrument=gate_fixtures._instrument(),
        reconciliation=gate_fixtures._snapshot(symbol=symbol),
        entry_limits=_limits(),
    )
    kwargs.update(overrides)
    return gate_fixtures._buy_ctx(**kwargs)


def _blocked(ctx):
    with pytest.raises(OrderGateBlockedError) as excinfo:
        evaluate_buy_gate(ctx)
    return excinfo.value


def _closed_s6(conn, symbol, *, price=50.79, exit_price=50.87,
               reason="RANGE_REENTRY", now=NOW):
    pid = s6ps.record_submission(conn, symbol=symbol, variant="S6-R",
                                 entry_session="REGULAR",
                                 client_order_id=f"k-{symbol}", now=now)
    s6ps.open_from_fill(conn, pid, quantity=1, average_fill_price=price,
                        venue="NYSE", now=now)
    s6ps.close_position(conn, pid, reason=reason, exit_price=exit_price,
                        exit_session="REGULAR", now=now)
    return pid


class TestManySymbolsAtOnce:
    """§D -- the lock is per symbol, not per strategy."""

    def test_a_second_symbol_is_allowed_while_the_first_is_open(self):
        ctx = _ctx(symbol="AAPL", entry_limits=_limits(
            strategy_symbols={"S6": frozenset({"PLTR"})}))
        assert evaluate_buy_gate(ctx) is True

    def test_a_third_and_fourth_are_allowed_too(self):
        ctx = _ctx(symbol="MSFT", entry_limits=_limits(
            strategy_symbols={"S6": frozenset({"PLTR", "AAPL", "NVDA"})}))
        assert evaluate_buy_gate(ctx) is True

    def test_the_same_symbol_is_refused_while_open(self):
        ctx = _ctx(symbol="AAPL", entry_limits=_limits(
            strategy_symbols={"S6": frozenset({"AAPL"})}))
        assert _blocked(ctx).code == entry_limits.SYMBOL_ALREADY_HELD

    def test_the_same_symbol_is_refused_while_its_exit_is_in_flight(self):
        """EXIT_PENDING and EXIT_SUBMITTED still hold shares. The slot is
        released by the fill, not by the decision to sell."""
        ctx = _ctx(symbol="DT", entry_limits=_limits(
            strategy_symbols={"S6": frozenset({"DT"})}))
        assert _blocked(ctx).code == entry_limits.SYMBOL_ALREADY_HELD

    def test_another_strategys_open_symbol_does_not_block_this_one(self):
        """S1 holding TX is not S6's business -- ownership is, and that
        is a separate fail-closed check."""
        ctx = _ctx(symbol="AAPL", strategy_id=S6, entry_limits=_limits(
            strategy_symbols={"S1": frozenset({"TX"})}))
        assert evaluate_buy_gate(ctx) is True

    def test_an_unattributed_symbol_still_blocks_that_symbol(self):
        """A holding nobody can claim might be this strategy's."""
        ctx = _ctx(symbol="AAPL", entry_limits=_limits(
            unattributed_symbols=frozenset({"AAPL"})))
        assert _blocked(ctx).code == entry_limits.SYMBOL_ALREADY_HELD


class TestSameDayReentry:
    """§E -- sold today, not bought again today."""

    def test_the_exact_DT_case(self):
        ctx = _ctx(symbol="DT", entry_limits=_limits(same_day_exits={
            "S6": {"DT": {"exit_reason": "RANGE_REENTRY",
                          "exit_price": 50.87,
                          "position_id": "s6pos_1"}}}))
        blocked = _blocked(ctx)
        assert blocked.code == reentry_policy.SAME_DAY_REENTRY_BLOCK
        assert "RANGE_REENTRY" in str(blocked)

    def test_a_symbol_not_sold_today_is_unaffected(self):
        ctx = _ctx(symbol="AAPL", entry_limits=_limits(same_day_exits={
            "S6": {"DT": {"exit_reason": "RANGE_REENTRY"}}}))
        assert evaluate_buy_gate(ctx) is True

    def test_another_strategys_exit_does_not_block_this_strategy(self):
        """§F -- the rule is per (strategy, symbol). A different
        strategy wanting the same name is an OWNERSHIP question, which
        reconciliation answers and fails closed on separately; it is
        never waved through merely for being a different strategy."""
        ctx = _ctx(symbol="DT", strategy_id=S6, entry_limits=_limits(
            same_day_exits={"S1": {"DT": {"exit_reason": "STOP"}}}))
        assert evaluate_buy_gate(ctx) is True

    def test_the_block_names_the_previous_exit(self):
        ctx = _ctx(symbol="DT", entry_limits=_limits(same_day_exits={
            "S6": {"DT": {"exit_reason": "VWAP_FAILURE", "exit_price": 12.5}}}))
        message = str(_blocked(ctx))
        assert "VWAP_FAILURE" in message and "12.5" in message
        assert "next trading day" in message


class TestTheBlockIsDerivedFromHistory:
    """§E -- no blacklist table, no daily cleanup job."""

    def test_a_closed_position_today_blocks_the_symbol(self, conn):
        _closed_s6(conn, "DT")
        assert reentry_policy.blocked_symbols(
            conn, strategy_id=S6, now=NOW) == frozenset({"DT"})

    def test_it_lifts_by_itself_on_the_next_trading_day(self, conn):
        """Nothing clears anything -- the query is scoped to today, so
        tomorrow simply does not match."""
        _closed_s6(conn, "DT")
        tomorrow = NOW + timedelta(days=1)
        assert reentry_policy.blocked_symbols(
            conn, strategy_id=S6, now=tomorrow) == frozenset()

    def test_an_ownership_release_is_not_an_exit(self, conn):
        """The row was never this strategy's position, so it never sold
        it. Blocking on this would bar a symbol it has not traded."""
        _closed_s6(conn, "DT", reason="RELEASED_WRONGLY_ATTRIBUTED")
        assert reentry_policy.blocked_symbols(
            conn, strategy_id=S6, now=NOW) == frozenset()

    def test_an_abandoned_entry_is_not_an_exit(self, conn):
        _closed_s6(conn, "DT", reason="BUY_NEVER_FILLED")
        assert reentry_policy.blocked_symbols(
            conn, strategy_id=S6, now=NOW) == frozenset()

    def test_an_open_position_is_not_an_exit(self, conn):
        pid = s6ps.record_submission(conn, symbol="DT", variant="S6-R",
                                     entry_session="REGULAR",
                                     client_order_id="k1", now=NOW)
        s6ps.open_from_fill(conn, pid, quantity=1, average_fill_price=50.0,
                            venue="NYSE", now=NOW)
        assert reentry_policy.blocked_symbols(
            conn, strategy_id=S6, now=NOW) == frozenset()

    def test_the_exit_that_caused_the_block_is_reported(self, conn):
        _closed_s6(conn, "DT", exit_price=50.87)
        found = reentry_policy.exits_today(conn, strategy_id=S6, now=NOW)
        assert found["DT"]["exit_reason"] == "RANGE_REENTRY"
        assert found["DT"]["exit_price"] == 50.87

    def test_an_unreadable_history_refuses_rather_than_permits(self, conn,
                                                               monkeypatch):
        class _Boom:
            def execute(self, *a, **k):
                raise RuntimeError("db gone")

        with pytest.raises(reentry_policy.ReentryStateUnavailable):
            reentry_policy.exits_today(_Boom(), strategy_id=S6, now=NOW)

    def test_an_unknown_strategy_refuses(self, conn):
        with pytest.raises(reentry_policy.ReentryStateUnavailable):
            reentry_policy.exits_today(conn, strategy_id="NOPE_V1", now=NOW)


class TestTheCountCapsAreGoneButStillHonouredIfSet:
    """§A and §R -- removed as a default, not disabled as a mechanism."""

    def test_no_cap_means_no_count_block(self):
        ctx = _ctx(symbol="AAPL", entry_limits=_limits(
            open_position_symbols=frozenset({"A", "B", "C", "D", "E"}),
            strategy_symbols={"S6": frozenset({"A", "B", "C"})},
            daily_entry_count=99))
        assert evaluate_buy_gate(ctx) is True

    def test_a_cap_an_operator_sets_is_still_enforced(self):
        """Not faked with 9999: an operator who wants a ceiling gets it."""
        ctx = _ctx(symbol="AAPL", entry_limits=_limits(
            max_open_positions=2,
            open_position_symbols=frozenset({"X", "Y"})))
        assert _blocked(ctx).code == entry_limits.MAX_OPEN_POSITIONS

    def test_a_per_strategy_cap_an_operator_sets_is_still_enforced(self):
        ctx = _ctx(symbol="AAPL", entry_limits=_limits(
            max_positions_per_strategy=1,
            strategy_symbols={"S6": frozenset({"PLTR"})}))
        assert _blocked(ctx).code == entry_limits.MAX_STRATEGY_POSITIONS

    def test_a_daily_cap_an_operator_sets_is_still_enforced(self):
        ctx = _ctx(symbol="AAPL", entry_limits=_limits(
            max_daily_entries=3, daily_entry_count=3))
        assert _blocked(ctx).code == entry_limits.MAX_DAILY_ENTRIES

    def test_the_specific_reason_wins_over_a_count(self):
        """A candidate we already hold reports that, not 'account full'."""
        ctx = _ctx(symbol="AAPL", entry_limits=_limits(
            max_open_positions=1,
            open_position_symbols=frozenset({"AAPL"}),
            strategy_symbols={"S6": frozenset({"AAPL"})}))
        assert _blocked(ctx).code == entry_limits.SYMBOL_ALREADY_HELD

    def test_the_reentry_block_wins_over_a_count(self):
        ctx = _ctx(symbol="DT", entry_limits=_limits(
            max_open_positions=1,
            open_position_symbols=frozenset({"ZZZ"}),
            same_day_exits={"S6": {"DT": {"exit_reason": "RANGE_REENTRY"}}}))
        assert _blocked(ctx).code == reentry_policy.SAME_DAY_REENTRY_BLOCK


class TestAmbiguityStillFailsClosed:
    """§C and §9 -- removing a test cap removed no safety."""

    def test_an_unnamed_strategy_is_refused(self):
        ctx = _ctx(symbol="AAPL", strategy_id="MYSTERY_V1")
        assert _blocked(ctx).code == entry_limits.STRATEGY_ATTRIBUTION_UNKNOWN

    def test_missing_limit_state_is_refused(self):
        ctx = _ctx(symbol="AAPL", entry_limits=None)
        assert _blocked(ctx).code == entry_limits.POSITION_LIMIT_STATE_UNKNOWN

    def test_intent_and_signal_must_name_the_same_strategy(self):
        ctx = _ctx(symbol="AAPL", strategy_id=S6,
                   signal=gate_fixtures._signal(symbol="AAPL", strategy_id=S1))
        assert _blocked(ctx).code == entry_limits.STRATEGY_ATTRIBUTION_UNKNOWN

    def test_an_unreadable_exit_history_makes_the_state_unavailable(self,
                                                                    monkeypatch):
        """The collector must not report 'nothing sold today' when it
        could not read -- that is exactly the re-entry it prevents."""
        monkeypatch.setattr(
            "execution.reentry_policy.exits_today",
            lambda *a, **k: (_ for _ in ()).throw(
                reentry_policy.ReentryStateUnavailable("unreadable")))
        with pytest.raises(entry_limits.EntryLimitStateUnavailable):
            entry_limits._same_day_exits_by_slot(
                None, trading_day="2026-08-26", now=NOW)
