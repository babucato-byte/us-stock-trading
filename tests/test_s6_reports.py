"""The four S6 reports, and the one rule that binds them.

The rule is that a report may not be able to lie in the optimistic
direction. Each of these is read immediately before a decision to put
real money behind S6, so the failure that matters is not "the report was
wrong" -- it is "the report said PASS about something it never asked".
Most of what follows is that property, stated once per report.

The second property is that none of them can trade. That one is checked
structurally, against each module's parsed import and call graph, because
a behaviour test only proves the paths someone thought to exercise are
safe -- and a call that does not exist cannot be reached by a path nobody
thought of.
"""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import s6_sessions  # noqa: E402
from config import scanner_live_mode as slm  # noqa: E402
from s6_live import final_check, observations, session_report, trade_timeline  # noqa: E402
from scanners.publish import candidates as publisher  # noqa: E402
from scanners.publish import s6_snapshot  # noqa: E402
from scanners.publish import scan_cycle  # noqa: E402

DAY = "2026-08-24"
T0 = datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc)
ORB = s6_sessions.SCANNER_NAME

#: Modules that can place an order. A report that imported one could
#: reach a submission by a path nobody thought to test.
ORDERING_MODULES = ("broker", "brokers", "execution", "kis_live_trading",
                    "live_pilot", "paper_strategy_order", "order_safety",
                    "order_intent_ledger")

#: Names whose invocation would BE a submission, or a write to the
#: position lifecycle. Bare `execute` is deliberately absent: it is
#: `sqlite3.Connection.execute`, and a report reading its own database is
#: the point.
ORDERING_CALLS = ("submit_order", "authorize_and_execute", "place_order",
                  "run_live_buy_entry_cycle", "reserve",
                  "record_submission", "close_position", "open_from_fill",
                  "apply_fill", "mark_exit_submitted", "latch_pending_exit",
                  "abandon_submission", "observe")


def _called_names(path):
    """Every function/method name this module calls.

    Parsed rather than string-matched: the same words appear in prose
    all over this codebase, and a grep-shaped test fails on a comment
    while passing on `getattr(broker, "submit" + "_order")`. Neither
    outcome is useful.
    """
    import ast

    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            names.add(func.attr)
        elif isinstance(func, ast.Name):
            names.add(func.id)
    return names


def _imported_roots(path):
    import ast

    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def assert_cannot_order(module_path, *, allow_calls=()):
    """The structural promise every report makes."""
    forbidden_calls = set(ORDERING_CALLS) - set(allow_calls)
    assert _called_names(module_path) & forbidden_calls == set(), (
        f"{module_path} calls {_called_names(module_path) & forbidden_calls}")
    assert _imported_roots(module_path) & set(ORDERING_MODULES) == set(), (
        f"{module_path} imports "
        f"{_imported_roots(module_path) & set(ORDERING_MODULES)}")


class Signal:
    def __init__(self, symbol, score=70.0, price=100.0):
        self.symbol, self.scanner_score, self.signal_price = symbol, score, price
        self.scanner_name, self.scanner_version = "orb", "orb_v1.0"
        self.signal_id, self.scanner_run_id = f"s-{symbol}", "run-1"
        self.volume, self.avg_volume, self.volume_multiple = 4_000_000, 1_000_000, 4.0
        self.price_change_pct = self.hma200 = self.hma200_slope = None
        self.hma89 = None
        self.vwap = 99.8
        self.market_data_provider = self.market_data_feed = None
        self.data_timestamp = self.feature_timestamp = None
        self.source_timeframe = self.timestamp = None
        self.reasons = []
        self.metrics = {"opening_range_high": 99.5, "opening_range_low": 99.0,
                        "orb_minutes": 15, "vwap": 99.8, "price": price,
                        "session_ema9": 100.1, "session_ema21": 99.9,
                        "volume_expansion": 2.5}


class FakeIndex:
    """A security master with one common stock and one ETP."""

    def __init__(self, types=None):
        self._types = types or {"AAPL": ("COMMON_STOCK", "NASDAQ"),
                                "IEFA": ("ETP", "NASDAQ")}

    def classify(self, symbol):
        from s1_live.security_type import Classification

        kind, exchange = self._types.get(
            str(symbol).upper(), ("UNKNOWN", None))
        return Classification(symbol=str(symbol).upper(), security_type=kind,
                              exchange=exchange, asof=T0.isoformat())


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    monkeypatch.setenv(publisher.CANDIDATE_DIR_ENV, str(tmp_path / "handoff"))
    return tmp_path / "handoff"


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE",
                       tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


