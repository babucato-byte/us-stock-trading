"""A reconciliation snapshot must be FRESH, not merely present.

Codex reproduced the defect: a 30-day-old snapshot that was clean and
mismatch-free armed the Shadow timer, because the approval script only
checked that `checked_at` existed. A recurring evaluation would then
have run indefinitely against an account reconciliation from a month
earlier.

The check lives in one module now, used by the approval script, the
service unit's ExecStartPre and the Shadow entrypoint, so their TTL,
clock-skew tolerance and reason codes cannot drift apart.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from reconciliation import freshness, reconciliation_state

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER = REPO_ROOT / "scripts" / "check_reconciliation_freshness.py"

NOW = datetime(2026, 8, 4, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(freshness.ENV_MAX_AGE, raising=False)
    monkeypatch.delenv(freshness.ENV_MAX_FUTURE_SKEW, raising=False)
    yield


def write_snapshot(tmp_path, *, checked_at=NOW, clean=True, mismatch_count=0,
                   raw=None, name="RECONCILIATION.json"):
    path = tmp_path / name
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
        return path
    payload = {"clean": clean, "mismatch_count": mismatch_count}
    if checked_at is not None:
        payload["checked_at"] = (checked_at.isoformat()
                                 if isinstance(checked_at, datetime) else checked_at)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def evaluate(path, **kwargs):
    return freshness.evaluate(path=path, now=NOW, **kwargs)


# =====================================================================
# The control, first: a fresh snapshot really is accepted.
# =====================================================================

class TestAFreshSnapshotIsAccepted:
    def test_a_snapshot_from_now_is_usable(self, tmp_path):
        result = evaluate(write_snapshot(tmp_path))
        assert result.age_seconds == 0
        assert result.clean is True
        assert result.max_age_seconds == 900

    def test_the_default_ttl_is_fifteen_minutes(self):
        assert freshness.DEFAULT_MAX_AGE_SECONDS == 900

    def test_the_default_future_skew_is_thirty_seconds(self):
        assert freshness.DEFAULT_MAX_FUTURE_SKEW_SECONDS == 30

    def test_the_log_fields_carry_the_numbers_and_nothing_else(self, tmp_path):
        fields = evaluate(write_snapshot(tmp_path)).as_log_fields()
        assert set(fields) == {"snapshot_age_seconds", "max_age_seconds",
                               "future_skew_seconds", "clean", "mismatch_count"}


# =====================================================================
# Staleness -- the reported defect.
# =====================================================================

class TestStaleSnapshotsAreRefused:
    def test_the_reported_thirty_day_snapshot_is_refused(self, tmp_path):
        """Codex's exact reproduction: clean, no mismatches, 30 days old."""
        path = write_snapshot(tmp_path, checked_at=NOW - timedelta(days=30))
        with pytest.raises(freshness.SnapshotUnusable) as excinfo:
            evaluate(path)
        assert excinfo.value.reason_code == "RECONCILIATION_SNAPSHOT_STALE"

    @pytest.mark.parametrize("age,usable", [(0, True), (899, True), (900, True),
                                            (901, False), (3600, False)])
    def test_the_ttl_boundary_is_inclusive(self, tmp_path, age, usable):
        """Documented policy: age <= TTL passes, age > TTL fails."""
        path = write_snapshot(tmp_path, checked_at=NOW - timedelta(seconds=age))
        if usable:
            assert evaluate(path).age_seconds == age
        else:
            with pytest.raises(freshness.SnapshotUnusable) as excinfo:
                evaluate(path)
            assert excinfo.value.reason_code == "RECONCILIATION_SNAPSHOT_STALE"

    def test_the_detail_names_the_age_and_the_limit(self, tmp_path):
        path = write_snapshot(tmp_path, checked_at=NOW - timedelta(hours=2))
        with pytest.raises(freshness.SnapshotUnusable) as excinfo:
            evaluate(path)
        assert "age=" in excinfo.value.detail and "max_age=" in excinfo.value.detail

    def test_a_configured_ttl_is_honoured(self, tmp_path, monkeypatch):
        monkeypatch.setenv(freshness.ENV_MAX_AGE, "60")
        path = write_snapshot(tmp_path, checked_at=NOW - timedelta(seconds=120))
        with pytest.raises(freshness.SnapshotUnusable):
            evaluate(path)


