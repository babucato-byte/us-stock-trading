"""A scan that runs long, and everything that must not follow from it.

S6 scans at :02/:17/:32/:47 and consumes at :07/:22/:37/:52. Five minutes
is not much, and the failure mode when a scan exceeds it is the quiet
kind: the candidate file still exists, still carries today's trading day,
today's session and this variant, and is one cycle out of date. Every
refusal S6 already had is keyed on one of those three fields, so none of
them sees it.

The six properties below split into two halves that pull in opposite
directions, which is why they are tested together:

    new BUY          fails CLOSED   -- refuse when unsure
    held positions   CONTINUE       -- exits, fills and retries run anyway

A guard that satisfied only the first would trap the account in a
position it could not leave, and that is a worse failure than the one it
was written to prevent.
"""

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import s6_sessions  # noqa: E402
from config import scanner_live_mode as slm  # noqa: E402
from s6_live import candidate_source as cs  # noqa: E402
from scanners.publish import candidates as publisher  # noqa: E402
from scanners.publish import scan_cycle  # noqa: E402

DAY = "2026-08-24"
SESSION = "REGULAR"
ORB = s6_sessions.SCANNER_NAME
T0 = datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc)


class Signal:
    """The minimum a publishable ORB signal carries."""

    def __init__(self, symbol, score=70.0, price=100.0):
        self.symbol, self.scanner_score, self.signal_price = symbol, score, price
        self.scanner_name, self.scanner_version = "orb", "orb_v1.0"
        self.signal_id, self.scanner_run_id = f"s-{symbol}", "run"
        self.volume = self.avg_volume = self.volume_multiple = None
        self.price_change_pct = self.hma200 = self.hma200_slope = None
        self.hma89 = self.vwap = None
        self.market_data_provider = self.market_data_feed = None
        self.data_timestamp = self.feature_timestamp = None
        self.source_timeframe = self.timestamp = None
        self.reasons = []
        self.metrics = {"opening_range_high": 99.5, "opening_range_low": 99.0,
                        "orb_minutes": 15, "vwap": 100.0, "price": price}


def live_modes():
    modes = dict(slm.SCANNER_LIVE_MODE)
    modes["orb"] = slm.MODE_LIMITED_LIVE
    return modes


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv(publisher.CANDIDATE_DIR_ENV, str(tmp_path / "handoff"))

    def publish(symbols, day=DAY, session=SESSION, variant="S6-R",
                status=scan_cycle.STATUS_OK):
        publisher.publish([Signal(s) for s in symbols],
                          strategy_id=cs.STRATEGY_ID, trading_day=day,
                          session=session, variant=variant)
        publisher.mark_run(day, session, strategy_id=cs.STRATEGY_ID,
                           candidates=len(symbols), status=status)
    return publish


def source(**kw):
    kw.setdefault("trading_day", DAY)
    kw.setdefault("session", SESSION)
    kw.setdefault("modes", live_modes())
    return cs.S6CandidateSource(**kw)


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE",
                       tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


# --------------------------------------------------------------------
# A. the normal case still works
# --------------------------------------------------------------------
class TestAScanThatFinishedIsConsumed:
    def test_a_completed_scan_hands_its_candidates_over(self, store):
        store(["AAPL"])
        assert source().symbols() == ["AAPL"]

    def test_the_lock_is_released_when_the_scan_ends(self, store):
        store(["AAPL"])
        with scan_cycle.hold(DAY, SESSION, scanner=ORB) as held:
            assert held.acquired
        assert scan_cycle.state(DAY, SESSION, scanner=ORB).running is False
        assert source().symbols() == ["AAPL"]

    def test_the_state_is_reported_in_the_audit_record(self, store):
        store(["AAPL"])
        described = source().describe()
        assert described["scan_state"]["running"] is False
        assert described["scan_state"]["detectable"] is True