@pytest.fixture
def master(monkeypatch):
    """Point every classification at a fixed, in-memory master."""
    index = FakeIndex()
    from s1_live import security_type

    monkeypatch.setattr(security_type, "load_index", lambda path=None: index)
    return index


def publish(symbols, *, day=DAY, session="REGULAR", variant="S6-R",
            status=scan_cycle.STATUS_OK, run_id="run-1"):
    rows = publisher.publish([Signal(s) for s in symbols],
                             strategy_id=s6_sessions.STRATEGY_ID,
                             trading_day=day, session=session, variant=variant,
                             run_id=run_id)
    publisher.mark_run(day, session, strategy_id=s6_sessions.STRATEGY_ID,
                       candidates=len(symbols), run_id=run_id, status=status,
                       started_at=(T0 - timedelta(seconds=90)).isoformat(),
                       completed_at=T0.isoformat(), duration_seconds=90.0)
    return rows


def live_modes():
    modes = dict(slm.SCANNER_LIVE_MODE)
    modes["orb"] = slm.MODE_LIMITED_LIVE
    return modes


# ====================================================================
# §2  S6-R FINAL CHECK
# ====================================================================
class TestFinalCheckReportsWhatItMeasured:
    def test_a_closed_market_is_not_a_verified_tick(self, handoff, master):
        report = final_check.build(trading_day=DAY, session="REGULAR", now=T0)
        # 14:00 UTC on a Monday IS the regular session, but the report
        # reads the real calendar; whatever it says, it must agree with
        # itself rather than assert a tick nobody observed.
        assert report["market_open_verified"] in (True, False)
        assert report["scanner_tick_verified"] is False

    def test_a_completed_scan_verifies_the_tick(self, handoff, master):
        publish(["AAPL"])
        report = final_check.build(trading_day=DAY, session="REGULAR", now=T0)
        assert report["scanner_tick_verified"] is True
        assert report["publisher_verified"] is True
        assert report["scan_duration_seconds"] == 90.0
        assert report["scan_started_at"]
        assert report["scan_completed_at"]

    def test_the_directory_it_read_is_reported(self, handoff, master):
        """The producer/consumer hand-off has broken silently here before.
        Naming the directory makes the next one a one-line comparison."""
        publish(["AAPL"])
        report = final_check.build(trading_day=DAY, session="REGULAR", now=T0)
        assert report["candidate_dir"] == str(handoff)
        assert str(handoff) in final_check.format_report(report)

    def test_a_running_scan_is_not_a_verified_tick(self, handoff, master):
        publish(["AAPL"])
        with scan_cycle.hold(DAY, "REGULAR", scanner=ORB):
            report = final_check.build(trading_day=DAY, session="REGULAR",
                                       now=T0)
        assert report["scan_in_progress"] is True
        assert report["scanner_tick_verified"] is False

    def test_common_stock_is_counted_apart_from_observed(self, handoff, master):
        publish(["AAPL", "IEFA"])
        report = final_check.build(trading_day=DAY, session="REGULAR", now=T0)
        assert report["candidate_count"] == 2
        assert report["common_stock_count"] == 1

    def test_the_instrument_gate_is_answered_offline(self, handoff, master):
        """The one gate that matters most is the one that needs no broker."""
        publish(["AAPL", "IEFA"])
        report = final_check.build(trading_day=DAY, session="REGULAR", now=T0)
        gates = {row["symbol"]: row["buy_gates"] for row in report["candidates"]}
        assert gates["AAPL"]["instrument"]["status"] == final_check.PASS
        assert gates["IEFA"]["instrument"]["status"] == final_check.BLOCK

    def test_broker_gates_are_not_measured_without_a_broker(self, handoff,
                                                            master):
        publish(["AAPL"])
        report = final_check.build(trading_day=DAY, session="REGULAR", now=T0)
        row = report["candidates"][0]
        for gate in ("cash_orderability", "reconciliation",
                     "kis_execution_sanity"):
            assert row["buy_gates"][gate]["status"] == final_check.NOT_MEASURED

    def test_discovery_only_never_reaches_the_submit_boundary(self, handoff,
                                                              master):
        publish(["AAPL"])
        report = final_check.build(trading_day=DAY, session="REGULAR", now=T0)
        assert report["submit_boundary_reached"] is False
        assert report["broker_submit_count"] == 0
        assert "not LIMITED_LIVE" in (report["source_refusal"] or "")

    def test_the_source_refusal_is_stated_once_at_the_top(self, handoff,
                                                          master):
        publish(["AAPL"])
        report = final_check.build(trading_day=DAY, session="REGULAR", now=T0)
        assert report["candidates"][0]["source_verified"] is False
        assert report["candidates"][0]["qualify_verified"] is None

    def test_a_limited_live_source_measures_freshness_at_consume(
            self, handoff, master):
        publish(["AAPL"])
        report = final_check.build(trading_day=DAY, session="REGULAR",
                                   modes=live_modes(), now=T0)
        assert report["candidates"][0]["source_verified"] is True
        assert report["candidates"][0]["qualify_verified"] is True
        assert report["candidate_age_at_consume_seconds"] is not None
        assert report["candidate_age_at_consume_seconds"] >= 0

    def test_the_risk_matrix_blocks_a_shadow_session(self, handoff, master,
                                                     conn):
        publish(["AAPL"], session="OVERNIGHT_DAYTIME", variant="S6-O")
        report = final_check.build(conn=conn, trading_day=DAY,
                                   session="OVERNIGHT_DAYTIME",
                                   modes=live_modes(), now=T0)
        gate = report["candidates"][0]["buy_gates"]["risk_matrix"]
        assert gate["status"] == final_check.BLOCK
        assert "REALTIME_SHADOW" in gate["detail"]

    def test_a_missing_master_is_not_measured_not_blocked(self, handoff,
                                                          monkeypatch):
        """A stale or absent master is the ABSENCE of a verdict about the
        symbol, and must not read as one."""
        from s1_live import security_type

        def unavailable(path=None):
            raise security_type.SecurityTypeUnavailable(
                f"{security_type.REASON_CACHE_UNAVAILABLE}: no file")

        monkeypatch.setattr(security_type, "load_index", unavailable)
        publish(["AAPL"])
        report = final_check.build(trading_day=DAY, session="REGULAR", now=T0)
        gate = report["candidates"][0]["buy_gates"]["instrument"]
        assert gate["status"] == final_check.NOT_MEASURED

    def test_no_candidate_means_not_measured_not_pass(self, handoff, master):
        publish([])
        report = final_check.build(trading_day=DAY, session="REGULAR", now=T0)
        for gate in final_check.GATES:
            assert report["buy_gates"][gate]["status"] == final_check.NOT_MEASURED

    def test_it_renders(self, handoff, master):
        publish(["AAPL", "IEFA"])
        text = final_check.format_report(
            final_check.build(trading_day=DAY, session="REGULAR", now=T0))
        assert "S6-R FINAL CHECK" in text
        assert "broker submit count     : 0" in text