# =====================================================================
# The future -- a clock that moved backwards must not read as fresh.
# =====================================================================

class TestFutureSnapshotsAreRefused:
    @pytest.mark.parametrize("ahead,usable", [(0, True), (29, True), (30, True),
                                              (31, False), (3600, False),
                                              (86400 * 30, False)])
    def test_the_skew_boundary(self, tmp_path, ahead, usable):
        path = write_snapshot(tmp_path, checked_at=NOW + timedelta(seconds=ahead))
        if usable:
            assert evaluate(path).age_seconds == 0
        else:
            with pytest.raises(freshness.SnapshotUnusable) as excinfo:
                evaluate(path)
            assert excinfo.value.reason_code == "RECONCILIATION_SNAPSHOT_FROM_FUTURE"

    def test_a_rolled_back_server_clock_does_not_look_fresh(self, tmp_path):
        """If the host clock jumps backwards, yesterday's snapshot looks
        like tomorrow's. That must block, not pass."""
        path = write_snapshot(tmp_path, checked_at=NOW + timedelta(days=1))
        with pytest.raises(freshness.SnapshotUnusable) as excinfo:
            evaluate(path)
        assert excinfo.value.reason_code == "RECONCILIATION_SNAPSHOT_FROM_FUTURE"

    def test_tolerated_skew_is_reported_as_zero_age_not_negative(self, tmp_path):
        path = write_snapshot(tmp_path, checked_at=NOW + timedelta(seconds=10))
        assert evaluate(path).age_seconds == 0


# =====================================================================
# Timestamps.
# =====================================================================

class TestTimestampParsing:
    @pytest.mark.parametrize("text", [
        "2026-08-04T15:00:00Z",
        "2026-08-04T15:00:00+00:00",
        "2026-08-05T00:00:00+09:00",
    ])
    def test_timezone_aware_forms_are_accepted(self, tmp_path, text):
        assert evaluate(write_snapshot(tmp_path, checked_at=text)) is not None

    def test_an_offset_timestamp_is_normalised_not_taken_literally(self, tmp_path):
        """15:00Z and 00:00+09:00 are the same instant; the age must be
        the same, not nine hours apart."""
        utc = evaluate(write_snapshot(tmp_path, checked_at="2026-08-04T15:00:00Z"))
        offset = evaluate(write_snapshot(tmp_path, checked_at="2026-08-05T00:00:00+09:00",
                                         name="B.json"))
        assert utc.age_seconds == offset.age_seconds == 0

    def test_a_naive_timestamp_is_refused(self, tmp_path):
        """Applying the server's local zone would shift the age silently,
        which is how a stale snapshot could read as fresh."""
        path = write_snapshot(tmp_path, checked_at="2026-08-04T15:00:00")
        with pytest.raises(freshness.SnapshotUnusable) as excinfo:
            evaluate(path)
        assert excinfo.value.reason_code == "RECONCILIATION_TIMESTAMP_TIMEZONE_MISSING"

    @pytest.mark.parametrize("value", ["", "   ", "not-a-date", "2026-08-04",
                                       "NaN", "20260804T150000"])
    def test_unparseable_values_are_refused(self, tmp_path, value):
        path = write_snapshot(tmp_path, checked_at=value)
        with pytest.raises(freshness.SnapshotUnusable) as excinfo:
            evaluate(path)
        assert excinfo.value.reason_code in ("RECONCILIATION_TIMESTAMP_INVALID",
                                             "RECONCILIATION_TIMESTAMP_TIMEZONE_MISSING")

    @pytest.mark.parametrize("value", [None, 12345, 1.5, True, [], {}])
    def test_non_string_values_are_refused(self, tmp_path, value):
        path = tmp_path / "R.json"
        path.write_text(json.dumps({"clean": True, "mismatch_count": 0,
                                    "checked_at": value}), encoding="utf-8")
        with pytest.raises(freshness.SnapshotUnusable) as excinfo:
            evaluate(path)
        assert excinfo.value.reason_code == "RECONCILIATION_TIMESTAMP_INVALID"

    def test_a_missing_checked_at_is_refused(self, tmp_path):
        path = write_snapshot(tmp_path, checked_at=None)
        with pytest.raises(freshness.SnapshotUnusable) as excinfo:
            evaluate(path)
        assert excinfo.value.reason_code == "RECONCILIATION_TIMESTAMP_INVALID"
        assert excinfo.value.detail == "missing"


