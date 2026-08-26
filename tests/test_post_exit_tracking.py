"""Studying an exit after it happened, without touching the trade.

Two properties this file is mostly about
----------------------------------------
1. The research path cannot break the trading path. A closed position is
   a finished trade; if tracking cannot be opened, an observation cannot
   be recorded, or the price feed is down, the trade is still closed and
   the caller sees no error.

2. Results are never pooled across strategies. S6's RANGE_REENTRY and
   S1's structure exit answer different questions, and an average over
   both describes neither. Every figure is keyed by (strategy_id,
   exit_reason).

Nothing here places an order or reads a live feed.
"""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import post_exit_policy  # noqa: E402
from post_exit import analytics, observations, tracker  # noqa: E402
from s1_live import position_store as s1ps  # noqa: E402
from s6_live import position_store as s6ps  # noqa: E402

S6 = "S6_ORB_BREAKOUT_V1"
S1 = "S1_HMA_EARLY_TREND_V1"
NOW = datetime(2026, 8, 26, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def _s6_trade(conn, symbol="DT", entry=50.79, exit_price=50.87,
              reason="RANGE_REENTRY", now=NOW):
    pid = s6ps.record_submission(conn, symbol=symbol, variant="S6-R",
                                 entry_session="REGULAR",
                                 client_order_id=f"k-{symbol}", now=now)
    s6ps.open_from_fill(conn, pid, quantity=1, average_fill_price=entry,
                        venue="NYSE", now=now)
    s6ps.close_position(conn, pid, reason=reason, exit_price=exit_price,
                        exit_session="AFTER_HOURS", now=now)
    return pid


def _tracking(conn):
    return conn.execute("SELECT * FROM post_exit_tracking").fetchall()


class TestATrackingRowIsOpenedOnEveryRealExit:
    def test_the_DT_trade_is_described_completely(self, conn):
        _s6_trade(conn)
        row = _tracking(conn)[0]
        assert row["strategy_id"] == S6
        assert row["symbol"] == "DT"
        assert row["exit_reason"] == "RANGE_REENTRY"
        assert row["entry_price"] == 50.79
        assert row["exit_price"] == 50.87
        assert row["entry_session"] == "REGULAR"
        assert row["exit_session"] == "AFTER_HOURS"
        assert row["realized_pnl"] == pytest.approx(0.08)
        assert row["realized_pnl_pct"] == pytest.approx(0.1575, abs=1e-3)
        assert row["status"] == post_exit_policy.STATUS_TRACKING

    def test_it_works_for_a_different_strategy(self, conn):
        """§H -- common, not S6-only."""
        pid = s1ps.open_position(conn, symbol="TX", strategy_id=S1,
                                 signal_id="s1-TX", entry_price=53.68,
                                 quantity=1, now=NOW)
        s1ps.close_position(conn, pid, exit_reason="HMA_STRUCTURE_EXIT",
                            exit_price=55.00, exit_session="REGULAR", now=NOW)
        row = _tracking(conn)[0]
        assert row["strategy_id"] == S1
        assert row["exit_reason"] == "HMA_STRUCTURE_EXIT"
        assert row["realized_pnl"] == pytest.approx(1.32)

    def test_an_ownership_release_opens_nothing(self, conn):
        """It was never this strategy's position, so there is no trade
        to study."""
        _s6_trade(conn, reason="RELEASED_WRONGLY_ATTRIBUTED")
        assert _tracking(conn) == []

    def test_an_abandoned_entry_opens_nothing(self, conn):
        _s6_trade(conn, reason="BUY_NEVER_FILLED")
        assert _tracking(conn) == []

    def test_a_close_without_an_exit_price_opens_nothing(self, conn):
        """Realised P&L would have to be invented."""
        pid = s6ps.record_submission(conn, symbol="DT", variant="S6-R",
                                     entry_session="REGULAR",
                                     client_order_id="k1", now=NOW)
        s6ps.open_from_fill(conn, pid, quantity=1, average_fill_price=50.0,
                            venue="NYSE", now=NOW)
        s6ps.close_position(conn, pid, reason="SESSION_EXIT", now=NOW)
        assert _tracking(conn) == []

    def test_one_row_per_position(self, conn):
        pid = _s6_trade(conn)
        s6ps.close_position(conn, pid, reason="RANGE_REENTRY",
                            exit_price=50.87, now=NOW)
        assert len(_tracking(conn)) == 1


class TestTheResearchPathCannotBreakTheTrade:
    def test_a_broken_tracker_still_closes_the_position(self, conn, monkeypatch):
        monkeypatch.setattr(
            "post_exit.tracker._record",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        pid = _s6_trade(conn)
        assert s6ps.load(conn, pid)["status"] == "CLOSED"

    def test_a_missing_table_is_not_an_error(self, conn):
        conn.execute("DROP TABLE post_exit_tracking")
        conn.commit()
        pid = _s6_trade(conn)
        assert s6ps.load(conn, pid)["status"] == "CLOSED"

    def test_an_unavailable_price_is_recorded_not_raised(self, conn):
        _s6_trade(conn)
        tid = _tracking(conn)[0]["tracking_id"]
        assert observations.record(conn, tracking_id=tid, horizon="M5",
                                   price=None, now=NOW) is True
        row = conn.execute(
            "SELECT status, price FROM post_exit_observations "
            "WHERE tracking_id=?", (tid,)).fetchone()
        assert row["status"] == post_exit_policy.OBSERVATION_UNAVAILABLE
        assert row["price"] is None

    def test_an_unknown_horizon_is_refused_quietly(self, conn):
        _s6_trade(conn)
        tid = _tracking(conn)[0]["tracking_id"]
        assert observations.record(conn, tracking_id=tid, horizon="M7",
                                   price=1.0, now=NOW) is False


class TestTheMeasurements:
    def _tracked(self, conn):
        _s6_trade(conn, entry=50.79, exit_price=50.87)
        return _tracking(conn)[0]["tracking_id"]

    def test_upside_after_the_exit_is_forgone_profit(self, conn):
        tid = self._tracked(conn)
        observations.record(conn, tracking_id=tid, horizon="M15", price=51.50,
                            now=NOW)
        row = _tracking(conn)[0]
        assert row["exit_mfe_pct"] == pytest.approx(1.238, abs=1e-2)
        assert row["max_price_after_exit"] == 51.50

    def test_downside_after_the_exit_is_avoided_loss(self, conn):
        tid = self._tracked(conn)
        observations.record(conn, tracking_id=tid, horizon="M15", price=49.00,
                            now=NOW)
        row = _tracking(conn)[0]
        assert row["avoided_loss_pct"] == pytest.approx(3.675, abs=1e-2)

    def test_a_trade_that_only_rose_avoided_no_loss(self, conn):
        """Not a negative number: the exit avoided nothing, it gave up."""
        tid = self._tracked(conn)
        observations.record(conn, tracking_id=tid, horizon="M5", price=51.0,
                            now=NOW)
        observations.record(conn, tracking_id=tid, horizon="M15", price=52.0,
                            now=NOW)
        assert _tracking(conn)[0]["avoided_loss_pct"] == pytest.approx(0.0)

    def test_re_recording_a_horizon_is_the_same_fact(self, conn):
        tid = self._tracked(conn)
        observations.record(conn, tracking_id=tid, horizon="M5", price=51.0,
                            now=NOW)
        observations.record(conn, tracking_id=tid, horizon="M5", price=51.0,
                            now=NOW)
        assert len(observations.observations_for(conn, tid)) == 1


class TestTheWindowIsFinite:
    """§J -- a price far enough from the exit stops being evidence."""

    def test_S6_is_tracked_to_the_next_trading_day(self, conn):
        _s6_trade(conn)
        row = _tracking(conn)[0]
        end = datetime.fromisoformat(row["tracking_end_at"])
        assert end.date() == datetime(2026, 8, 27).date()

    def test_S1_gets_longer_than_S6(self, conn):
        assert (post_exit_policy.tracking_days_for(S1)
                > post_exit_policy.tracking_days_for(S6))

    def test_no_strategy_exceeds_the_common_maximum(self):
        for slot_days in post_exit_policy.TRACKING_DAYS_BY_SLOT.values():
            assert slot_days <= post_exit_policy.MAX_TRACKING_DAYS

    def test_a_strategy_asking_for_more_is_capped(self, monkeypatch):
        monkeypatch.setitem(post_exit_policy.TRACKING_DAYS_BY_SLOT, "S6", 99)
        assert post_exit_policy.tracking_days_for(S6) == \
            post_exit_policy.MAX_TRACKING_DAYS

    def test_the_window_skips_days_with_no_prices_in_them(self, conn):
        """A Friday exit is not 'tracked' across the weekend."""
        friday = datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc)
        _s6_trade(conn, now=friday)
        end = datetime.fromisoformat(_tracking(conn)[0]["tracking_end_at"])
        assert end.weekday() < 5

    def test_tracking_completes_once_the_window_passes(self, conn):
        _s6_trade(conn)
        assert tracker.complete_expired(
            conn, now=NOW + timedelta(days=10)) == 1
        assert _tracking(conn)[0]["status"] == post_exit_policy.STATUS_COMPLETED

    def test_a_row_inside_its_window_is_not_completed(self, conn):
        _s6_trade(conn)
        assert tracker.complete_expired(conn, now=NOW) == 0

    def test_due_for_observation_excludes_expired_rows(self, conn):
        _s6_trade(conn)
        assert len(tracker.due_for_observation(conn, now=NOW)) == 1
        assert tracker.due_for_observation(
            conn, now=NOW + timedelta(days=10)) == []


class TestAnalyticsAreNeverPooled:
    """§K -- one strategy's result never speaks for another's."""

    def _completed(self, conn, strategy, symbol, reason, exit_price):
        if strategy == S6:
            _s6_trade(conn, symbol=symbol, exit_price=exit_price,
                      reason=reason)
        else:
            pid = s1ps.open_position(conn, symbol=symbol, strategy_id=S1,
                                     signal_id=f"s1-{symbol}",
                                     entry_price=50.79, quantity=1, now=NOW)
            s1ps.close_position(conn, pid, exit_reason=reason,
                                exit_price=exit_price, now=NOW)
        conn.execute("UPDATE post_exit_tracking SET status = ?",
                     (post_exit_policy.STATUS_COMPLETED,))
        conn.commit()

    def test_two_strategies_produce_two_groups(self, conn):
        self._completed(conn, S6, "DT", "RANGE_REENTRY", 50.87)
        self._completed(conn, S1, "TX", "HMA_STRUCTURE_EXIT", 55.0)
        groups = analytics.summarise(conn)
        assert {(g["strategy_id"], g["exit_reason"]) for g in groups} == {
            (S6, "RANGE_REENTRY"), (S1, "HMA_STRUCTURE_EXIT")}
        assert all(g["sample_count"] == 1 for g in groups)

    def test_two_reasons_in_one_strategy_produce_two_groups(self, conn):
        self._completed(conn, S6, "DT", "RANGE_REENTRY", 50.87)
        self._completed(conn, S6, "AAPL", "VWAP_FAILURE", 49.0)
        groups = analytics.summarise(conn, strategy_id=S6)
        assert {g["exit_reason"] for g in groups} == {"RANGE_REENTRY",
                                                      "VWAP_FAILURE"}

    def test_there_is_no_all_strategies_total(self, conn):
        self._completed(conn, S6, "DT", "RANGE_REENTRY", 50.87)
        self._completed(conn, S1, "TX", "HMA_STRUCTURE_EXIT", 55.0)
        for group in analytics.summarise(conn):
            assert group["strategy_id"] is not None
            assert group["exit_reason"] is not None

    def test_incomplete_rows_are_excluded_by_default(self, conn):
        """A window that has not finished has not produced its best or
        worst moment yet."""
        _s6_trade(conn)
        assert analytics.summarise(conn) == []
        assert analytics.summarise(conn, include_incomplete=True)

    def test_the_sample_size_is_stated_in_words(self, conn):
        self._completed(conn, S6, "DT", "RANGE_REENTRY", 50.87)
        assert analytics.summarise(conn)[0]["interpretation"] == \
            post_exit_policy.OBSERVATION_ONLY

    @pytest.mark.parametrize("count,expected", [
        (0, post_exit_policy.OBSERVATION_ONLY),
        (19, post_exit_policy.OBSERVATION_ONLY),
        (20, post_exit_policy.TREND_INDICATION),
        (49, post_exit_policy.TREND_INDICATION),
        (50, post_exit_policy.MODIFICATION_CANDIDATE),
        (99, post_exit_policy.MODIFICATION_CANDIDATE),
        (100, post_exit_policy.STRATEGY_REVIEW_CANDIDATE),
    ])
    def test_the_bands_match_the_spec(self, count, expected):
        assert post_exit_policy.interpretation_for(count) == expected

    def test_horizon_performance_is_grouped_the_same_way(self, conn):
        self._completed(conn, S6, "DT", "RANGE_REENTRY", 50.87)
        tid = _tracking(conn)[0]["tracking_id"]
        observations.record(conn, tracking_id=tid, horizon="M5", price=51.0,
                            now=NOW)
        perf = analytics.horizon_performance(conn)
        assert all(len(key) == 3 for key in perf)
        assert (S6, "RANGE_REENTRY", "M5") in perf


class TestNothingHereChangesAStrategy:
    """§M -- the sample bands are labels on a report, never an action."""

    def test_no_module_in_the_package_imports_a_strategy_rule(self):
        """Checked on the import graph rather than on substrings: the
        package's own `post_exit_policy` contains the word `exit_policy`
        and is exactly the module it is allowed to read.

        A strategy's exit rules, entry permissions and thresholds live
        in modules this package must never touch. Reading one would be
        the first step towards a research result editing a live rule.
        """
        import ast
        import inspect

        from post_exit import analytics as a
        from post_exit import observations as o
        from post_exit import tracker as t

        forbidden = ("exit_policy", "entry_policy", "strategy_entry_policy",
                     "scanner_live_mode")
        for module in (a, o, t):
            tree = ast.parse(inspect.getsource(module))
            imported = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported += [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    base = node.module or ""
                    imported.append(base)
                    imported += [f"{base}.{alias.name}" for alias in node.names]
            for name in imported:
                leaf = name.rsplit(".", 1)[-1]
                assert leaf not in forbidden, (module.__name__, name)

    def test_the_package_touches_no_strategy_position_store_directly(self):
        """The tracker is handed a table name; it never reaches into a
        strategy module to decide what a position means."""
        import inspect

        from post_exit import tracker as t

        source = inspect.getsource(t)
        for forbidden in ("s1_live", "s2_live", "s6_live"):
            assert forbidden not in source, forbidden


class TestTheDTRegressionCaseIsMarked:
    """§P -- the trade that motivated the block stays in the data, and
    says so."""

    def test_a_note_can_be_attached_without_changing_the_trade(self, conn):
        pid = _s6_trade(conn)
        assert tracker.annotate(
            conn, position_id=pid,
            note=post_exit_policy.NOTE_REENTRY_POLICY_MISSING) is True
        row = _tracking(conn)[0]
        assert row["note"] == post_exit_policy.NOTE_REENTRY_POLICY_MISSING
        # Still a real trade, still counted.
        assert row["realized_pnl"] == pytest.approx(0.08)
        assert row["exit_reason"] == "RANGE_REENTRY"

    def test_annotating_an_unknown_position_is_not_an_error(self, conn):
        assert tracker.annotate(conn, position_id="nope", note="x") is False

    def test_a_noted_trade_is_still_included_in_the_statistics(self, conn):
        pid = _s6_trade(conn)
        tracker.annotate(conn, position_id=pid,
                         note=post_exit_policy.NOTE_REENTRY_POLICY_MISSING)
        conn.execute("UPDATE post_exit_tracking SET status = ?",
                     (post_exit_policy.STATUS_COMPLETED,))
        conn.commit()
        assert analytics.summarise(conn)[0]["sample_count"] == 1