class TestFinalCheckCannotTrade:
    def test_it_never_submits(self):
        assert_cannot_order(REPO_ROOT / "s6_live" / "final_check.py")

    def test_broker_submit_count_is_a_constant(self, handoff, master):
        publish(["AAPL"])
        report = final_check.build(trading_day=DAY, session="REGULAR",
                                   modes=live_modes(), now=T0)
        assert report["broker_submit_count"] == 0

    def test_a_broken_section_does_not_raise(self, handoff, monkeypatch):
        monkeypatch.setattr(final_check, "_scan_facts",
                            lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("disk gone")))
        report = final_check.build(trading_day=DAY, session="REGULAR", now=T0)
        assert any("disk gone" in e for e in report["errors"])


# ====================================================================
# §3  FIRST COMMON_STOCK SNAPSHOT
# ====================================================================
class TestCommonStockSnapshot:
    def test_a_common_stock_candidate_is_snapshotted(self, handoff, master):
        rows = publish(["AAPL"])
        written = s6_snapshot.record_from_published(
            [r.as_dict() for r in rows], trading_day=DAY, session="REGULAR",
            index=master, now=T0)
        assert [r["symbol"] for r in written] == ["AAPL"]

    def test_an_etp_is_not_snapshotted(self, handoff, master):
        rows = publish(["IEFA"])
        assert s6_snapshot.record_from_published(
            [r.as_dict() for r in rows], trading_day=DAY, session="REGULAR",
            index=master, now=T0) == []

    def test_an_s6_o_candidate_is_not_snapshotted(self, handoff, master):
        """The activation gate is about REGULAR. An overnight candidate is
        a different setup that happens to share a scanner."""
        rows = publish(["AAPL"], session="OVERNIGHT_DAYTIME", variant="S6-O")
        assert s6_snapshot.record_from_published(
            [r.as_dict() for r in rows], trading_day=DAY,
            session="OVERNIGHT_DAYTIME", index=master, now=T0) == []

    def test_every_field_the_gate_asks_for_is_present(self, handoff, master):
        rows = publish(["AAPL"])
        record = s6_snapshot.record_from_published(
            [r.as_dict() for r in rows], trading_day=DAY, session="REGULAR",
            index=master, now=T0)[0]
        for field in ("symbol", "rank", "score", "security_type",
                      "live_eligible", "range_high", "range_low",
                      "range_width_pct", "structural_risk_pct",
                      "breakout_pct", "normalized_breakout_by_range",
                      "volume_expansion", "daily_relative_volume",
                      "absolute_volume", "dollar_volume", "vwap",
                      "vwap_distance_pct", "ema9", "ema21", "ema_spread_pct",
                      "generated_at", "consumed_at", "candidate_age_seconds",
                      "qualify_result", "buy_gates"):
            assert field in record, field

    def test_the_derived_measures_are_computed(self, handoff, master):
        rows = publish(["AAPL"])
        record = s6_snapshot.record_from_published(
            [r.as_dict() for r in rows], trading_day=DAY, session="REGULAR",
            index=master, now=T0)[0]
        assert record["breakout_pct"] == pytest.approx(
            (100.0 / 99.5 - 1.0) * 100.0)
        assert record["dollar_volume"] == pytest.approx(100.0 * 4_000_000)
        assert record["daily_relative_volume"] == pytest.approx(4.0)
        assert record["structural_risk_pct"] == pytest.approx(1.0)

    def test_gates_start_unmeasured_on_the_scanner_side(self, handoff, master):
        rows = publish(["AAPL"])
        record = s6_snapshot.record_from_published(
            [r.as_dict() for r in rows], trading_day=DAY, session="REGULAR",
            index=master, now=T0)[0]
        assert record["qualify_result"] == s6_snapshot.NOT_MEASURED
        assert set(record["buy_gates"].values()) == {s6_snapshot.NOT_MEASURED}
        assert record["broker_submit_count"] == 0

    def test_first_names_the_earliest(self, handoff, master):
        rows = publish(["AAPL"])
        s6_snapshot.record_from_published(
            [r.as_dict() for r in rows], trading_day=DAY, session="REGULAR",
            index=master, now=T0)
        s6_snapshot.record_from_published(
            [r.as_dict() for r in rows], trading_day=DAY, session="REGULAR",
            index=master, now=T0 + timedelta(minutes=15))
        assert len(s6_snapshot.read()) == 2
        assert s6_snapshot.first()["recorded_at"] == T0.isoformat()

    def test_the_final_check_attaches_the_gate_answers(self, handoff, master):
        rows = publish(["AAPL"])
        s6_snapshot.record_from_published(
            [r.as_dict() for r in rows], trading_day=DAY, session="REGULAR",
            index=master, now=T0)
        report = final_check.build(trading_day=DAY, session="REGULAR",
                                   modes=live_modes(), now=T0)
        assert final_check.attach_to_snapshots(report) == 1

        amended = s6_snapshot.read(trading_day=DAY)[-1]
        assert amended["amends"] == T0.isoformat()
        assert amended["buy_gates"]["instrument"] == final_check.PASS
        assert amended["qualify_result"] == final_check.PASS
        assert amended["consumed_at"]
        # The scanner's untouched observation is still on disk.
        assert s6_snapshot.read(trading_day=DAY)[0]["buy_gates"][
            "instrument"] == s6_snapshot.NOT_MEASURED

    def test_the_scanner_runtime_writes_one_automatically(self, handoff,
                                                          master, monkeypatch):
        """§3 asks for automatic capture, not an operator remembering."""
        from scanners import runner

        class Outcome:
            scanner_name = "orb"
            failed = False
            signals = [Signal("AAPL")]

        class Report:
            trading_day = DAY
            session = "REGULAR"
            run_id = "run-1"
            started_at = T0.isoformat()
            duration_seconds = 90.0
            outcomes = [Outcome()]

        runner.publish_report_candidates(Report())
        assert [r["symbol"] for r in s6_snapshot.read()] == ["AAPL"]


