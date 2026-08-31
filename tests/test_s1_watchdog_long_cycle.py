"""A running cycle is not a silent one.

The false positive
------------------
The watchdog measures silence against the newest `started_at` in the S1
cycle log. That record is appended only when `run_cycle()` RETURNS, so
its view lagged by one whole cycle -- and cycles now take 14-17 minutes
against a 15-minute schedule.

Production, 2026-08-31 (all UTC):

    14:00:05  cycle starts      -> completes 14:17:01  (17 min)
    14:15     invocation SKIPPED -- flock -n, 14:00 still running
    14:30:05  cycle starts      -> completes 14:44:49
    14:40:08  watchdog checks   -> newest recorded start = 14:00:05
                                   40.05 min > 40 -> ENTRY_DISABLED

TX was being actively managed by the cycle then in flight. The account
lost all new entries for a false reading, and the identical thing had
already happened on 2026-08-27 ("40.1 min old").

The fix records the start when it happens. It does not weaken the
check: a start marker's timestamp never advances, so a HUNG cycle still
crosses the threshold -- which is the case the watchdog exists for.
"""

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location(
    "run_s1_position_watchdog",
    REPO_ROOT / "scripts" / "run_s1_position_watchdog.py")
watchdog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watchdog)

DAY = "2026-08-31"


def _write(directory, records):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"cycles-{DAY}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    return path


def _at(hh, mm, ss=5):
    return datetime(2026, 8, 31, hh, mm, ss, tzinfo=timezone.utc)


def _started(hh, mm, ss=5):
    return {"started_at": _at(hh, mm, ss).isoformat(), "trading_day": DAY,
            "phase": "CYCLE_STARTED"}


