"""Actually running the post-exit observations.

The gap this covers
-------------------
`post_exit/` had registration, a schema, a roll-up and no data. On
2026-08-29 the live database held three tracked exits --
DT/SESSION_EXIT, OWL/RANGE_REENTRY, SBS/SESSION_EXIT -- all still
TRACKING, with `post_exit_observations` empty and every metric NULL.
`due_for_observation` was called from tests and nowhere else. Three real
exits had gone unmeasured because nothing ever asked what happened next.

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
from post_exit import observations, tracker  # noqa: E402
from s6_live import position_store as s6ps  # noqa: E402

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "run_post_exit_observations",
    REPO_ROOT / "scripts" / "run_post_exit_observations.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


def _code_tokens_of(script_name):
    """Every identifier in a script, with docstrings and comments gone."""
    import ast
    import io
    import tokenize

    path = REPO_ROOT / "scripts" / script_name
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            node.value.value = ""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
    return names


S6 = "S6_ORB_BREAKOUT_V1"
EXIT_AT = datetime(2026, 8, 28, 19, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DB", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def _closed(conn, symbol="OWL", exit_reason="RANGE_REENTRY", exit_price=12.11):
    pid = s6ps.record_submission(conn, symbol=symbol, variant="S6-R",
                                 entry_session="REGULAR",
                                 client_order_id=f"kislive-{symbol}-1",
                                 now=EXIT_AT - timedelta(hours=2))
    s6ps.open_from_fill(conn, pid, quantity=1, average_fill_price=12.13,
                        venue="NYSE", now=EXIT_AT - timedelta(hours=2))
    s6ps.close_position(conn, pid, reason=exit_reason, now=EXIT_AT,
                        exit_price=exit_price)
    return pid


def _row(conn):
    return tracker.due_for_observation(conn, now=EXIT_AT + timedelta(minutes=1))[0]


class TestClosingATradeRegistersItForObservation:
    def test_a_closed_s6_position_becomes_a_tracking_row(self, conn):
        _closed(conn)
        rows = tracker.due_for_observation(conn, now=EXIT_AT + timedelta(minutes=1))
        assert [r["symbol"] for r in rows] == ["OWL"]
        assert rows[0]["exit_reason"] == "RANGE_REENTRY"


class TestOnlyHorizonsThatHaveActuallyArrived:
    def test_nothing_is_due_the_moment_the_trade_closes(self, conn):
        _closed(conn)
        assert runner.due_horizons(_row(conn), now=EXIT_AT, already=set()) == []

    def test_the_five_minute_mark_comes_due_at_five_minutes(self, conn):
        _closed(conn)
        due = runner.due_horizons(_row(conn), now=EXIT_AT + timedelta(minutes=5),
                                  already=set())
        assert [h for h, _m in due] == [post_exit_policy.HORIZON_M5]

    def test_every_passed_mark_is_due_at_once_after_a_gap(self, conn):
        """A runner that was down for an hour must not skip the marks it
        missed -- they are still answerable from stored bars."""
        _closed(conn)
        due = runner.due_horizons(_row(conn), now=EXIT_AT + timedelta(minutes=61),
                                  already=set())
        assert [h for h, _m in due] == ["M5", "M15", "M30", "M60"]

    def test_an_already_observed_mark_is_not_repeated(self, conn):
        _closed(conn)
        due = runner.due_horizons(_row(conn), now=EXIT_AT + timedelta(minutes=61),
                                  already={"M5", "M15"})
        assert [h for h, _m in due] == ["M30", "M60"]

    def test_a_row_without_an_exit_time_yields_nothing(self, conn):
        _closed(conn)
        row = dict(_row(conn))
        row["exit_time"] = None
        assert runner.due_horizons(row, now=EXIT_AT + timedelta(days=1),
                                   already=set()) == []

    def test_daily_horizons_are_not_claimed_from_an_intraday_feed(self, conn):
        """SAME_DAY_CLOSE and the next-day OHLC settle from daily bars.
        Answering them from an intraday price would put a mislabelled
        number in the record."""
        _closed(conn)
        due = {h for h, _m in runner.due_horizons(
            _row(conn), now=EXIT_AT + timedelta(days=3), already=set())}
        assert due == {"M5", "M15", "M30", "M60"}
        assert post_exit_policy.HORIZON_SAME_DAY_CLOSE not in due
        assert not due & set(post_exit_policy.NEXT_DAY_HORIZONS)


class TestAMissingPriceIsRecordedNotSkipped:
    """"Not observed yet" and "observed, no price" look identical if the
    second is omitted -- and the loop would chase that horizon forever."""

    def test_an_unavailable_price_is_stored_as_unavailable(self, conn):
        _closed(conn)
        runner.observe_row(conn, _row(conn),
                           price_lookup=lambda *a: (None, "no bars"),
                           now=EXIT_AT + timedelta(minutes=6))
        stored = observations.observations_for(conn, _row(conn)["tracking_id"])
        assert [o["horizon"] for o in stored] == ["M5"]
        assert stored[0]["status"] == post_exit_policy.OBSERVATION_UNAVAILABLE
        assert stored[0]["price"] is None

    def test_an_unavailable_mark_is_not_retried_forever(self, conn):
        _closed(conn)
        for _ in range(3):
            runner.observe_row(conn, _row(conn),
                               price_lookup=lambda *a: (None, "no bars"),
                               now=EXIT_AT + timedelta(minutes=6))
        stored = observations.observations_for(conn, _row(conn)["tracking_id"])
        assert len(stored) == 1

    def test_a_real_price_is_recorded_with_its_return(self, conn):
        _closed(conn, exit_price=12.11)
        runner.observe_row(conn, _row(conn),
                           price_lookup=lambda *a: (12.72, None),
                           now=EXIT_AT + timedelta(minutes=6))
        stored = observations.observations_for(conn, _row(conn)["tracking_id"])
        assert stored[0]["price"] == pytest.approx(12.72)
        # Sold at 12.11, traded at 12.72 five minutes later: it kept going.
        assert stored[0]["return_pct"] == pytest.approx(5.037, abs=0.01)


class TestABarTooFarAwayDoesNotAnswerForTheMark:
    def test_a_nearby_bar_answers(self, monkeypatch, tmp_path):
        lookup = self._lookup(monkeypatch, tmp_path, offset_minutes=1)
        price, detail = lookup("OWL", EXIT_AT, 15)
        assert price == pytest.approx(12.5)
        assert detail is None

    def test_a_distant_bar_does_not(self, monkeypatch, tmp_path):
        """Forty minutes from the +15m mark is a different fact wearing
        the same label, and these horizons are only worth having because
        they are comparable across trades."""
        lookup = self._lookup(monkeypatch, tmp_path, offset_minutes=40)
        price, detail = lookup("OWL", EXIT_AT, 15)
        assert price is None
        assert "from the mark" in detail

    def test_no_bars_at_all_is_reported_plainly(self, monkeypatch, tmp_path):
        from s6_live import kis_bar_features

        monkeypatch.setattr(kis_bar_features, "load_store",
                            lambda *a, **k: None)
        price, detail = runner._bar_price_lookup()("OWL", EXIT_AT, 15)
        assert price is None
        assert detail == "no collected bars for that symbol"

    def test_an_unreadable_store_does_not_raise(self, monkeypatch):
        from s6_live import kis_bar_features

        def _boom(*a, **k):
            raise RuntimeError("unreadable")

        monkeypatch.setattr(kis_bar_features, "load_store", _boom)
        price, _detail = runner._bar_price_lookup()("OWL", EXIT_AT, 15)
        assert price is None

    def _lookup(self, monkeypatch, tmp_path, *, offset_minutes):
        from market_data import realtime_bars
        from s6_live import kis_bar_features

        target = EXIT_AT + timedelta(minutes=15)
        bar = realtime_bars.Bar(
            symbol="OWL", session="REGULAR",
            minute=target + timedelta(minutes=offset_minutes),
            open=12.4, high=12.6, low=12.3, close=12.5, volume=100,
            trade_count=3, first_trade_at=target, last_trade_at=target)

        class _Store:
            def bars(self, symbol, session):
                return [bar] if session == "REGULAR" else []

        monkeypatch.setattr(kis_bar_features, "load_store",
                            lambda session, day, **k: _Store())
        return runner._bar_price_lookup()


class TestResearchNeverTouchesTrading:
    def test_the_runner_places_no_orders(self):
        source = (REPO_ROOT / "scripts" / "run_post_exit_observations.py").read_text()
        for forbidden in ("submit_buy", "submit_sell", "submit_order",
                          "cancel_order", "execution_engine", "order_gate",
                          "close_position"):
            assert forbidden not in source, forbidden

    def test_it_changes_no_strategy_parameter(self):
        """Post-Exit 결과로 threshold/Exit/stop/TP 자동 변경 금지 -- it
        produces evidence for a person to read.

        Checked against the CODE with docstrings stripped: the module
        quotes that rule in prose, and a plain substring search would
        match the quotation and pass for the wrong reason.
        """
        assert not (_code_tokens_of("run_post_exit_observations.py")
                    & {"threshold", "stop_price", "target_price",
                       "exit_policy", "stop_loss"})

    def test_it_does_not_call_the_broker(self):
        """It reads the collector's snapshot instead, so it adds nothing
        to the rate limiter that starved S1 once already."""
        source = (REPO_ROOT / "scripts" / "run_post_exit_observations.py").read_text()
        assert "kis_broker" not in source
        assert "from brokers" not in source
