"""The supervisor's session-boundary decision.

Covers the seven cases the P0-3 fix has to get right: one per real
session transition, the no-op when nothing moved, the unknown-input
cases that must NOT restart, and the loop guard.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from market_data import collector_session as cs


def _status(tmp_path, session, name="collector_status.json"):
    path = tmp_path / name
    payload = {"state": "CONNECTED_ACTIVE", "connection_state": "CONNECTED",
               "market_session": session, "subscription_count": 41,
               "subscription_requested": 41}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestSameSession:
    def test_matching_session_does_not_restart(self, tmp_path):
        """1. same session -> no restart."""
        status = _status(tmp_path, "REGULAR")
        verdict = cs.assess(status, current_session="REGULAR")
        assert verdict.restart is False
        assert verdict.reason == cs.SESSION_MATCH

    def test_match_is_case_and_space_insensitive(self, tmp_path):
        """A cosmetic difference is not a boundary crossing."""
        status = _status(tmp_path, " regular ")
        assert cs.assess(status, current_session="REGULAR").restart is False


class TestBoundaryCrossings:
    """2-5. every real session transition restarts."""

    @pytest.mark.parametrize("running,now", [
        ("OVERNIGHT_DAYTIME", "PREMARKET"),
        ("PREMARKET", "REGULAR"),
        ("REGULAR", "AFTER_HOURS"),
        ("AFTER_HOURS", "OVERNIGHT_DAYTIME"),
    ])
    def test_crossing_restarts(self, tmp_path, running, now):
        status = _status(tmp_path, running)
        verdict = cs.assess(status, current_session=now)
        assert verdict.restart is True
        assert verdict.reason == cs.SESSION_CHANGED
        assert verdict.detail == f"{running}->{now}"


class TestUnknownInputsNeverRestart:
    """6. missing/malformed status preserves the existing safe behaviour."""

    def test_missing_status_file(self, tmp_path):
        verdict = cs.assess(tmp_path / "nope.json", current_session="REGULAR")
        assert verdict.restart is False
        assert verdict.reason == cs.SESSION_UNKNOWN

    def test_malformed_json(self, tmp_path):
        path = tmp_path / "collector_status.json"
        path.write_text("{not json", encoding="utf-8")
        assert cs.assess(path, current_session="REGULAR").restart is False

    def test_status_is_not_an_object(self, tmp_path):
        path = tmp_path / "collector_status.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        assert cs.assess(path, current_session="REGULAR").restart is False

    def test_status_without_market_session(self, tmp_path):
        path = tmp_path / "collector_status.json"
        path.write_text(json.dumps({"state": "CONNECTED_ACTIVE"}),
                        encoding="utf-8")
        verdict = cs.assess(path, current_session="REGULAR")
        assert verdict.restart is False
        assert verdict.reason == cs.SESSION_UNKNOWN

    def test_null_market_session(self, tmp_path):
        status = _status(tmp_path, None)
        assert cs.assess(status, current_session="REGULAR").restart is False

    def test_unknown_current_session(self, tmp_path):
        status = _status(tmp_path, "REGULAR")
        assert cs.assess(status, current_session="").restart is False
        assert cs.assess(status, current_session=None).restart is False

    def test_no_process_running_is_never_a_restart(self, tmp_path):
        """Nothing to replace; the wrapper starts one regardless."""
        status = _status(tmp_path, "PREMARKET")
        verdict = cs.assess(status, current_session="REGULAR",
                            process_running=False)
        assert verdict.restart is False


class TestRestartLoopGuard:
    """7. the restart happens once, not every cron tick."""

    def test_second_restart_within_cooldown_is_refused(self, tmp_path):
        marker = tmp_path / cs.MARKER_NAME
        now = datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc)
        assert cs.restart_allowed(marker, now=now) is True
        cs.note_restart(marker, now=now)
        assert cs.restart_allowed(
            marker, now=now + timedelta(seconds=60)) is False
        assert cs.restart_allowed(
            marker, now=now + timedelta(seconds=599)) is False

    def test_restart_allowed_again_after_cooldown(self, tmp_path):
        marker = tmp_path / cs.MARKER_NAME
        now = datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc)
        cs.note_restart(marker, now=now)
        assert cs.restart_allowed(
            marker, now=now + timedelta(seconds=601)) is True

    def test_unreadable_marker_allows_the_restart(self, tmp_path):
        """A missing marker must not block the FIRST crossing."""
        assert cs.restart_allowed(tmp_path / "never-written") is True

    def test_a_correct_collector_is_left_alone_after_its_restart(
            self, tmp_path):
        """The loop cannot continue: the replacement reports the new
        session, so the next tick sees SESSION_MATCH."""
        status = _status(tmp_path, "OVERNIGHT_DAYTIME")
        assert cs.assess(status, current_session="PREMARKET").restart is True
        _status(tmp_path, "PREMARKET")  # the replacement comes up
        verdict = cs.assess(status, current_session="PREMARKET")
        assert verdict.restart is False
        assert verdict.reason == cs.SESSION_MATCH


class TestCliContract:
    """Exit codes are the contract the shell wrapper reads."""

    def test_exit_2_on_a_crossing_then_0_while_cooling_down(self, tmp_path):
        status = _status(tmp_path, "REGULAR")
        marker = tmp_path / cs.MARKER_NAME
        argv = ["--status", str(status), "--marker", str(marker),
                "--process-running", "yes", "--current-session", "AFTER_HOURS"]
        assert cs.main(argv) == 2
        assert cs.main(argv) == 0  # cooldown holds the loop shut

    def test_exit_0_when_sessions_agree(self, tmp_path):
        status = _status(tmp_path, "REGULAR")
        assert cs.main([
            "--status", str(status), "--marker", str(tmp_path / cs.MARKER_NAME),
            "--process-running", "yes", "--current-session", "REGULAR"]) == 0

    def test_exit_0_when_status_is_missing(self, tmp_path):
        """Unknown is never a non-zero exit: collector_health owns that."""
        assert cs.main([
            "--status", str(tmp_path / "absent.json"),
            "--marker", str(tmp_path / cs.MARKER_NAME),
            "--process-running", "yes", "--current-session", "REGULAR"]) == 0

    def test_current_session_defaults_to_the_live_clock(self, tmp_path):
        """Omitting --current-session must resolve, not crash."""
        from scanners.base import scan_session

        status = _status(tmp_path, scan_session.session_at())
        assert cs.main([
            "--status", str(status), "--marker", str(tmp_path / cs.MARKER_NAME),
            "--process-running", "yes"]) == 0