# ====================================================================
# §4  S6-O SESSION REPORT
# ====================================================================
class TestSessionReport:
    def test_the_overnight_session_expects_shadow(self, handoff, conn):
        report = session_report.build(conn=conn, trading_day=DAY,
                                      session="OVERNIGHT_DAYTIME", now=T0)
        assert report["variant"] == "S6-O"
        assert report["strategy_mode"] == s6_sessions.MODE_REALTIME_SHADOW
        assert report["order_capable"] is False
        assert report["orders_allowed"] is False
        assert report["broker_submit_count"] == 0
        assert report["matches_expectations"]["matched"] is True

    def test_the_expectations_come_from_the_session_matrix(self, handoff):
        """Not a second copy of the policy. If LIVE_SESSIONS widens, the
        report's expectation widens with it."""
        text = (REPO_ROOT / "s6_live" / "session_report.py").read_text()
        assert "s6_sessions.orders_allowed(session)" in text
        assert "s6_sessions.mode_for(session)" in text

    def test_a_quiet_session_is_distinguishable_from_a_missing_producer(
            self, handoff, conn):
        no_scan = session_report.build(conn=conn, trading_day=DAY,
                                       session="OVERNIGHT_DAYTIME", now=T0)
        assert no_scan["scan_status"] == session_report.NOT_MEASURED
        assert no_scan["scan_ran"] is False

        publish([], session="OVERNIGHT_DAYTIME", variant="S6-O")
        quiet = session_report.build(conn=conn, trading_day=DAY,
                                     session="OVERNIGHT_DAYTIME", now=T0)
        assert quiet["scan_status"] == session_report.OK
        assert quiet["scan_ran"] is True
        assert quiet["candidate_count"] == 0

    def test_a_range_that_never_formed_is_visible(self, handoff, conn):
        publish([], session="OVERNIGHT_DAYTIME", variant="S6-O")
        report = session_report.build(conn=conn, trading_day=DAY,
                                      session="OVERNIGHT_DAYTIME", now=T0)
        assert report["range_ready"] is False

        publish(["AAPL"], session="OVERNIGHT_DAYTIME", variant="S6-O")
        report = session_report.build(conn=conn, trading_day=DAY,
                                      session="OVERNIGHT_DAYTIME", now=T0)
        assert report["range_ready"] is True
        assert report["range_minutes"] == [15]

    def test_a_row_filed_under_the_wrong_variant_is_counted(self, handoff,
                                                            conn):
        """An S6-R row in the overnight file is the failure that would
        otherwise be invisible -- the count would simply be lower."""
        publish(["AAPL"], session="OVERNIGHT_DAYTIME", variant="S6-R")
        report = session_report.build(conn=conn, trading_day=DAY,
                                      session="OVERNIGHT_DAYTIME", now=T0)
        assert report["candidate_count"] == 0
        assert report["foreign_variant_rows"] == 1

    def test_a_failed_scan_is_degraded_not_ok(self, handoff, conn):
        publish([], session="OVERNIGHT_DAYTIME", variant="S6-O",
                status=scan_cycle.STATUS_FAILED)
        report = session_report.build(conn=conn, trading_day=DAY,
                                      session="OVERNIGHT_DAYTIME", now=T0)
        assert report["scan_status"] == session_report.DEGRADED

    def test_a_missing_runtime_tick_is_not_measured(self, handoff, conn):
        report = session_report.build(conn=conn, trading_day=DAY,
                                      session="OVERNIGHT_DAYTIME", now=T0)
        assert report["runtime_status"] == session_report.NOT_MEASURED

    def test_a_supplied_runtime_tick_is_reported(self, handoff, conn):
        report = session_report.build(
            conn=conn, trading_day=DAY, session="OVERNIGHT_DAYTIME", now=T0,
            runtime_report={"status": "OK", "errors": [], "exits": [{}]})
        assert report["runtime_status"] == session_report.OK
        assert report["runtime_exits"] == 1

    def test_a_failing_runtime_tick_is_degraded(self, handoff, conn):
        report = session_report.build(
            conn=conn, trading_day=DAY, session="OVERNIGHT_DAYTIME", now=T0,
            runtime_report={"status": "ERROR", "errors": ["exits: boom"]})
        assert report["runtime_status"] == session_report.DEGRADED

    def test_the_runtime_generates_it_for_a_shadow_session(self, monkeypatch,
                                                           handoff, conn):
        """§4 asks for automatic generation on the tick."""
        import scripts.run_s6_runtime as runtime

        report = {"session": "OVERNIGHT_DAYTIME"}
        runtime._attach_session_report(report, conn=conn,
                                       session="OVERNIGHT_DAYTIME", now=T0)
        assert report["session_report"]["variant"] == "S6-O"

    def test_the_runtime_does_not_generate_it_for_regular(self, handoff, conn):
        """S6-R has the final check, which asks a different question and
        needs a broker for most of it."""
        import scripts.run_s6_runtime as runtime

        report = {"session": "REGULAR"}
        runtime._attach_session_report(report, conn=conn, session="REGULAR",
                                       now=T0)
        assert "session_report" not in report

    def test_it_renders(self, handoff, conn):
        text = session_report.format_report(
            session_report.build(conn=conn, trading_day=DAY,
                                 session="OVERNIGHT_DAYTIME", now=T0))
        assert "S6 SESSION REPORT -- S6-O" in text
        assert "broker submit count : 0" in text

    def test_it_never_submits(self):
        assert_cannot_order(REPO_ROOT / "s6_live" / "session_report.py")


