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
            status=scan_cycle.STATUS_OK, run_id="run-1", generated_at=None):
    """Publish rows stamped relative to T0, never to the wall clock.

    `generated_at` used to be left to the publisher, which stamps
    `now()`. Every report here is then read with a frozen `now=T0`
    (14:00 UTC on DAY), so the candidate's age is `T0 - wall clock` --
    positive all morning and NEGATIVE from 14:00 UTC onwards on the one
    day DAY names. A freshness assertion that inverts partway through
    its own trading day is measuring the test runner's clock, not the
    handoff.
    """
    stamp = generated_at or (T0 - timedelta(seconds=90)).isoformat()
    rows = publisher.publish([Signal(s) for s in symbols],
                             strategy_id=s6_sessions.STRATEGY_ID,
                             trading_day=day, session=session, variant=variant,
                             run_id=run_id, generated_at=stamp)
    publisher.mark_run(day, session, strategy_id=s6_sessions.STRATEGY_ID,
                       candidates=len(symbols), run_id=run_id, status=status,
                       started_at=(T0 - timedelta(seconds=90)).isoformat(),
                       completed_at=T0.isoformat(), duration_seconds=90.0)
    return rows


def live_modes():
    modes = dict(slm.SCANNER_LIVE_MODE)
    modes["orb"] = slm.MODE_LIMITED_LIVE
    return modes