# --------------------------------------------------------------------
# B. the long scan itself
# --------------------------------------------------------------------
class TestAScanStillRunningBlocksNewEntries:
    def test_undeclared_rows_are_not_reused_while_a_scan_runs(self, store):
        """Rows with no DECLARED generation are not reused.

        This used to be stated as "the previous cycle's candidates are
        never reused", and that was the right rule while a generation was
        something inferred from row timestamps: an inferred generation
        cannot be shown to be complete, so it could not be trusted while
        a newer answer was being computed.

        Generations are now declared and published atomically
        (scanners/publish/generations.py), so a COMPLETED one CAN be
        trusted -- see tests/test_candidate_generations.py for that
        contract. What has not changed, and is what this pins: rows that
        no generation record vouches for stay refused, and a partial
        in-progress answer is never consumable.
        """
        store(["AAPL"])
        with scan_cycle.hold(DAY, SESSION, scanner=ORB) as held:
            assert held.acquired
            assert source().symbols() == []

    def test_the_refusal_names_the_reason(self, store):
        store(["AAPL"])
        with scan_cycle.hold(DAY, SESSION, scanner=ORB):
            refusal = source().describe()["refusal"]
        assert scan_cycle.REASON_SCAN_IN_PROGRESS in refusal

    def test_qualification_refuses_too(self, store):
        """Not only `symbols()`. A caller holding a symbol from an earlier
        read must not be able to qualify it through a side door."""
        store(["AAPL"])
        with scan_cycle.hold(DAY, SESSION, scanner=ORB):
            assert source().qualify("AAPL").qualified is False

    def test_the_allow_list_is_empty_while_a_scan_runs(self, store):
        store(["AAPL"])
        with scan_cycle.hold(DAY, SESSION, scanner=ORB):
            assert source().allowed_symbols() == frozenset()

    def test_entries_resume_once_the_scan_completes(self, store):
        store(["AAPL"])
        with scan_cycle.hold(DAY, SESSION, scanner=ORB):
            assert source().symbols() == []
        assert source().symbols() == ["AAPL"]

    def test_another_scanners_run_does_not_block_s6(self, store):
        """S1's premarket scan holding its own lock must not stop S6.
        Locking on (day, session) alone would have made it."""
        store(["AAPL"])
        with scan_cycle.hold(DAY, SESSION, scanner="hma_early_trend"):
            assert source().symbols() == ["AAPL"]

    def test_a_scan_in_another_session_does_not_block_this_one(self, store):
        store(["AAPL"])
        with scan_cycle.hold(DAY, "PREMARKET", scanner=ORB):
            assert source().symbols() == ["AAPL"]


# --------------------------------------------------------------------
# C. flock skips, never queues
# --------------------------------------------------------------------
class TestOverlappingScansAreSkippedNotQueued:
    def test_a_second_scan_is_refused_while_the_first_holds(self, store):
        store([])
        with scan_cycle.hold(DAY, SESSION, scanner=ORB) as first:
            assert first.acquired
            with scan_cycle.hold(DAY, SESSION, scanner=ORB) as second:
                assert second.acquired is False
                assert second.skipped is True

    def test_the_refusal_names_what_it_collided_with(self, store):
        store([])
        with scan_cycle.hold(DAY, SESSION, scanner=ORB, run_id="run-1"):
            with scan_cycle.hold(DAY, SESSION, scanner=ORB) as second:
                assert second.blocked_by.run_id == "run-1"
                assert second.blocked_by.running is True

    def test_hold_all_is_all_or_nothing(self, store):
        store([])
        with scan_cycle.hold(DAY, SESSION, scanner=ORB):
            with scan_cycle.hold_all(DAY, SESSION,
                                     scanners=["hma_early_trend", ORB]) as cycle:
                assert cycle.skipped is True
                assert cycle.detail()

    def test_hold_all_with_no_publishing_scanner_takes_nothing(self, store):
        with scan_cycle.hold_all(DAY, SESSION, scanners=[]) as cycle:
            assert cycle.acquired is True
            assert cycle.holds == []

    def test_a_killed_scan_does_not_block_forever(self, store, tmp_path):
        """A flag file would stay set. The kernel releases a flock."""
        store(["AAPL"])
        path = scan_cycle.cycle_path(DAY, SESSION, ORB)
        path.write_text(json.dumps({"started_at": "2026-08-24T13:00:00+00:00",
                                    "pid": 999999}), encoding="utf-8")
        assert scan_cycle.state(DAY, SESSION, scanner=ORB).running is False
        assert source().symbols() == ["AAPL"]


# --------------------------------------------------------------------
# D. a failed scan blocks new entries
# --------------------------------------------------------------------
class TestAFailedScanBlocksNewEntries:
    def test_a_failed_run_refuses_the_rows_that_predate_it(self, store):
        store(["AAPL"])                                   # the good cycle
        publisher.mark_run(DAY, SESSION, strategy_id=cs.STRATEGY_ID,
                           candidates=0, status=scan_cycle.STATUS_FAILED)
        assert source().symbols() == []
        assert scan_cycle.REASON_SCAN_FAILED in source().describe()["refusal"]

    def test_a_later_good_run_clears_it(self, store):
        store(["AAPL"])
        publisher.mark_run(DAY, SESSION, strategy_id=cs.STRATEGY_ID,
                           candidates=0, status=scan_cycle.STATUS_FAILED)
        assert source().symbols() == []
        publisher.mark_run(DAY, SESSION, strategy_id=cs.STRATEGY_ID,
                           candidates=1, status=scan_cycle.STATUS_OK)
        assert source().symbols() == ["AAPL"]

    def test_another_strategys_failure_does_not_block_s6(self, store):
        store(["AAPL"])
        publisher.mark_run(DAY, SESSION, strategy_id="S1_HMA_EARLY_TREND_V1",
                           candidates=0, status=scan_cycle.STATUS_FAILED)
        assert source().symbols() == ["AAPL"]

    def test_a_marker_without_a_status_is_not_read_as_a_failure(self, store):
        """Every marker written before this existed carries no status.
        Reading those as failures would refuse candidates that were fine."""
        publisher.publish([Signal("AAPL")], strategy_id=cs.STRATEGY_ID,
                          trading_day=DAY, session=SESSION, variant="S6-R")
        publisher.mark_run(DAY, SESSION, strategy_id=cs.STRATEGY_ID,
                           candidates=1)
        assert scan_cycle.last_run_consumable(
            DAY, SESSION, strategy_id=cs.STRATEGY_ID) == (True, "")
        assert source().symbols() == ["AAPL"]