# =====================================================================
# Clean is not sufficient on its own.
# =====================================================================

class TestCleanAloneIsNotEnough:
    def test_clean_but_stale(self, tmp_path):
        with pytest.raises(freshness.SnapshotUnusable) as e:
            evaluate(write_snapshot(tmp_path, checked_at=NOW - timedelta(days=30)))
        assert e.value.reason_code == "RECONCILIATION_SNAPSHOT_STALE"

    def test_clean_but_from_the_future(self, tmp_path):
        with pytest.raises(freshness.SnapshotUnusable) as e:
            evaluate(write_snapshot(tmp_path, checked_at=NOW + timedelta(hours=1)))
        assert e.value.reason_code == "RECONCILIATION_SNAPSHOT_FROM_FUTURE"

    def test_clean_but_unparseable_timestamp(self, tmp_path):
        with pytest.raises(freshness.SnapshotUnusable):
            evaluate(write_snapshot(tmp_path, checked_at="whenever"))

    def test_not_clean(self, tmp_path):
        with pytest.raises(freshness.SnapshotUnusable) as e:
            evaluate(write_snapshot(tmp_path, clean=False))
        assert e.value.reason_code == "RECONCILIATION_NOT_CLEAN"

    def test_clean_but_mismatched(self, tmp_path):
        with pytest.raises(freshness.SnapshotUnusable) as e:
            evaluate(write_snapshot(tmp_path, clean=True, mismatch_count=3))
        assert e.value.reason_code == "RECONCILIATION_NOT_CLEAN"
        assert "mismatch_count=3" in e.value.detail


# =====================================================================
# The file itself.
# =====================================================================

class TestSnapshotFileSafety:
    def test_a_missing_file(self, tmp_path):
        with pytest.raises(freshness.SnapshotUnusable) as e:
            evaluate(tmp_path / "absent.json")
        assert e.value.reason_code == "RECONCILIATION_SNAPSHOT_MISSING"

    def test_a_symlink_is_refused_without_following_it(self, tmp_path):
        real = write_snapshot(tmp_path, name="real.json")
        link = tmp_path / "link.json"
        os.symlink(real, link)
        with pytest.raises(freshness.SnapshotUnusable) as e:
            evaluate(link)
        assert e.value.reason_code == "RECONCILIATION_SNAPSHOT_INVALID"
        assert e.value.detail == "symlink"
        assert os.path.islink(link), "the symlink was removed"

    def test_a_directory_is_refused(self, tmp_path):
        target = tmp_path / "dir.json"
        target.mkdir()
        with pytest.raises(freshness.SnapshotUnusable) as e:
            evaluate(target)
        assert e.value.detail == "non_regular_file"

    def test_a_world_writable_snapshot_is_refused(self, tmp_path):
        path = write_snapshot(tmp_path)
        path.chmod(0o666)
        with pytest.raises(freshness.SnapshotUnusable) as e:
            evaluate(path)
        assert e.value.detail == "world_writable"

    @pytest.mark.parametrize("raw", ['{"clean": true, "mismatch_c',
                                     "", "not json", "[]", "null", '"text"'])
    def test_partial_or_malformed_json_is_refused(self, tmp_path, raw):
        with pytest.raises(freshness.SnapshotUnusable) as e:
            evaluate(write_snapshot(tmp_path, raw=raw))
        assert e.value.reason_code == "RECONCILIATION_SNAPSHOT_INVALID"