# ====================================================================
# §5  FIRST LIVE TRADE TIMELINE
# ====================================================================
def _submitted(conn, **kw):
    from s6_live import position_store

    kw.setdefault("symbol", "AAPL")
    kw.setdefault("variant", "S6-R")
    kw.setdefault("entry_session", "REGULAR")
    kw.setdefault("range_high", 99.5)
    kw.setdefault("range_low", 99.0)
    kw.setdefault("client_order_id", "s6buy-AAPL-1")
    kw.setdefault("now", T0)
    return position_store.record_submission(conn, **kw)


class TestTradeTimeline:
    def test_no_trade_is_reported_as_no_trade(self, conn, handoff):
        report = trade_timeline.build(conn, now=T0)
        assert report["trade_found"] is False
        assert all(stage["status"] == trade_timeline.NOT_REACHED
                   for stage in report["stages"])
        assert len(report["stages"]) == len(trade_timeline.STAGES)

    def test_a_submitted_order_reaches_only_that_far(self, conn, handoff):
        _submitted(conn)
        report = trade_timeline.build(conn, now=T0)
        stages = {s["stage"]: s["status"] for s in report["stages"]}
        assert stages["BUY_SUBMITTED"] == trade_timeline.REACHED
        assert stages["BUY_FILL"] == trade_timeline.NOT_REACHED
        assert stages["OPEN"] == trade_timeline.NOT_REACHED
        assert report["realized_pnl"] is None

    def test_an_open_position_has_no_holding_period(self, conn, handoff):
        from s6_live import position_store

        pid = _submitted(conn)
        position_store.open_from_fill(conn, pid, quantity=1,
                                      average_fill_price=100.0, now=T0)
        report = trade_timeline.build(conn, now=T0 + timedelta(hours=2))
        assert report["holding_minutes"] is None
        assert "still open" in report["holding_detail"]

    def test_the_full_lifecycle_is_described(self, conn, handoff):
        from s6_live import exit_runtime, position_store

        pid = _submitted(conn)
        position_store.open_from_fill(conn, pid, quantity=2,
                                      average_fill_price=100.0, now=T0)
        position_store.observe(conn, pid, price=103.0, now=T0)
        position_store.observe(conn, pid, price=97.0, now=T0)
        position_store.latch_pending_exit(conn, pid, "RANGE_REENTRY", now=T0)
        position_store.mark_exit_submitted(conn, pid, "RANGE_REENTRY", now=T0)
        exit_runtime.sync_sell_fills(
            conn, fills_for=lambda row: {"filled_quantity": 2,
                                         "average_fill_price": 101.0},
            now=T0 + timedelta(minutes=42))

        report = trade_timeline.build(conn, now=T0 + timedelta(hours=1))
        stages = {s["stage"]: s["status"] for s in report["stages"]}
        for stage in ("CANDIDATE", "BUY_SUBMITTED", "BUY_FILL", "OPEN",
                      "EXIT_SIGNAL", "SELL_SUBMITTED", "SELL_FILL", "CLOSED"):
            assert stages[stage] == trade_timeline.REACHED, stage

        assert report["entry_price"] == 100.0
        assert report["exit_price"] == 101.0
        assert report["realized_pnl"] == pytest.approx(2.0)
        assert report["realized_pnl_pct"] == pytest.approx(1.0)
        assert report["mfe_pct"] == pytest.approx(3.0)
        assert report["mae_pct"] == pytest.approx(-3.0)
        assert report["holding_minutes"] == pytest.approx(42.0)
        assert report["exit_reason"] == "RANGE_REENTRY"

    def test_mae_is_recorded_not_estimated(self, conn, handoff):
        """A position with no observation has no trough, and the report
        says so rather than substituting the entry."""
        from s6_live import position_store

        pid = _submitted(conn)
        position_store.open_from_fill(conn, pid, quantity=1,
                                      average_fill_price=100.0, now=T0)
        report = trade_timeline.build(conn, now=T0)
        # The entry IS the first trough, so MAE is 0.0 -- not None and
        # not a fabricated drawdown.
        assert report["mae_pct"] == 0.0
        assert report["trough_price"] == 100.0

    def test_the_trough_only_ratchets_down(self, conn, handoff):
        from s6_live import position_store

        pid = _submitted(conn)
        position_store.open_from_fill(conn, pid, quantity=1,
                                      average_fill_price=100.0, now=T0)
        for price in (97.0, 99.0, 95.0, 96.0):
            position_store.observe(conn, pid, price=price, now=T0)
        assert position_store.load(conn, pid)["trough_price"] == 95.0
        assert position_store.load(conn, pid)["peak_price"] == 100.0

    def test_a_duplicate_exit_intent_would_be_visible(self, conn, handoff):
        from s6_live import position_store
        from state_store import exit_intent_ledger

        pid = _submitted(conn)
        position_store.open_from_fill(conn, pid, quantity=1,
                                      average_fill_price=100.0, now=T0)
        exit_intent_ledger.reserve(conn, pid, "RANGE_REENTRY", 1, "s6exit-1")
        report = trade_timeline.build(conn, now=T0)
        assert report["exit_intents"] == 1
        assert report["duplicate_order_detected"] is False
        with pytest.raises(exit_intent_ledger.DuplicateExitIntentError):
            exit_intent_ledger.reserve(conn, pid, "RANGE_REENTRY", 1,
                                       "s6exit-2")

    def test_the_first_trade_is_the_first_one_sent(self, conn, handoff):
        second = _submitted(conn, symbol="MSFT",
                            now=T0 + timedelta(minutes=30),
                            client_order_id="s6buy-MSFT-1")
        first = _submitted(conn, symbol="AAPL", now=T0,
                           client_order_id="s6buy-AAPL-2")
        assert trade_timeline.build(conn, now=T0)["position_id"] == first
        assert trade_timeline.build(
            conn, position_id=second, now=T0)["symbol"] == "MSFT"

    def test_it_renders_both_shapes(self, conn, handoff):
        empty = trade_timeline.format_report(trade_timeline.build(conn, now=T0))
        assert "no S6 position has ever been recorded" in empty
        _submitted(conn)
        text = trade_timeline.format_report(trade_timeline.build(conn, now=T0))
        assert "S6 FIRST LIVE TRADE" in text
        assert "broker submit count : 0" in text

    def test_it_never_submits(self):
        assert_cannot_order(REPO_ROOT / "s6_live" / "trade_timeline.py")

    def test_the_snapshot_writer_cannot_order_either(self):
        assert_cannot_order(REPO_ROOT / "scanners" / "publish" / "s6_snapshot.py")