# --------------------------------------------------------------------
# E/F. continuity: none of this may touch an exit
# --------------------------------------------------------------------
class TestHeldPositionsAreNotAffected:
    def test_the_exit_path_reads_no_scan_state(self):
        """Structural. An exit that consulted the scan cycle could be
        stopped by a slow scanner, which is how an account gets trapped
        in a position it decided to leave."""
        source_text = (REPO_ROOT / "s6_live" / "exit_runtime.py").read_text()
        assert "scan_cycle" not in source_text
        assert "candidate_source" not in source_text

    def test_exits_run_while_a_scan_holds_the_lock(self, conn, store):
        from s6_live import exit_runtime, position_store

        pid = position_store.record_submission(
            conn, symbol="ABC", variant="S6-R", entry_session=SESSION,
            range_high=99.5, range_low=99.0, now=T0)
        position_store.open_from_fill(conn, pid, quantity=1,
                                      average_fill_price=100.0, now=T0)

        with scan_cycle.hold(DAY, SESSION, scanner=ORB):
            outcomes = exit_runtime.run_exits(
                conn, broker_adapter=None,
                features_fn=lambda s: None, price_fn=lambda s: 98.0,
                session=SESSION, now=T0, orders_allowed=False)
        # It was EVALUATED. Whether it sold is the policy's business; the
        # property here is that the evaluation happened at all.
        assert [o["symbol"] for o in outcomes] == ["ABC"]

    def test_fill_sync_runs_while_a_scan_holds_the_lock(self, conn, store):
        from s6_live import exit_runtime, position_store

        pid = position_store.record_submission(
            conn, symbol="ABC", variant="S6-R", entry_session=SESSION,
            range_high=99.5, range_low=99.0, now=T0)
        with scan_cycle.hold(DAY, SESSION, scanner=ORB):
            applied = exit_runtime.sync_buy_fills(
                conn, fills_for=lambda row: {"filled_quantity": 1,
                                             "average_fill_price": 100.0},
                now=T0)
        assert applied[0]["status"] == "OPENED"
        assert position_store.load(conn, pid)["status"] == position_store.OPEN

    def test_a_latched_exit_still_retries_while_a_scan_runs(self, conn):
        from s6_live import exit_runtime, position_store

        pid = position_store.record_submission(
            conn, symbol="ABC", variant="S6-R", entry_session=SESSION,
            range_high=99.5, range_low=99.0, now=T0)
        position_store.open_from_fill(conn, pid, quantity=1,
                                      average_fill_price=100.0, now=T0)
        position_store.latch_pending_exit(conn, pid, "RANGE_REENTRY", now=T0)

        sent = []

        class Adapter:
            def submit_order(self, symbol, quantity, side, client_order_id):
                sent.append((symbol, quantity, side))

                class Response:
                    status_code = 200
                    text = "ok"
                return Response()

        with scan_cycle.hold(DAY, SESSION, scanner=ORB):
            outcomes = exit_runtime.retry_latched_exits(
                conn, broker_adapter=Adapter(), session=SESSION, now=T0,
                orders_allowed=True)
        assert sent == [("ABC", 1, "sell")]
        assert outcomes[0]["action"] == exit_runtime.ACTION_SOLD