def discovery_modes():
    """S6 stood down, whatever the production table currently says.

    The DISCOVERY_ONLY half of these reports used to come free from the
    production default. `orb` is promoted now, so a test whose subject
    is "what an unpromoted strategy reports" has to say so explicitly --
    otherwise it is asserting today's posture rather than the reporting
    rule it was written for.
    """
    modes = dict(slm.SCANNER_LIVE_MODE)
    modes["orb"] = slm.MODE_DISCOVERY_ONLY
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
        report = final_check.build(trading_day=DAY, session="REGULAR",
                                   modes=discovery_modes(), now=T0)
        assert report["submit_boundary_reached"] is False
        assert report["broker_submit_count"] == 0
        assert "not LIMITED_LIVE" in (report["source_refusal"] or "")

    def test_the_source_refusal_is_stated_once_at_the_top(self, handoff,
                                                          master):
        publish(["AAPL"])
        report = final_check.build(trading_day=DAY, session="REGULAR",
                                   modes=discovery_modes(), now=T0)
        assert report["candidates"][0]["source_verified"] is False
        assert "not LIMITED_LIVE" in (report["source_refusal"] or "")

    def test_qualification_is_measured_even_when_the_source_refuses(
            self, handoff, master):
        """Two different facts, reported separately.

        `source_verified` is the LIVE consumer's answer and stays False
        while S6 is DISCOVERY_ONLY. `qualify_verified` is a pure function
        of the published row -- no broker, no mode -- so it says what the
        shared cycle WOULD decide. Tying the second to the first made the
        freshness observation circular: it gated the promotion that would
        have made it measurable.
        """
        publish(["AAPL"])
        report = final_check.build(trading_day=DAY, session="REGULAR",
                                   modes=discovery_modes(), now=T0)
        row = report["candidates"][0]
        assert row["source_verified"] is False
        assert row["qualify_verified"] is True

    def test_freshness_is_measured_at_read_without_order_permission(
            self, handoff, master):
        publish(["AAPL"])
        report = final_check.build(trading_day=DAY, session="REGULAR",
                                   modes=discovery_modes(), now=T0)
        # Measured: the observation path read real rows at a real moment.
        assert report["candidate_generated_at"]
        assert report["candidate_read_at"] == T0.isoformat()
        assert report["candidate_age_at_read_seconds"] is not None
        assert report["candidate_age_at_read_seconds"] >= 0
        # The LIVE consumer still refused, and says so separately.
        assert report["candidate_consumed_at"] is None
        assert report["candidate_age_at_consume_seconds"] is None

    def test_the_observation_path_still_cannot_order(self, handoff, master):
        publish(["AAPL"])
        report = final_check.build(trading_day=DAY, session="REGULAR",
                                   modes=discovery_modes(), now=T0)
        assert report["broker_submit_count"] == 0
        assert report["submit_boundary_reached"] is False
        assert report["strategy_live_mode"] == slm.MODE_DISCOVERY_ONLY

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
        # PREMARKET is the shadow exemplar. OVERNIGHT_DAYTIME has a
        # specified KIS order route and is session-permitted now;
        # PREMARKET has none and cannot become one, so it is the session
        # that still demonstrates a shadow refusal.
        publish(["AAPL"], session="PREMARKET", variant="S6-P")
        report = final_check.build(conn=conn, trading_day=DAY,
                                   session="PREMARKET",
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
    def test_an_unrouted_session_expects_shadow(self, handoff, conn):
        report = session_report.build(conn=conn, trading_day=DAY,
                                      session="PREMARKET",
                                      modes=discovery_modes(), now=T0)
        assert report["variant"] == "S6-P"
        assert report["session_mode"] == s6_sessions.MODE_REALTIME_SHADOW
        assert report["strategy_live_mode"] == slm.MODE_DISCOVERY_ONLY
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

        # The shadow report is for sessions that CANNOT order. S6-O now
        # can, so it takes the final-check path like S6-R; PREMARKET is
        # the session that still cannot and still gets this.
        report = {"session": "PREMARKET"}
        runtime._attach_session_report(report, conn=conn,
                                       session="PREMARKET", now=T0)
        assert report["session_report"]["variant"] == "S6-P"

    def test_the_runtime_does_not_generate_it_for_an_ordering_session(
            self, handoff, conn):
        """An ordering session has the final check, which asks a
        different question and needs a broker for most of it. That is
        now true of OVERNIGHT_DAYTIME as well as REGULAR."""
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
        # Immutability is the invariant; the value is not. Asserting the
        # literal made this a second copy of the posture.
        assert slm.SCANNER_LIVE_MODE == before


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


class TestCapabilityIsNotPromotion:
    """A session that WOULD permit orders and a strategy that MAY place
    them are different facts. The final check printed the first under the
    name of the second, so a REGULAR report read `LIMITED_LIVE` while S6
    was DISCOVERY_ONLY -- on the one page an operator reads immediately
    before deciding to promote it."""

    def test_the_final_check_separates_them(self, handoff, master):
        publish(["AAPL"])
        report = final_check.build(trading_day=DAY, session="REGULAR",
                                   modes=discovery_modes(), now=T0)
        assert report["session_mode"] == s6_sessions.MODE_LIMITED_LIVE
        assert report["strategy_live_mode"] == slm.MODE_DISCOVERY_ONLY
        assert report["order_capable"] is True
        # Capable session, unpromoted strategy -> no orders.
        assert report["orders_allowed"] is False

    def test_both_are_printed_with_what_they_mean(self, handoff, master):
        text = final_check.format_report(
            final_check.build(trading_day=DAY, session="REGULAR", now=T0))
        assert "what this SESSION permits" in text
        assert "whether S6 ITSELF may act" in text

    def test_a_promoted_strategy_says_so(self, handoff, master):
        report = final_check.build(trading_day=DAY, session="REGULAR",
                                   modes=live_modes(), now=T0)
        assert report["strategy_live_mode"] == slm.MODE_LIMITED_LIVE

    def test_the_session_report_separates_them_too(self, handoff, conn):
        # PREMARKET is the shadow exemplar now: OVERNIGHT_DAYTIME has a
        # specified KIS order route and is session-permitted, while
        # PREMARKET has none and cannot become one.
        report = session_report.build(conn=conn, trading_day=DAY,
                                      session="PREMARKET",
                                      modes=discovery_modes(), now=T0)
        assert report["session_mode"] == s6_sessions.MODE_REALTIME_SHADOW
        assert report["strategy_live_mode"] == slm.MODE_DISCOVERY_ONLY
        assert report["orders_allowed"] is False

    def test_orders_allowed_needs_both(self, handoff, master):
        """Neither alone is enough, in either direction."""
        capable_not_promoted = final_check.build(
            trading_day=DAY, session="REGULAR",
            modes=discovery_modes(), now=T0)
        promoted_not_capable = final_check.build(
            trading_day=DAY, session="PREMARKET", modes=live_modes(),
            now=T0)
        assert capable_not_promoted["orders_allowed"] is False
        assert promoted_not_capable["orders_allowed"] is False


# ====================================================================
# §1-§9  THE ACCOUNT READS ARE THE REPORT'S, NOT EACH SYMBOL'S
# ====================================================================
class CountingBroker:
    """A read-only broker that records how often each call was made.

    Enough of one to answer every gate, and nothing that could submit.
    """

    def __init__(self, *, orderable=1000.0, fail=(), positions=None,
                 open_orders=None):
        self.calls = {}
        self._orderable = orderable
        self._fail = set(fail)
        self._positions = positions if positions is not None else []
        self._open_orders = open_orders if open_orders is not None else []

    def _count(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1
        if name in self._fail:
            raise RuntimeError(f"{name} unavailable")

    def get_positions(self):
        self._count("get_positions")
        return list(self._positions)

    def get_open_orders(self):
        self._count("get_open_orders")
        return list(self._open_orders)

    def get_account_snapshot(self):
        self._count("get_account_snapshot")
        return {"cash": 1000.0}

    def get_orderable_usd(self, instrument, limit):
        self._count("get_orderable_usd")
        if callable(self._orderable):
            return self._orderable(instrument, limit)
        return self._orderable

    def get_price_detail(self, instrument):
        self._count("get_price_detail")
        return {"last": 100.0, "high": 101.0, "low": 99.0,
                "previous_close": 99.5}


def _many_rows(symbols, *, n=8, session="REGULAR", variant="S6-R"):
    """The real shape of the store: one append per scan, same symbols."""
    for i in range(n):
        publish(symbols, session=session, variant=variant,
                run_id=f"run-{i}")


class TestAccountReadsAreTakenOncePerReport:
    """Positions, open orders and the account snapshot do not vary by
    symbol, but they were read inside the candidate loop. Over 32 stored
    rows for two symbols that made KIS answer the same three questions
    again and again, each sweeping three exchanges -- and under that load
    the orderable-amount call came back with a body carrying no `output`,
    so `cash_orderability` reported NOT_MEASURED for a question it
    answers correctly in 0.2s on a cold call."""

    def test_account_level_reads_happen_at_most_once(self, handoff, master,
                                                     conn):
        _many_rows(["AAPL", "IEFA"], n=16)
        broker = CountingBroker()
        report = final_check.build(conn=conn, broker=broker, trading_day=DAY,
                                   session="REGULAR", now=T0)

        assert report["published_rows"] == 32
        assert broker.calls.get("get_positions", 0) == 1
        assert broker.calls.get("get_open_orders", 0) == 1
        assert broker.calls.get("get_account_snapshot", 0) == 1

    def test_symbol_level_reads_happen_once_per_distinct_symbol(
            self, handoff, master, conn):
        _many_rows(["AAPL", "IEFA"], n=16)
        broker = CountingBroker()
        final_check.build(conn=conn, broker=broker, trading_day=DAY,
                          session="REGULAR", now=T0)

        # Two distinct symbols across 32 rows: the per-row cache holds.
        assert broker.calls.get("get_orderable_usd", 0) <= 2
        assert broker.calls.get("get_price_detail", 0) <= 2

    def test_the_report_publishes_its_own_call_counts(self, handoff, master,
                                                      conn):
        _many_rows(["AAPL"], n=4)
        broker = CountingBroker()
        report = final_check.build(conn=conn, broker=broker, trading_day=DAY,
                                   session="REGULAR", now=T0)

        counts = report["broker_reads"]["calls"]
        assert counts["positions_calls"] == 1
        assert counts["open_orders_calls"] == 1
        assert counts["account_snapshot_calls"] == 1
        assert counts["orderable_usd_calls"] <= 1
        assert counts["price_detail_calls"] <= 1
        assert report["broker_reads"]["fetched_at"]

    def test_a_report_with_no_broker_makes_no_calls(self, handoff, master,
                                                    conn):
        publish(["AAPL"])
        report = final_check.build(conn=conn, trading_day=DAY,
                                   session="REGULAR", now=T0)
        assert report["broker_reads"]["calls"] == {
            "positions_calls": 0, "open_orders_calls": 0,
            "account_snapshot_calls": 0, "orderable_usd_calls": 0,
            "price_detail_calls": 0}


class TestOneFailedReadDoesNotSinkTheOthers:
    """§5. A snapshot is three independent reads. An unreadable open-order
    list says nothing about reconciliation, and neither says anything
    about whether KIS calls the symbol a common stock -- so one failure
    must cost exactly one gate."""

    def _gates(self, conn, broker):
        report = final_check.build(conn=conn, broker=broker, trading_day=DAY,
                                   session="REGULAR", now=T0)
        return report, report["candidates"][0]["buy_gates"]

    def test_a_failed_position_read_costs_only_reconciliation(
            self, handoff, master, conn):
        publish(["AAPL"])
        report, gates = self._gates(conn, CountingBroker(
            fail={"get_positions"}))

        assert gates["reconciliation"]["status"] == final_check.NOT_MEASURED
        assert gates["duplicate_protection"]["status"] == final_check.PASS
        assert gates["cash_orderability"]["status"] == final_check.PASS
        assert gates["instrument"]["status"] == final_check.PASS
        assert report["broker_reads"]["errors"].keys() == {"positions"}

    def test_a_failed_open_order_read_costs_only_duplicate_protection(
            self, handoff, master, conn):
        publish(["AAPL"])
        _report, gates = self._gates(conn, CountingBroker(
            fail={"get_open_orders"}))

        assert gates["duplicate_protection"]["status"] == final_check.NOT_MEASURED
        assert gates["reconciliation"]["status"] == final_check.PASS
        assert gates["cash_orderability"]["status"] == final_check.PASS

    def test_a_failed_account_read_costs_only_cash_orderability(
            self, handoff, master, conn):
        publish(["AAPL"])
        _report, gates = self._gates(conn, CountingBroker(
            fail={"get_account_snapshot"}))

        assert gates["cash_orderability"]["status"] == final_check.NOT_MEASURED
        assert gates["reconciliation"]["status"] == final_check.PASS
        assert gates["duplicate_protection"]["status"] == final_check.PASS

    def test_a_failed_account_read_is_never_a_pass(self, handoff, master,
                                                   conn):
        publish(["AAPL"])
        _report, gates = self._gates(conn, CountingBroker(
            fail={"get_account_snapshot"}))
        assert gates["cash_orderability"]["status"] != final_check.PASS
        assert final_check.ORDERABILITY_API_ERROR in \
            gates["cash_orderability"]["detail"]


class TestOrderableCashIsStillAskedPerSymbol:
    """§3/§4. The account snapshot is not orderable USD and cannot stand
    in for it: KIS answers the second from a separate read that takes the
    symbol, its venue and the limit price. Reusing the cached account
    figure would be an estimate wearing a measurement's name."""

    def test_the_orderable_read_still_happens(self, handoff, master, conn):
        publish(["AAPL"])
        broker = CountingBroker()
        final_check.build(conn=conn, broker=broker, trading_day=DAY,
                          session="REGULAR", now=T0)
        assert broker.calls.get("get_orderable_usd", 0) == 1

    def test_zero_orderable_is_a_block_not_an_absence(self, handoff, master,
                                                      conn):
        publish(["AAPL"])
        report = final_check.build(conn=conn, broker=CountingBroker(
            orderable=1.0), trading_day=DAY, session="REGULAR", now=T0)
        gate = report["candidates"][0]["buy_gates"]["cash_orderability"]
        assert gate["status"] == final_check.BLOCK
        assert final_check.ORDERABILITY_ZERO in gate["detail"]

    def test_an_unusable_body_is_not_measured_and_is_not_zero(
            self, handoff, master, conn):
        publish(["AAPL"])

        def unusable(instrument, limit):
            raise RuntimeError(
                "KIS orderable-amount response unusable (output_missing)")

        report = final_check.build(conn=conn, broker=CountingBroker(
            orderable=unusable), trading_day=DAY, session="REGULAR", now=T0)
        gate = report["candidates"][0]["buy_gates"]["cash_orderability"]
        assert gate["status"] == final_check.NOT_MEASURED
        assert "0 whole shares" not in gate["detail"]

    def test_none_is_not_converted_to_zero_usd(self, handoff, master, conn):
        publish(["AAPL"])
        report = final_check.build(conn=conn, broker=CountingBroker(
            orderable=lambda *_: None), trading_day=DAY, session="REGULAR",
            now=T0)
        gate = report["candidates"][0]["buy_gates"]["cash_orderability"]
        assert gate["status"] == final_check.NOT_MEASURED
        assert final_check.ORDERABILITY_PARSE_ERROR in gate["detail"]


class TestTheSnapshotCannotTrade:
    """§1. It holds three lists. There is no method on it that could
    place an order, and the module it lives in is already asserted
    against its own parsed call graph."""

    def test_it_exposes_only_reads(self):
        public = {n for n in dir(final_check.ReportBrokerSnapshot)
                  if not n.startswith("_")}
        assert public == {"count", "as_dict"}

    def test_the_report_still_submits_nothing(self, handoff, master, conn):
        _many_rows(["AAPL", "IEFA"], n=8)
        broker = CountingBroker()
        report = final_check.build(conn=conn, broker=broker, trading_day=DAY,
                                   session="REGULAR", now=T0)
        assert report["broker_submit_count"] == 0
        assert not any(name.startswith("submit") or name.startswith("place")
                       for name in broker.calls)


# ====================================================================
#  FUNDING IS A FACT ABOUT THE ACCOUNT, NOT AN ABSENCE OF MEASUREMENT
# ====================================================================
class TestNotEnoughCashIsABlockAndNotAnUnknown:
    """The live account holds 74.01 USD orderable. S6 buys whole shares
    only, one at a time, and fractional is OFF -- so a candidate priced
    above the orderable figure cannot be bought at all.

    That is a measured fact about the account and must read BLOCK /
    ORDERABILITY_ZERO. It must NOT read NOT_MEASURED, which would say the
    question went unanswered, and it must not read PASS. The opposite
    error matters just as much: a read that did not land is NOT_MEASURED
    and must never be dressed up as poverty, because
    `whole_shares_affordable` returns 0 for an unusable figure exactly as
    it does for a genuinely empty account.
    """

    #: The real production numbers, so this test fails if either the
    #: sizing rule or the classification drifts.
    ORDERABLE = 74.01
    UNAFFORDABLE = 309.49   # UNP, the S6-R candidate on 2026-08-24
    AFFORDABLE = 70.00

    def _gate(self, orderable, price):
        return final_check._cash_gate(
            CountingBroker(orderable=orderable), "AAPL", price,
            ReportBrokerSnapshotStub())

    def test_a_share_priced_above_the_cash_is_a_block(self):
        gate = self._gate(self.ORDERABLE, self.UNAFFORDABLE)
        assert gate["status"] == final_check.BLOCK
        assert final_check.ORDERABILITY_ZERO in gate["detail"]
        assert "74.01" in gate["detail"]

    def test_that_block_is_not_an_unknown(self):
        gate = self._gate(self.ORDERABLE, self.UNAFFORDABLE)
        assert gate["status"] != final_check.NOT_MEASURED
        assert gate["status"] != final_check.PASS

    def test_a_share_priced_within_the_cash_passes(self):
        gate = self._gate(self.ORDERABLE, self.AFFORDABLE)
        assert gate["status"] == final_check.PASS
        assert final_check.ORDERABILITY_OK in gate["detail"]
        assert "1 whole share" in gate["detail"]

    def test_the_boundary_is_one_whole_share_not_a_fraction(self):
        """At 74.02 the account affords 0.999... of a share. There is no
        fractional path to fall back to, so it is a block."""
        assert self._gate(self.ORDERABLE, 74.01)["status"] == final_check.PASS
        assert self._gate(self.ORDERABLE, 74.02)["status"] == final_check.BLOCK

    def test_the_count_is_a_floor_never_a_round(self):
        """floor(74.01/40) == 1, not 2."""
        gate = self._gate(self.ORDERABLE, 40.00)
        assert gate["status"] == final_check.PASS
        assert "1 whole share" in gate["detail"]

    @pytest.mark.parametrize("unusable", [
        None, float("nan"), float("inf"), -1.0, "74.01", True])
    def test_an_unusable_figure_is_not_measured_never_zero(self, unusable):
        gate = self._gate(lambda *_: unusable, self.UNAFFORDABLE)
        assert gate["status"] == final_check.NOT_MEASURED, unusable
        assert final_check.ORDERABILITY_ZERO not in gate["detail"]

    def test_a_failed_read_is_not_measured_never_zero(self):
        def boom(instrument, limit):
            raise RuntimeError("KIS orderable-amount response unusable "
                               "(output_missing)")

        gate = self._gate(boom, self.UNAFFORDABLE)
        assert gate["status"] == final_check.NOT_MEASURED
        assert final_check.ORDERABILITY_ZERO not in gate["detail"]

    def test_a_genuinely_empty_account_is_still_a_block(self):
        """Zero cash is measured, not unknown."""
        gate = self._gate(0.0, self.AFFORDABLE)
        assert gate["status"] == final_check.BLOCK
        assert final_check.ORDERABILITY_ZERO in gate["detail"]

    def test_the_block_says_whole_shares_only(self):
        """The operator reading this must not have to remember that
        fractional is off before deciding the number looks wrong."""
        detail = self._gate(self.ORDERABLE, self.UNAFFORDABLE)["detail"]
        assert "whole shares only" in detail
        assert "fractional is OFF" in detail


class ReportBrokerSnapshotStub:
    """A snapshot whose account read succeeded and holds no errors."""

    def __init__(self):
        self.positions, self.open_orders = [], []
        self.account = {"cash": 74.01}
        self.errors = {}
        self.calls = {}

    def count(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1
