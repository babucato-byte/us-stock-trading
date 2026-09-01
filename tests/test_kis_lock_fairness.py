"""The pacing wait happens OUTSIDE the state lock.

2026-08-27. S6's new one-minute entry cycle polled KIS continuously.
S1's executor -- which runs every fifteen minutes and was holding a real
TX position -- logged
`account={'error': 'the shared KIS rate-limit lock could not be
acquired'}`, missed its 18:30 and 18:45 ticks, and at 19:10:07 its
watchdog set the kill switch to ENTRY_DISABLED:
"S1_POSITION_UNMANAGED: newest tick is 40.1 min old (limit 40) while
holding ['TX']". That blocks entries for every strategy, including the
one holding the position.

The cause was not that S6 did too much work. `_wait_locked` slept out
the whole pacing interval while holding the exclusive flock, so the lock
was standing in for the rate budget instead of guarding the state file.
With a 3s READ interval, a process issuing back-to-back reads held it
~3s at a time and re-took it immediately; a process asking occasionally
could lose that race for the entire 10s acquisition timeout and give up.

The lock now covers only the read-modify-write of the reservation.
Callers claim successive slots in the order they get the lock and then
wait for their own slot with the lock released, so the queue is fair and
hold time is file-I/O, not seconds. The SPACING between requests is
unchanged -- that is the part that must not move.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from brokers import kis_rate_limiter  # noqa: E402
from brokers.kis_rate_limiter import KisRateLimiter  # noqa: E402


class Clock:
    """Virtual time. Records what was slept and when, and whether the
    lock was held at the moment of each sleep."""

    def __init__(self):
        self.t = 1000.0
        self.slept = []
        self.slept_while_locked = []
        self.locked = False

    def time(self):
        return self.t

    def sleep(self, seconds):
        if seconds > 0:
            self.slept.append(seconds)
            if self.locked:
                self.slept_while_locked.append(seconds)
        self.t += seconds


#: The suite-wide conftest zeroes every pacing interval so tests do not
#: wait. Pacing is exactly what these tests are about, so they put a real
#: one back -- otherwise `wait()` takes its "pacing is off" early return
#: and never touches the lock at all.
INTERVAL = 3.0


@pytest.fixture(autouse=True)
def _real_read_interval(monkeypatch):
    monkeypatch.setenv("KIS_READ_MIN_INTERVAL_SECONDS", str(INTERVAL))
    kis_rate_limiter.reset_limiter()
    yield
    kis_rate_limiter.reset_limiter()


def _limiter(tmp_path, clock, name="kis_rate_limit.json"):
    limiter = KisRateLimiter(path=tmp_path / name, clock=clock.time,
                             sleeper=clock.sleep)
    # The stored timestamps come from `_wall`, so the virtual clock has
    # to drive that too. Leaving it on real time would freeze `now` while
    # the test's clock advanced, and the arithmetic under test is exactly
    # the relationship between the two.
    limiter._wall = clock.time
    return limiter


class TestThePacingWaitIsNotUnderTheLock:
    def test_the_second_caller_waits_outside_the_lock(self, tmp_path, monkeypatch):
        """The whole point. If this regresses, a busy caller starves an
        occasional one and a watchdog eventually stops all trading."""
        clock = Clock()
        limiter = _limiter(tmp_path, clock)

        real_acquire = limiter._acquire
        real_release = limiter._release

        def acquire(handle):
            got = real_acquire(handle)
            clock.locked = clock.locked or got
            return got

        def release(handle, acquired, category):
            out = real_release(handle, acquired, category)
            clock.locked = False
            return out

        monkeypatch.setattr(limiter, "_acquire", acquire)
        monkeypatch.setattr(limiter, "_release", release)

        limiter.wait(category=kis_rate_limiter.CATEGORY_READ)
        limiter.wait(category=kis_rate_limiter.CATEGORY_READ)

        assert clock.slept, "the second call should have been paced"
        assert clock.slept_while_locked == [], (
            "the pacing wait happened while the state lock was held")

    def test_the_spacing_between_requests_is_unchanged(self, tmp_path):
        """Fairness must not have been bought with a weaker rate limit."""
        clock = Clock()
        limiter = _limiter(tmp_path, clock)
        limiter.wait(category=kis_rate_limiter.CATEGORY_READ)
        first = clock.t
        limiter.wait(category=kis_rate_limiter.CATEGORY_READ)
        assert clock.t - first == pytest.approx(INTERVAL, abs=0.05)

    def test_a_first_call_still_does_not_wait(self, tmp_path):
        clock = Clock()
        limiter = _limiter(tmp_path, clock)
        assert limiter.wait(category=kis_rate_limiter.CATEGORY_READ) == 0.0
        assert clock.slept == []


class TestSlotsAreHandedOutInOrder:
    def test_two_processes_take_successive_slots(self, tmp_path):
        """Two limiters over one state file, as two crons are. Each takes
        its own slot rather than both waiting on the same one."""
        clock = Clock()
        a = _limiter(tmp_path, clock)
        b = _limiter(tmp_path, clock)
        a.wait(category=kis_rate_limiter.CATEGORY_READ)
        start = clock.t
        b.wait(category=kis_rate_limiter.CATEGORY_READ)
        assert clock.t - start == pytest.approx(INTERVAL, abs=0.05)
        a.wait(category=kis_rate_limiter.CATEGORY_READ)
        assert clock.t - start == pytest.approx(2 * INTERVAL, abs=0.05)

    def test_a_reserved_future_slot_is_not_corruption(self, tmp_path):
        """A reservation is legitimately ahead of `now` by up to one
        interval. Reading that as a bad clock would fail closed on the
        normal case."""
        clock = Clock()
        a = _limiter(tmp_path, clock)
        b = _limiter(tmp_path, clock)
        a.wait(category=kis_rate_limiter.CATEGORY_READ)
        a.wait(category=kis_rate_limiter.CATEGORY_READ)
        # `a` has now reserved a slot in the future; `b` must queue
        # behind it rather than refuse.
        b.wait(category=kis_rate_limiter.CATEGORY_READ)

    def test_a_wildly_future_timestamp_is_still_corruption(self, tmp_path):
        """The guard must still catch a real clock fault -- it was only
        widened by one interval, not removed."""
        import json

        clock = Clock()
        limiter = _limiter(tmp_path, clock)
        path = tmp_path / "kis_rate_limit.json"
        limiter.wait(category=kis_rate_limiter.CATEGORY_READ)
        state = json.loads(path.read_text())
        state[kis_rate_limiter.CATEGORY_READ] = clock.t + 86400
        path.write_text(json.dumps(state))

        with pytest.raises(kis_rate_limiter.KISRateLimitStateInvalid):
            limiter.wait(category=kis_rate_limiter.CATEGORY_READ)


class TestTheReservationIsDurableBeforeTheLockDrops:
    def test_the_state_is_stored_inside_the_locked_section(self):
        """A slot handed to two callers would let both issue at the same
        instant, which is the thing the pacing exists to prevent."""
        source = (REPO_ROOT / "brokers" / "kis_rate_limiter.py").read_text(
            encoding="utf-8")
        locked = source[source.index("def _wait_locked"):
                        source.index("# -- artifacts")]
        assert "self._store_state(" in locked
        assert "self._sleeper(" not in locked, (
            "the pacing sleep is back inside the lock")


class TestContentionIsMeasurable:
    """§5. The starvation was diagnosed from one missing tick and an
    error line that said a lock could not be acquired -- not who held it,
    nor for how long. The next one should be arithmetic."""

    SOURCE = (REPO_ROOT / "brokers" / "kis_rate_limiter.py").read_text(
        encoding="utf-8")

    def test_wait_and_hold_are_both_recorded(self):
        assert "lock_wait_ms=" in self.SOURCE
        assert "lock_hold_ms=" in self.SOURCE

    def test_a_failed_acquisition_is_also_recorded(self):
        """The case that actually happened. If only successes are
        measured, the starving process is the one that leaves no trace."""
        assert "outcome=NOT_ACQUIRED" in self.SOURCE

    def test_telemetry_cannot_fail_a_paced_request(self):
        body = self.SOURCE[self.SOURCE.index("def _report_contention"):
                           self.SOURCE.index("def _release")]
        assert "except Exception" in body

    def test_the_owner_falls_back_to_the_entrypoint(self):
        """Several wrappers live outside the repository on the host, so a
        label that required editing them would be missing from exactly
        the processes whose contention most needs naming."""
        assert '"run_s1_live_cycle.py": "S1_EXECUTOR"' in self.SOURCE
        assert '"run_live_buy_entry.py": "S6_ENTRY"' in self.SOURCE

    def test_an_unlabelled_caller_is_unknown_not_guessed(self, monkeypatch):
        monkeypatch.delenv(kis_rate_limiter.OWNER_ENV, raising=False)
        monkeypatch.setattr("sys.argv", ["something_unmapped.py"])
        assert kis_rate_limiter.lock_owner() == "UNKNOWN"

    def test_the_environment_wins(self, monkeypatch):
        monkeypatch.setenv(kis_rate_limiter.OWNER_ENV, "S6_ENTRY")
        assert kis_rate_limiter.lock_owner() == "S6_ENTRY"


class TestANewEntryYieldsToEveryoneElse:
    """§2 and §6. A new BUY is the lowest-priority use of the broker."""

    def test_the_acquire_timeout_is_overridable(self, monkeypatch):
        monkeypatch.setenv(kis_rate_limiter.ACQUIRE_TIMEOUT_ENV, "1")
        assert kis_rate_limiter.acquire_timeout() == 1.0

    def test_the_default_patience_is_unchanged_for_everyone_else(self, monkeypatch):
        """Only the entry is impatient. An exit or a watchdog waiting a
        second and giving up would be the same defect pointed the other
        way."""
        monkeypatch.delenv(kis_rate_limiter.ACQUIRE_TIMEOUT_ENV, raising=False)
        assert kis_rate_limiter.acquire_timeout() == kis_rate_limiter._STATE_LOCK_TIMEOUT

    def test_the_entry_cron_asks_for_one_second(self):
        wrapper = (REPO_ROOT / "deploy" / "cron" / "s6_buy_entry.sh").read_text(
            encoding="utf-8")
        assert "KIS_LOCK_ACQUIRE_TIMEOUT_SECONDS=1" in wrapper
        assert "KIS_LOCK_OWNER=S6_ENTRY" in wrapper

    def test_the_exit_cron_does_not_shorten_its_patience(self):
        wrapper = (REPO_ROOT / "deploy" / "cron" / "s6_exit_monitor.sh").read_text(
            encoding="utf-8")
        assert "KIS_LOCK_OWNER=S6_EXIT" in wrapper
        assert "KIS_LOCK_ACQUIRE_TIMEOUT_SECONDS" not in wrapper

    def test_a_busy_broker_ends_the_tick_successfully(self):
        """Deferring is the design working, so it must not read as a
        failure -- and it must not be reached by a genuinely broken state
        file, which is a different fault."""
        runner = (REPO_ROOT / "scripts" / "run_live_buy_entry.py").read_text(
            encoding="utf-8")
        assert "ENTRY_DEFERRED_KIS_BUSY" in runner
        assert "REASON_LOCK_FAILED" in runner
        block = runner[runner.index("except kis_rate_limiter.KISRateLimitStateUnavailable"):]
        assert "return EXIT_ERROR" in block[:600]
        assert "return EXIT_OK" in block[:900]

    def test_a_deferred_tick_is_not_queued(self):
        runner = (REPO_ROOT / "scripts" / "run_live_buy_entry.py").read_text(
            encoding="utf-8")
        assert "dropped, not queued" in runner


class TestDiscoveryAndPositionManagementCannotBlockEachOther:
    """The 37-minute scan and the 1-minute position check share nothing.

    A full-universe pass now takes ~37 minutes against real KIS data. If
    it held the same lock the exit monitor takes, an open position would
    go unchecked for the length of a scan -- and the whole point of a
    per-minute monitor is that an exit never waits on discovery.

    Pinned on the wrappers because this is a deployment property, not a
    library one: the lock names live in the cron scripts, and the two
    production incidents in this area were both a cron pointing at the
    wrong file rather than a function behaving wrongly.
    """

    CRON = REPO_ROOT / "deploy" / "cron"

    def _lock_of(self, wrapper):
        import re
        text = (self.CRON / wrapper).read_text(encoding="utf-8")
        found = re.findall(r"flock[^\n]*?(/\S+\.lock)", text)
        assert found, f"{wrapper} takes no flock"
        return found[0]

    def test_discovery_and_the_exit_monitor_take_different_locks(self):
        assert self._lock_of("s6_scan.sh") != self._lock_of("s6_exit_monitor.sh")

    def test_the_exit_monitor_does_not_take_the_discovery_lock(self):
        """So a position check can never make a scan stand down."""
        assert "s6_scan.lock" not in (
            self.CRON / "s6_exit_monitor.sh").read_text(encoding="utf-8")

    def test_discovery_does_not_take_the_execution_lock(self):
        """So a 37-minute scan can never delay an exit."""
        assert "s6_exec.lock" not in (
            self.CRON / "s6_scan.sh").read_text(encoding="utf-8")

    def test_the_exit_monitor_runs_release_code_not_a_checkout(self):
        """Both production incidents here were a wrapper resolving a
        mutable checkout: the scan wrapper ran 2026-08-27 code with no
        credentials for days while reporting SUCCESS."""
        text = (self.CRON / "s6_exit_monitor.sh").read_text(encoding="utf-8")
        assert 'ROOT="${TRADING_PROJECT_ROOT:?}"' in text
        assert "/home/ubuntu/trading/" not in text

    def test_the_monitor_makes_no_broker_call_while_flat(self):
        """It must stay cheap enough to run every minute: no position
        means no KIS request at all, not a universe scan."""
        text = (self.CRON / "s6_exit_monitor.sh").read_text(encoding="utf-8")
        assert "s6_positions" in text
        assert "0)        exit 0" in text or "0) exit 0" in text
