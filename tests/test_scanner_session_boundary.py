"""A scan that outlives its session must not publish into the next one.

On 2026-09-16 a DAYTIME scan held the outer lock for ~55 minutes, ran
past the 08:00 PREMARKET boundary, and published a completed DAYTIME
generation into a session that no longer existed. The 08:02 PREMARKET
invocation was skipped for a lock the previous session still held.

Two lines of defence, and both are tested here: the symbol loop stops
cooperatively when the clock moves, and the publisher re-asks at the
last possible moment in case the loop had already ended.
"""

from pathlib import Path

import pytest

from scanners.publish import generations

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = (REPO_ROOT / "scanners" / "runner.py").read_text(encoding="utf-8")
WRAPPER = (REPO_ROOT / "deploy" / "cron" / "s6_scan.sh").read_text(encoding="utf-8")


class TestA_SameSessionCompletesNormally:
    """A. Nothing changes for a scan that stays inside its session."""

    def test_the_guard_only_fires_on_a_difference(self):
        block = RUNNER[RUNNER.index("SESSION_BOUNDARY_ABORTED session=%s"):]
        # The abort is conditional on inequality, never unconditional.
        prelude = RUNNER[:RUNNER.index("SESSION_BOUNDARY_ABORTED session=%s")]
        assert "if current_session != clock_session_at_start:" in prelude
        assert block  # the message exists

    def test_publication_proceeds_when_the_session_is_unchanged(self):
        block = RUNNER[RUNNER.index("STALE_PUBLISH_BLOCKED"):]
        prelude = RUNNER[:RUNNER.index("STALE_PUBLISH_BLOCKED")]
        assert 'getattr(report, "session_boundary_aborted", False) or crossed' \
            in prelude
        assert block


class TestB_BoundaryDuringSymbolLoop:
    """B. The clock moves mid-loop -> cooperative abort."""

    def test_the_loop_breaks_rather_than_being_killed(self):
        start = RUNNER.index("SESSION_BOUNDARY_ABORTED session=%s")
        after = RUNNER[start:start + 700]
        assert "break" in after, "the loop must stop cooperatively"
        for violent in ("sys.exit", "os._exit", "SIGKILL", "kill("):
            assert violent not in after, "a boundary is not a reason to be killed"

    def test_the_check_runs_after_the_symbol_not_during_the_fetch(self):
        """`_symbol_bundles` yields once the provider call has returned,
        and breaking at the END of the body never advances it again --
        so nothing in flight is interrupted and no new fetch starts."""
        body = RUNNER[RUNNER.index("for bundle in _symbol_bundles("):
                      RUNNER.index("report.outcomes = [outcomes[")]
        assert body.index("total_symbol_elapsed_ms") < body.index(
            "current_session = scan_session.session_at()"), (
            "the boundary check must come after the symbol's own work")

    def test_the_run_records_what_it_saw(self):
        for field in ("session_boundary_aborted", "boundary_session",
                      "boundary_symbols_scanned", "clock_session_at_start"):
            assert f"{field}:" in RUNNER, f"RunReport must carry {field}"


class TestC_BoundaryAfterLastSymbol:
    """C. The loop ended cleanly; the boundary moved before persistence."""

    def test_the_publisher_re_resolves_the_session_itself(self):
        """Defence in depth: the publisher does not trust the loop to
        have noticed."""
        block = RUNNER[RUNNER.index("def publish_report_candidates"):]
        assert "current_session = scan_session.session_at()" in block, (
            "publication must re-ask, not rely on the loop's verdict")


class TestD_NoPublishAtTheBoundary:
    """D. Nothing is written when the session has moved."""

    def test_rows_are_not_written(self):
        block = RUNNER[RUNNER.index("STALE_PUBLISH_BLOCKED"):]
        guard = block[:block.index("continue")]
        assert "candidate_publisher.publish(" not in guard, (
            "no candidate row may be written for a stale session")

    def test_it_continues_rather_than_falling_through(self):
        block = RUNNER[RUNNER.index("STALE_PUBLISH_BLOCKED"):]
        assert "continue" in block[:1400]