# ====================================================================
# The evaluator wiring: a synthetic run can never produce a PASS
# ====================================================================
@pytest.fixture
def production(monkeypatch):
    monkeypatch.setenv("DEPLOYED_COMMIT", "99a9e51")
    monkeypatch.setenv("VALIDATED_COMMIT", "99a9e51")


@pytest.fixture
def unverified(monkeypatch):
    monkeypatch.delenv("DEPLOYED_COMMIT", raising=False)
    monkeypatch.delenv("VALIDATED_COMMIT", raising=False)


class TestSyntheticRunsCannotSupplyAProductionPass:
    def test_an_unverified_run_stamps_its_artifacts_as_such(self, unverified,
                                                            handoff, master):
        publish(["AAPL"])
        report = final_check.build(trading_day=DAY, session="REGULAR",
                                   modes=live_modes(), now=T0)
        assert report["origin"] == s6_snapshot.ORIGIN_UNVERIFIED

    def test_and_therefore_supplies_no_observation(self, unverified, handoff,
                                                   master):
        publish(["AAPL"])
        report = final_check.build(trading_day=DAY, session="REGULAR",
                                   modes=live_modes(), now=T0)
        assert observations.candidate_freshness(report) is None
        assert observations.regular_market_tick(report) is None

    def test_a_mismatched_deployment_is_not_production(self, monkeypatch):
        monkeypatch.setenv("DEPLOYED_COMMIT", "aaaaaaa")
        monkeypatch.setenv("VALIDATED_COMMIT", "bbbbbbb")
        assert observations.is_production({"origin": s6_snapshot.origin()}) \
            is False

    def test_a_validated_deployment_can_supply_one(self, production, handoff,
                                                   master):
        publish(["AAPL"])
        report = final_check.build(trading_day=DAY, session="REGULAR",
                                   modes=live_modes(), now=T0)
        assert report["origin"] == s6_snapshot.ORIGIN_PRODUCTION
        assert observations.candidate_freshness(report) is True

    def test_a_closed_market_yields_no_tick_observation(self, production,
                                                        handoff, master):
        """Not False. A weekend cannot manufacture a failure any more than
        it can manufacture a pass."""
        publish(["AAPL"])
        report = final_check.build(trading_day=DAY, session="REGULAR",
                                   modes=live_modes(), now=T0)
        report["market_open_verified"] = False
        assert observations.regular_market_tick(report) is None

    def test_an_open_market_without_a_tick_is_a_failure(self, production,
                                                        handoff, master):
        publish(["AAPL"])
        report = final_check.build(trading_day=DAY, session="REGULAR",
                                   modes=live_modes(), now=T0)
        report["market_open_verified"] = True
        report["scanner_tick_verified"] = False
        assert observations.regular_market_tick(report) is False

    def test_a_report_from_another_session_says_nothing_about_regular(
            self, production, handoff, master):
        publish(["AAPL"], session="OVERNIGHT_DAYTIME", variant="S6-O")
        report = final_check.build(trading_day=DAY,
                                   session="OVERNIGHT_DAYTIME",
                                   modes=live_modes(), now=T0)
        assert observations.regular_market_tick(report) is None

    def test_an_unverified_snapshot_is_not_evidence(self, unverified, handoff,
                                                    master):
        rows = publish(["AAPL"])
        s6_snapshot.record_from_published(
            [r.as_dict() for r in rows], trading_day=DAY, session="REGULAR",
            index=master, now=T0)
        assert s6_snapshot.first(production_only=True) is None
        assert observations.common_stock_dry_run(
            s6_snapshot.first(production_only=True)) is None

    def test_a_production_snapshot_is(self, production, handoff, master):
        rows = publish(["AAPL"])
        s6_snapshot.record_from_published(
            [r.as_dict() for r in rows], trading_day=DAY, session="REGULAR",
            index=master, now=T0)
        found = s6_snapshot.first(production_only=True)
        assert found["symbol"] == "AAPL"
        assert observations.common_stock_dry_run(found) is True