class TestTheWriterIsAtomic:
    def test_a_reader_never_sees_a_partial_document(self, tmp_path, monkeypatch):
        """The writer truncated in place, so a crash mid-write destroyed
        a good snapshot and left an unusable one."""
        target = tmp_path / "R.json"
        reconciliation_state.record_result(clean=True, mismatch_count=0, now=NOW,
                                           path=target)
        original = target.read_text(encoding="utf-8")

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(reconciliation_state.os, "replace", _boom)
        with pytest.raises(reconciliation_state.ReconciliationStateError):
            reconciliation_state.record_result(clean=False, mismatch_count=9, now=NOW,
                                               path=target)
        assert target.read_text(encoding="utf-8") == original

    def test_no_temp_file_survives_a_failed_write(self, tmp_path, monkeypatch):
        target = tmp_path / "R.json"
        reconciliation_state.record_result(clean=True, mismatch_count=0, now=NOW,
                                           path=target)
        monkeypatch.setattr(reconciliation_state.os, "replace",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
        with pytest.raises(reconciliation_state.ReconciliationStateError):
            reconciliation_state.record_result(clean=True, mismatch_count=0, now=NOW,
                                               path=target)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["R.json"]

    def test_the_written_snapshot_is_accepted_by_the_freshness_check(self, tmp_path):
        target = tmp_path / "R.json"
        reconciliation_state.record_result(clean=True, mismatch_count=0, now=NOW,
                                           path=target)
        assert evaluate(target).clean is True

    def test_a_naive_timestamp_no_longer_crashes_the_legacy_reader(self, tmp_path):
        """is_current_and_clean() used to raise TypeError out of a
        fail-closed check; it must return False instead."""
        path = write_snapshot(tmp_path, checked_at="2026-08-04T15:00:00")
        assert reconciliation_state.is_current_and_clean(
            max_age_seconds=300, now=NOW, path=path) is False


# =====================================================================
# Configuration bounds.
# =====================================================================

class TestConfiguration:
    @pytest.mark.parametrize("value", ["0", "-1", "1.5", "abc", "3601", "999999"])
    def test_a_bad_ttl_is_a_configuration_error(self, tmp_path, monkeypatch, value):
        monkeypatch.setenv(freshness.ENV_MAX_AGE, value)
        with pytest.raises(freshness.SnapshotUnusable) as e:
            evaluate(write_snapshot(tmp_path))
        assert e.value.reason_code == "RECONCILIATION_FRESHNESS_CONFIG_INVALID"

    @pytest.mark.parametrize("value", ["0", "-5", "abc", "301"])
    def test_a_bad_skew_is_a_configuration_error(self, tmp_path, monkeypatch, value):
        monkeypatch.setenv(freshness.ENV_MAX_FUTURE_SKEW, value)
        with pytest.raises(freshness.SnapshotUnusable) as e:
            evaluate(write_snapshot(tmp_path))
        assert e.value.reason_code == "RECONCILIATION_FRESHNESS_CONFIG_INVALID"

    @pytest.mark.parametrize("value", ["", "   "])
    def test_a_blank_value_uses_the_documented_default(self, tmp_path, monkeypatch,
                                                        value):
        monkeypatch.setenv(freshness.ENV_MAX_AGE, value)
        assert evaluate(write_snapshot(tmp_path)).max_age_seconds == 900

    def test_the_ttl_ceiling_is_an_hour(self):
        assert freshness.MAX_ALLOWED_MAX_AGE_SECONDS == 3600

    def test_the_reconciler_runs_far_more_often_than_the_ttl(self):
        """A TTL shorter than the reconciliation cadence would block
        normal operation; this asserts the two are consistent."""
        timer = (REPO_ROOT / "deploy" / "systemd"
                 / "us-stock-trading-reconcile.timer").read_text(encoding="utf-8")
        assert "OnUnitActiveSec=2min" in timer
        assert freshness.DEFAULT_MAX_AGE_SECONDS >= 4 * 120


# =====================================================================
# The CLI both callers use.
# =====================================================================

def run_checker(snapshot, *args, **env):
    return subprocess.run(
        [sys.executable, str(CHECKER), *args],
        env={**os.environ, "RECONCILIATION_STATE_FILE": str(snapshot),
             "PYTHONPATH": str(REPO_ROOT), **env},
        capture_output=True, text=True, timeout=120)


class TestTheSharedChecker:
    def test_a_fresh_snapshot_exits_zero(self, tmp_path):
        result = run_checker(write_snapshot(tmp_path, checked_at=datetime.now(timezone.utc)))
        assert result.returncode == 0, result.stderr
        assert "RECONCILIATION CHECK OK" in result.stdout

    def test_a_stale_snapshot_exits_one(self, tmp_path):
        old = datetime.now(timezone.utc) - timedelta(days=30)
        result = run_checker(write_snapshot(tmp_path, checked_at=old))
        assert result.returncode == 1
        assert "RECONCILIATION_SNAPSHOT_STALE" in result.stderr

    def test_the_failure_log_carries_the_required_fields(self, tmp_path):
        old = datetime.now(timezone.utc) - timedelta(days=30)
        result = run_checker(write_snapshot(tmp_path, checked_at=old))
        assert "timer_enable_suppressed=true" in result.stderr
        assert "shadow_run_suppressed=true" in result.stderr

    def test_the_success_log_carries_the_age_and_limit(self, tmp_path):
        result = run_checker(write_snapshot(tmp_path, checked_at=datetime.now(timezone.utc)))
        assert "snapshot_age_seconds=" in result.stdout
        assert "max_age_seconds=900" in result.stdout
        assert "future_skew_seconds=30" in result.stdout

    def test_it_never_prints_a_path_or_an_account(self, tmp_path):
        old = datetime.now(timezone.utc) - timedelta(days=30)
        snapshot = write_snapshot(tmp_path, checked_at=old)
        result = run_checker(snapshot)
        combined = result.stdout + result.stderr
        assert str(snapshot) not in combined
        assert str(tmp_path) not in combined

    def test_a_missing_snapshot_exits_one(self, tmp_path):
        result = run_checker(tmp_path / "absent.json")
        assert result.returncode == 1
        assert "RECONCILIATION_SNAPSHOT_MISSING" in result.stderr


class TestBothCallersUseTheSameCheck:
    def test_the_approval_script_runs_the_shared_checker(self):
        source = (REPO_ROOT / "scripts" / "enable_oracle_shadow_timer.sh").read_text(
            encoding="utf-8")
        assert "check_reconciliation_freshness.py" in source
        assert "--require-unknown-zero" in source
        assert "--require-halt-clear" in source

    def test_the_shadow_unit_runs_it_before_every_start(self):
        unit = (REPO_ROOT / "deploy" / "systemd"
                / "us-stock-trading-shadow.service").read_text(encoding="utf-8")
        pre = [l for l in unit.splitlines() if l.startswith("ExecStartPre=")]
        assert any("check_reconciliation_freshness.py" in l for l in pre), pre

    def test_the_shadow_entrypoint_checks_it_too(self):
        """A manual run does not go through systemd at all."""
        source = (REPO_ROOT / "scripts" / "run_shadow_mode.py").read_text(encoding="utf-8")
        assert "freshness.evaluate()" in source
        assert "EXIT_STALE_RECONCILIATION" in source

    def test_the_freshness_check_precedes_any_enable_in_the_script(self):
        source = (REPO_ROOT / "scripts" / "enable_oracle_shadow_timer.sh").read_text(
            encoding="utf-8")
        check_at = source.index("check_reconciliation_freshness.py")
        enable_at = source.index('enable "${TARGET_TIMER}"')
        assert check_at < enable_at

    def test_no_second_copy_of_the_policy_exists(self):
        """The inline heredoc that used to hold this logic was invisible
        to the test suite, which is how the defect survived."""
        source = (REPO_ROOT / "scripts" / "enable_oracle_shadow_timer.sh").read_text(
            encoding="utf-8")
        assert "PYCHECK" not in source
        code = [l for l in source.splitlines() if not l.strip().startswith("#")]
        assert not [l for l in code if "checked_at" in l], code
