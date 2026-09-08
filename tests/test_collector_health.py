"""The collector's self-healing is bounded and never restarts a quiet venue."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_data import collector_health as ch  # noqa: E402

NOW = datetime(2026, 9, 9, 1, 30, tzinfo=timezone.utc)


def _status(tmp_path, **over):
    payload = {"state": "CONNECTED_NO_TRADES", "connection_state": "CONNECTED",
               "subscription_requested": 32, "subscription_count": 32,
               "collector_started_at": (NOW - timedelta(minutes=20)).isoformat(),
               "last_heartbeat_at": (NOW - timedelta(seconds=30)).isoformat()}
    payload.update(over)
    path = tmp_path / "collector_status.json"
    path.write_text(json.dumps(payload))
    return path


class TestAssess:
    def test_a_quiet_venue_is_healthy(self, tmp_path):
        verdict = ch.assess(_status(tmp_path), process_running=True, now=NOW)
        assert verdict.healthy and verdict.reason == ch.HEALTHY

    def test_no_process_is_reported_but_not_a_forced_restart(self, tmp_path):
        verdict = ch.assess(_status(tmp_path), process_running=False, now=NOW)
        assert not verdict.healthy and verdict.reason == ch.NO_PROCESS
        assert verdict.restart_recommended is False

    def test_a_stale_heartbeat_recommends_a_restart(self, tmp_path):
        path = _status(tmp_path, last_heartbeat_at=(NOW - timedelta(minutes=10)).isoformat())
        verdict = ch.assess(path, process_running=True, now=NOW)
        assert verdict.reason == ch.HEARTBEAT_STALE and verdict.restart_recommended

    def test_a_long_disconnect_recommends_a_restart(self, tmp_path):
        path = _status(tmp_path, connection_state="DISCONNECTED")
        verdict = ch.assess(path, process_running=True, now=NOW)
        assert verdict.reason == ch.NOT_CONNECTED and verdict.restart_recommended

    def test_a_fresh_start_still_connecting_is_given_grace(self, tmp_path):
        path = _status(tmp_path, connection_state="CONNECTING",
                       collector_started_at=(NOW - timedelta(seconds=60)).isoformat())
        assert ch.assess(path, process_running=True, now=NOW).healthy

    def test_incomplete_subscriptions_recommend_a_restart(self, tmp_path):
        path = _status(tmp_path, subscription_count=12)
        verdict = ch.assess(path, process_running=True, now=NOW)
        assert verdict.reason == ch.SUBSCRIPTIONS_INCOMPLETE and verdict.restart_recommended

    def test_an_unreadable_status_with_a_process_recommends_a_restart(self, tmp_path):
        verdict = ch.assess(tmp_path / "missing.json", process_running=True, now=NOW)
        assert verdict.reason == ch.NO_STATUS and verdict.restart_recommended


class TestTheRestartIsBounded:
    def test_first_restart_is_allowed_and_recorded(self, tmp_path):
        marker = tmp_path / "m"
        assert ch.restart_allowed(marker, now=NOW)
        ch.note_restart(marker, now=NOW)
        assert not ch.restart_allowed(marker, now=NOW + timedelta(minutes=5))
        assert ch.restart_allowed(marker, now=NOW + timedelta(minutes=16))

    def test_the_cli_exit_codes(self, tmp_path, capsys):
        """The CLI uses the wall clock, so the fixture is dated from it."""
        real_now = datetime.now(timezone.utc)
        path = _status(tmp_path, last_heartbeat_at=(real_now - timedelta(hours=1)).isoformat(),
                       collector_started_at=(real_now - timedelta(hours=2)).isoformat())
        marker = tmp_path / "marker"
        assert ch.main(["--status", str(path), "--marker", str(marker),
                        "--process-running", "yes"]) == 2
        assert ch.main(["--status", str(path), "--marker", str(marker),
                        "--process-running", "yes"]) == 1     # cooled down
        healthy = _status(tmp_path, last_heartbeat_at=real_now.isoformat(),
                          collector_started_at=(real_now - timedelta(hours=2)).isoformat())
        assert ch.main(["--status", str(healthy), "--marker", str(marker),
                        "--process-running", "yes"]) == 0

    def test_the_wrapper_restarts_only_on_exit_code_2(self):
        source = (REPO_ROOT / "deploy" / "cron" / "s6_realtime_collector.sh").read_text()
        assert "market_data.collector_health" in source
        assert 'if [ "$HEALTH_RC" = "2" ]; then' in source
        assert "pkill -TERM" in source and "COLLECTOR_RESTART" in source
        assert "collector_restart.marker" in source