class TestE_AbortedGenerationIsNotCompleted:
    """E. An aborted run declares itself, and never as COMPLETED."""

    def test_the_status_exists_and_is_distinct(self):
        assert generations.STATUS_SESSION_BOUNDARY_ABORTED == \
            "SESSION_BOUNDARY_ABORTED"
        assert generations.STATUS_SESSION_BOUNDARY_ABORTED not in (
            generations.STATUS_COMPLETED, generations.STATUS_FAILED)

    def test_the_publisher_declares_it(self):
        block = RUNNER[RUNNER.index("STALE_PUBLISH_BLOCKED"):]
        guard = block[:block.index("continue")]
        assert "STATUS_SESSION_BOUNDARY_ABORTED" in guard
        assert "STATUS_COMPLETED" not in guard

    def test_a_declared_abort_beats_a_missing_record(self):
        """Absence would fall back to inferring the newest generation
        from the rows on disk. Declaring it is what makes the abort
        visible as an abort."""
        block = RUNNER[RUNNER.index("STALE_PUBLISH_BLOCKED"):]
        guard = block[:block.index("continue")]
        assert "generations.publish(" in guard


class TestF_AbortedRowsAreInvisible:
    """F. Nothing from an aborted run reaches the consumers."""

    def _record(self, status):
        return {"status": status, "trading_day": "2026-09-16",
                "session": "OVERNIGHT_DAYTIME", "strategy_id": "S6_ORB_BREAKOUT_V1",
                "variant": "S6-O", "generation_id": "r1", "candidate_count": 3}

    def test_an_aborted_generation_is_not_consumable(self):
        assert generations.is_consumable(
            self._record(generations.STATUS_SESSION_BOUNDARY_ABORTED),
            variant="S6-O", trading_day="2026-09-16",
            session="OVERNIGHT_DAYTIME",
            strategy_id="S6_ORB_BREAKOUT_V1") is False

    def test_a_completed_generation_still_is(self):
        assert generations.is_consumable(
            self._record(generations.STATUS_COMPLETED),
            variant="S6-O", trading_day="2026-09-16",
            session="OVERNIGHT_DAYTIME",
            strategy_id="S6_ORB_BREAKOUT_V1") is True

    def test_only_completed_is_consumable(self):
        assert generations.CONSUMABLE_STATUSES == frozenset(
            {generations.STATUS_COMPLETED})

    def test_candidate_source_gates_on_consumability(self):
        source = (REPO_ROOT / "s6_live" / "candidate_source.py").read_text(
            encoding="utf-8")
        assert "generations.is_consumable(" in source, (
            "active-watch and fast-watch read through candidate_source; "
            "it is the gate that keeps an aborted generation invisible")


class TestTheGuardActuallyBlocks:
    """The behavioural version of C/D/E/F -- the source-text checks above
    say the guard is written; these say it works."""

    @pytest.fixture
    def published(self, tmp_path, monkeypatch):
        """Capture what publication would write, without a disk."""
        monkeypatch.setenv("SCANNER_CANDIDATE_DIR", str(tmp_path))
        calls = {"rows": [], "generations": []}

        from scanners.publish import candidates as cp
        from scanners.publish import generations as gp
        from scanners.publish import scan_cycle

        monkeypatch.setattr(cp, "publish",
                            lambda *a, **k: calls["rows"].append(k) or [])
        monkeypatch.setattr(gp, "publish",
                            lambda *a, **k: calls["generations"].append(k))
        monkeypatch.setattr(scan_cycle, "mark_run", lambda *a, **k: None,
                            raising=False)
        monkeypatch.setattr(cp, "mark_run", lambda *a, **k: None, raising=False)
        return calls

    def _report(self, session="OVERNIGHT_DAYTIME", aborted=False):
        from scanners.base.scanner_base import ScanOutcome
        from scanners.runner import RunReport

        outcome = ScanOutcome(scanner_name="orb", scanner_version="v1",
                              config_fingerprint="fp", trading_day="2026-09-16")
        outcome.failed = False
        outcome.signals = [object()]
        report = RunReport(trading_day="2026-09-16", started_at="2026-09-16T01:00:00Z",
                           provider="test", universe_size=1)
        report.session = session
        report.clock_session_at_start = session
        report.run_id = "r1"
        report.outcomes = [outcome]
        report.session_boundary_aborted = aborted
        return report

    def test_a_boundary_blocks_every_row(self, published, monkeypatch):
        """D/F. The clock has moved to PREMARKET; nothing is written."""
        from scanners.base import scan_session
        from scanners.publish import generations as gp
        from scanners import runner

        monkeypatch.setattr(scan_session, "session_at", lambda *a, **k: "PREMARKET")
        runner.publish_report_candidates(self._report("OVERNIGHT_DAYTIME"))

        assert published["rows"] == [], "no candidate row may be written"
        assert len(published["generations"]) == 1
        assert published["generations"][0]["status"] == \
            gp.STATUS_SESSION_BOUNDARY_ABORTED

    def test_a_loop_abort_blocks_even_inside_the_same_session(
            self, published, monkeypatch):
        """E. The loop already stopped; the clock has not moved yet."""
        from scanners.base import scan_session
        from scanners.publish import generations as gp
        from scanners import runner

        monkeypatch.setattr(scan_session, "session_at",
                            lambda *a, **k: "OVERNIGHT_DAYTIME")
        runner.publish_report_candidates(
            self._report("OVERNIGHT_DAYTIME", aborted=True))

        assert published["rows"] == []
        assert published["generations"][0]["status"] == \
            gp.STATUS_SESSION_BOUNDARY_ABORTED

    def test_an_unbroken_session_still_publishes(self, published, monkeypatch):
        """J. The normal path is untouched."""
        from scanners.base import scan_session
        from scanners.publish import generations as gp
        from scanners import runner

        monkeypatch.setattr(scan_session, "session_at",
                            lambda *a, **k: "OVERNIGHT_DAYTIME")
        runner.publish_report_candidates(self._report("OVERNIGHT_DAYTIME"))

        assert published["rows"], "a same-session scan must still publish"
        assert published["generations"][0]["status"] == gp.STATUS_COMPLETED