class TestTheEntryPointTakesTheLock:
    """The guard has to be on the thing cron actually invokes."""

    @pytest.fixture
    def runner_calls(self, monkeypatch, store):
        from scanners import runner

        calls = []

        class Report:
            trading_day = DAY
            session = SESSION
            run_id = "run-1"
            started_at = T0.isoformat()
            duration_seconds = 1.0
            outcomes = []
            status = "SUCCESS"
            candidate_count = 0
            provider = "fake"
            provider_feed = None
            universe_size = 0
            universe_type = "explicit"
            skipped_ineligible = 0
            skipped_reason = None
            construction_failures = {}
            fetch_failures = 0
            stored_signals = 0
            publication_status = None
            publication_detail = None
            published_rows = 0

        def fake_run(**kwargs):
            calls.append(kwargs)
            return Report()

        monkeypatch.setattr(runner, "run_scanners", fake_run)
        monkeypatch.setattr(runner, "print_report", lambda report: None)
        monkeypatch.setattr(runner, "publish_report_candidates",
                            lambda report: 0)
        return runner, calls

    def test_a_scan_runs_when_the_lock_is_free(self, runner_calls):
        runner, calls = runner_calls
        assert runner.main(["--scanners", "orb", "--session", SESSION,
                            "--trading-day", DAY,
                            "--ignore-market-calendar"]) == 0
        assert len(calls) == 1

    def test_an_overlapping_scan_is_skipped_with_exit_zero(self, runner_calls):
        """Zero, not one: a refused overlap is the guard working. A
        non-zero exit would page an operator every time a scan ran long."""
        runner, calls = runner_calls
        with scan_cycle.hold(DAY, SESSION, scanner=ORB):
            code = runner.main(["--scanners", "orb", "--session", SESSION,
                                "--trading-day", DAY,
                                "--ignore-market-calendar"])
        assert code == 0
        assert calls == []

    def test_a_non_publishing_scan_takes_no_lock(self, runner_calls):
        runner, calls = runner_calls
        with scan_cycle.hold(DAY, SESSION, scanner=ORB):
            assert runner.main(["--scanners", "breakout_ready",
                                "--session", SESSION, "--trading-day", DAY,
                                "--ignore-market-calendar"]) == 0
        assert len(calls) == 1

    def test_a_default_run_still_locks_orb(self, runner_calls):
        """No profile and no list runs everything, orb included -- which
        is the invocation a person types while a scan is already going."""
        runner, calls = runner_calls
        with scan_cycle.hold(DAY, SESSION, scanner=ORB):
            assert runner.main(["--session", SESSION, "--trading-day", DAY,
                                "--ignore-market-calendar"]) == 0
        assert calls == []

    def test_the_session_flag_reaches_the_scan(self, runner_calls):
        """It was parsed and then never passed, so it silently did
        nothing on a real run."""
        runner, calls = runner_calls
        runner.main(["--scanners", "orb", "--session", "PREMARKET",
                     "--trading-day", DAY, "--ignore-market-calendar"])
        assert calls[0]["session"] == "PREMARKET"
        assert calls[0]["trading_day"] == DAY


class TestOtherStrategiesAreUnaffected:
    def test_s1s_candidate_source_does_not_read_the_scan_cycle(self):
        text = (REPO_ROOT / "s1_live" / "candidate_source.py").read_text()
        assert "scan_cycle" not in text

    def test_s2s_candidate_source_does_not_read_the_scan_cycle(self):
        text = (REPO_ROOT / "s2_live" / "candidate_source.py").read_text()
        assert "scan_cycle" not in text


class TestTheRuntimeIsolatesItsStages:
    """§1 E: one failing stage must not cost the others theirs."""

    def test_each_stage_is_wrapped_independently(self, monkeypatch, conn):
        import scripts.run_s6_runtime as runtime
        from s6_live import position_store

        position_store.record_submission(
            conn, symbol="ABC", variant="S6-R", entry_session=SESSION,
            range_high=99.5, range_low=99.0, now=T0)

        from s6_live import exit_runtime

        called = []

        def stage(name, boom=False):
            def call(*a, **k):
                called.append(name)
                if boom:
                    raise RuntimeError(f"{name} unavailable")
                return []
            return call

        monkeypatch.setattr(exit_runtime, "sync_buy_fills",
                            stage("buy_fills", boom=True))
        monkeypatch.setattr(exit_runtime, "run_exits",
                            stage("exits", boom=True))
        monkeypatch.setattr(exit_runtime, "retry_latched_exits",
                            stage("retried"))
        monkeypatch.setattr(exit_runtime, "sync_sell_fills",
                            stage("sell_fills"))
        monkeypatch.setattr(runtime, "_dependencies",
                            lambda: (None, lambda s: None, lambda s: None, None))
        monkeypatch.setattr(runtime, "_attach_session_report",
                            lambda *a, **k: None)
        report = runtime.run_once(now=T0)

        # Every stage was attempted, and the two failures are reported
        # rather than swallowed: an exit that was never evaluated looks
        # exactly like one that decided to hold.
        assert called == ["buy_fills", "exits", "retried", "sell_fills"]
        assert len(report["errors"]) == 2
        assert report["status"] == "ERROR"