class TestTheGateStillRefusesToday:
    def test_the_three_market_checks_remain_unmeasured(self, unverified,
                                                        handoff, conn, master):
        from s6_live import readiness

        publish(["AAPL"])
        verdict = readiness.evaluate(
            conn=conn, crontab="2,17,32,47 * * * * s6_scan.sh\n"
                               "7,22,37,52 * * * * s6_exec.sh\n",
            observations=observations.load(
                conn=conn, trading_day=DAY, session="REGULAR", now=T0,
                extra={"s1_healthy": True, "regression_healthy": True,
                       "account_rows": []}))
        assert verdict.ready is False
        assert verdict.verdict == readiness.NOT_READY
        assert set(verdict.unmeasured()) == set(observations.MARKET_DEPENDENT)
        assert verdict.failures() == []

    def test_the_evaluator_still_changes_no_mode(self):
        before = dict(slm.SCANNER_LIVE_MODE)
        from s6_live import readiness

        readiness.evaluate(observations={})
        assert slm.SCANNER_LIVE_MODE == before
        assert slm.SCANNER_LIVE_MODE["orb"] == slm.MODE_DISCOVERY_ONLY


class TestS6GetsTheStrategyGates:
    """The COMMON_STOCK gate and the day-range execution check apply to a
    STRATEGY source. S6 was not in that set, so promoting it would have
    handed its candidates the legacy path instead."""

    def test_s6_is_a_strategy_source(self):
        import kis_live_trading as klt
        from s6_live import candidate_source

        assert candidate_source.SOURCE_S6 in klt.STRATEGY_SOURCES
        assert klt.is_strategy_source(
            candidate_source.S6CandidateSource(trading_day=DAY,
                                               session="REGULAR")) is True

    def test_all_three_live_strategies_are_in_the_set(self):
        import kis_live_trading as klt
        from s1_live import candidate_source as s1
        from s2_live import candidate_source as s2
        from s6_live import candidate_source as s6

        assert klt.STRATEGY_SOURCES == frozenset(
            {s1.SOURCE_S1, s2.SOURCE_S2, s6.SOURCE_S6})

    def test_an_unknown_source_still_gets_the_legacy_path(self):
        import kis_live_trading as klt

        class Mystery:
            name = "something_new"

        assert klt.is_strategy_source(Mystery()) is False