class TestG_TheNextSessionCanStart:
    """G. An aborted scan releases the outer lock normally."""

    def test_the_wrapper_releases_by_exiting(self):
        """`flock` holds the lock for the life of the command it wraps,
        so a cooperative return releases it -- no explicit unlock, and
        nothing left held for the next session."""
        assert "flock -n -E 99 /home/ubuntu/logs/cron/s6_scan.lock" in WRAPPER
        block = RUNNER[RUNNER.index("SESSION_BOUNDARY_ABORTED session=%s"):]
        for held in ("while True", "time.sleep(", "input("):
            assert held not in block[:700], "an aborted scan must not linger"

    def test_a_skipped_invocation_is_no_longer_silent(self):
        assert "SCANNER_LOCK_SKIPPED" in WRAPPER
        assert "exit 0" in WRAPPER[WRAPPER.index("SCANNER_LOCK_SKIPPED"):]


class TestH_NoConcurrentSessionScanners:
    """H. Serialisation is kept; parallel scans are not enabled."""

    def test_the_outer_lock_is_still_taken_non_blocking(self):
        flock_lines = [ln for ln in WRAPPER.splitlines()
                       if ln.strip().startswith("flock")]
        assert flock_lines, "the outer lock must remain"
        assert any("s6_scan.lock" in ln and "-n" in ln for ln in flock_lines), (
            "two sessions scanning at once is not approved")

    def test_the_lock_file_is_unchanged(self):
        assert "/home/ubuntu/logs/cron/s6_scan.lock" in WRAPPER


class TestI_InnerScanLockUnchanged:
    """I. The existing same-session cycle lock is untouched."""

    def test_the_runner_still_uses_its_cycle_lock(self):
        from scanners.base import run_context

        assert hasattr(run_context, "__file__")
        assert "run_context" in RUNNER


class TestJ_ObservabilityFields:
    """5. Each outcome carries enough to reconstruct what happened."""

    @pytest.mark.parametrize("field", [
        "session=", "current_session=", "pid=", "generation_id=",
        "started_at=", "elapsed_seconds=",
    ])
    def test_the_boundary_abort_reports_it(self, field):
        block = RUNNER[RUNNER.index("SESSION_BOUNDARY_ABORTED session=%s"):]
        assert field in block[:600]

    @pytest.mark.parametrize("field", [
        "session=", "current_session=", "pid=", "generation_id=", "started_at=",
    ])
    def test_the_stale_publish_block_reports_it(self, field):
        block = RUNNER[RUNNER.index("STALE_PUBLISH_BLOCKED"):]
        assert field in block[:700]

    @pytest.mark.parametrize("field", [
        "session=", "pid=", "started_at=", "elapsed_seconds=",
    ])
    def test_the_lock_outcomes_report_it(self, field):
        block = WRAPPER[WRAPPER.index("SCANNER_LOCK_SKIPPED"):]
        assert field in block[:400]


class TestNothingElseChanged:
    """The scope fence, asserted."""

    def test_the_cron_cadence_is_untouched(self):
        assert "--supplement-size 50" in WRAPPER
        assert "run_scanners.py --scanners orb" in WRAPPER

    def test_no_parallelism_was_introduced(self):
        for token in ("ThreadPool", "ProcessPool", "concurrent.futures",
                      "multiprocessing"):
            assert token not in RUNNER, "parallel scans are not approved"