def _completed(hh, mm, ss=5):
    return {"started_at": _at(hh, mm, ss).isoformat(), "trading_day": DAY,
            "phase": "CYCLE_COMPLETED"}


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("S1_LIVE_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(watchdog, "log_dir", lambda: tmp_path)
    return tmp_path


class TestTheProductionTimeline:
    """The exact sequence that fired the kill switch."""

    def test_the_old_shape_would_have_looked_stale(self, log_dir):
        """Completion records only -- 14:15 skipped, 14:30 in flight and
        therefore unrecorded. This is what the watchdog actually read."""
        _write(log_dir, [_completed(13, 0), _completed(13, 15),
                         _completed(13, 30), _completed(13, 45),
                         _completed(14, 0)])
        newest = watchdog.newest_tick_at(DAY)
        silence = (_at(14, 40, 8) - newest).total_seconds() / 60.0
        assert silence == pytest.approx(40.05, abs=0.01)
        assert silence > watchdog.DEFAULT_MAX_SILENCE_MINUTES

    def test_the_start_marker_makes_the_running_cycle_visible(self, log_dir):
        """Same timeline, with the 14:30 cycle's START recorded."""
        _write(log_dir, [_completed(13, 0), _completed(13, 15),
                         _completed(13, 30), _completed(13, 45),
                         _completed(14, 0), _started(14, 30)])
        newest = watchdog.newest_tick_at(DAY)
        silence = (_at(14, 40, 8) - newest).total_seconds() / 60.0
        assert silence == pytest.approx(10.05, abs=0.01)
        assert silence < watchdog.DEFAULT_MAX_SILENCE_MINUTES

    def test_the_healthy_active_case_does_not_trigger(self, log_dir, monkeypatch):
        """End to end through check(): a held position, a skipped 14:15,
        and a cycle in flight must NOT be S1_POSITION_UNMANAGED."""
        _write(log_dir, [_completed(13, 0), _completed(13, 15),
                         _completed(13, 30), _completed(13, 45),
                         _completed(14, 0), _started(14, 30)])
        monkeypatch.setattr(watchdog, "ticks_expected_now", lambda: True)
        result = _check_with_position(watchdog, monkeypatch, now=_at(14, 40, 8))
        assert result["status"] == watchdog.STATUS_HEALTHY

    def test_a_missing_interval_alone_is_not_stale(self, log_dir, monkeypatch):
        """14:15 skipped by flock is normal when a cycle overruns."""
        _write(log_dir, [_completed(14, 0), _started(14, 30),
                         _completed(14, 30)])
        monkeypatch.setattr(watchdog, "ticks_expected_now", lambda: True)
        result = _check_with_position(watchdog, monkeypatch, now=_at(14, 35))
        assert result["status"] == watchdog.STATUS_HEALTHY


class TestGenuineStalenessStillTriggers:
    """The fix must not blind the watchdog to what it exists for."""

    def test_a_hung_cycle_still_trips_it(self, log_dir, monkeypatch):
        """A start marker's timestamp never advances. A cycle that began
        at 14:00 and never finished is exactly an unmanaged position."""
        _write(log_dir, [_completed(13, 45), _started(14, 0)])
        monkeypatch.setattr(watchdog, "ticks_expected_now", lambda: True)
        result = _check_with_position(watchdog, monkeypatch, now=_at(14, 41))
        assert result["status"] == watchdog.STATUS_STALE
        assert result["silence_minutes"] > 40

    def test_a_stopped_executor_still_trips_it(self, log_dir, monkeypatch):
        """No records at all after 13:45."""
        _write(log_dir, [_completed(13, 30), _completed(13, 45)])
        monkeypatch.setattr(watchdog, "ticks_expected_now", lambda: True)
        result = _check_with_position(watchdog, monkeypatch, now=_at(14, 30))
        assert result["status"] == watchdog.STATUS_STALE

    def test_no_record_at_all_while_holding_still_trips_it(self, log_dir,
                                                           monkeypatch):
        _write(log_dir, [])
        monkeypatch.setattr(watchdog, "ticks_expected_now", lambda: True)
        result = _check_with_position(watchdog, monkeypatch, now=_at(14, 30))
        assert result["status"] == watchdog.STATUS_STALE

    def test_the_threshold_is_unchanged(self):
        """The bug is not fixed by widening the limit."""
        assert watchdog.DEFAULT_MAX_SILENCE_MINUTES == 40


class TestTheWriterEmitsTheMarker:
    def test_the_cycle_records_its_start_before_running(self):
        source = (REPO_ROOT / "scripts" / "run_s1_live_cycle.py").read_text()
        start_marker = source.index("PHASE_STARTED, \"dry_run\"")
        run_cycle = source.index("executor.run_cycle(")
        assert start_marker < run_cycle, (
            "the start marker must be written before the cycle runs, or it "
            "records nothing the watchdog did not already have")

    def test_both_phases_are_defined(self):
        source = (REPO_ROOT / "scripts" / "run_s1_live_cycle.py").read_text()
        assert 'PHASE_STARTED = "CYCLE_STARTED"' in source
        assert 'PHASE_COMPLETED = "CYCLE_COMPLETED"' in source

    def test_a_failed_marker_write_does_not_stop_the_cycle(self):
        """Bookkeeping must never cost a management cycle."""
        source = (REPO_ROOT / "scripts" / "run_s1_live_cycle.py").read_text()
        marker = source.index("could not record the cycle start marker")
        assert "except Exception" in source[:marker]


def _check_with_position(module, monkeypatch, *, now):
    """Run check() with one OPEN S1 position, without a real database."""
    class _State:
        symbol = "TX"

    row = {"status": "OPEN", "exit_submitted": 0}

    class _PS:
        @staticmethod
        def load_live(_conn):
            return [("s1pos_x", _State(), row)]

    class _Conn:
        def close(self):
            pass

    # Patch the ATTRIBUTE on the package, not sys.modules. `check()`
    # does `from s1_live import position_store`, which reads the
    # attribute once the package is imported -- so a sys.modules entry
    # wins only when nothing has imported it yet, making the test pass
    # alone and fail after any test that imports s1_live.
    import s1_live
    import state_store.db as sdb

    monkeypatch.setattr(s1_live, "position_store", _PS, raising=False)
    monkeypatch.setitem(sys.modules, "s1_live.position_store", _PS)
    monkeypatch.setattr(sdb, "open_db", lambda *a, **k: _Conn())
    return module.check(now=now)
