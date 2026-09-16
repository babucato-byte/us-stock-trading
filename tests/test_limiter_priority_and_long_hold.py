"""The shared READ limiter must not let low-priority work strand an exit.

2026-09-16 ~21:43 KST. S6_BUY_EXECUTION held the shared READ lock for
21,252ms on CMG's pre-submit path. Reconciliation waited 12.3s and never
sent TTTS3018R; S6_EXIT waited 11.2s and never sent TTTS3035R. The
limiter recovered on its own, and the low-level cause of the hold was
never established because the only number recorded was the hold itself.

Two separate problems, tested separately here:

  * a hold that long is invisible until it has already hurt someone, so
    the phases inside the critical section are now timed and classified;
  * the reservation QUEUE can legitimately run 16 deep -- 48s at a 3s
    READ interval -- so a safety reader can arrive behind a wall of
    discovery reads and wait out its own timeout without anything
    misbehaving at all.

Nothing here changes pacing. Every priority obeys the same interval;
what differs is how deep a queue each is willing to add itself to.
"""

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from brokers import kis_rate_limiter as rl  # noqa: E402
from brokers.kis_rate_limiter import KisRateLimiter  # noqa: E402

READ = rl.CATEGORY_READ
INTERVAL = rl.DEFAULT_READ_MIN_INTERVAL


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def state(tmp_path):
    return tmp_path / "kis_rate_limit.json"


@pytest.fixture(autouse=True)
def _clean_owner(monkeypatch):
    monkeypatch.delenv(rl.OWNER_ENV, raising=False)
    monkeypatch.delenv(rl.LONG_LOCK_HOLD_ENV, raising=False)
    for name in rl.QUEUE_CAP_ENV_BY_PRIORITY.values():
        monkeypatch.delenv(name, raising=False)
    # tests/conftest.py zeroes every pacing interval so the suite does not
    # really wait. This file is ABOUT the queue that pacing creates, so it
    # restores the production READ interval -- with the interval at zero
    # `wait()` returns before it ever reaches a reservation.
    monkeypatch.setenv("KIS_READ_MIN_INTERVAL_SECONDS", str(INTERVAL))
    monkeypatch.setenv("KIS_TOKEN_MIN_INTERVAL_SECONDS",
                       str(rl.DEFAULT_TOKEN_MIN_INTERVAL))
    rl.metrics().reset()


def _limiter(path, clock):
    inst = KisRateLimiter(path=path, clock=clock.time, sleeper=clock.sleep)
    inst._wall = clock.time
    return inst


def _as(monkeypatch, owner):
    monkeypatch.setenv(rl.OWNER_ENV, owner)


def _executable_source(fn):
    """`fn`'s source with docstrings and comments stripped.

    A secrets check that reads prose fails on a docstring explaining
    that no secret is logged, which is the opposite of useful.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Module)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(getattr(body[0], "value", None), ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.dump(ast.fix_missing_locations(tree))


# -- 7. the normal case ---------------------------------------------------

class TestNormalReservation:
    """7. A short hold, and the numbers to prove it."""

    def test_the_hold_is_short_and_metrics_are_recorded(self, state, clock,
                                                        monkeypatch, caplog):
        _as(monkeypatch, "S6_EXIT")
        limiter = _limiter(state, clock)
        with caplog.at_level("DEBUG", logger="brokers.kis_rate_limiter"):
            slept = limiter.wait(category=READ)
        assert slept == 0.0, "an idle limiter reserves the current slot"
        recorded = rl.metrics().as_dict()
        assert recorded["limiter_acquisitions"] == 1
        assert recorded["limiter_long_holds"] == 0
        assert rl.LONG_LOCK_HOLD_EVENT not in caplog.text

    def test_the_phase_fields_account_for_the_hold(self):
        timer = rl._PhaseTimer()
        with timer.phase("STATE_LOAD_MS"):
            pass
        fields = timer.as_log_fields(hold_ms=500.0)
        for name in ("NAMESPACE_SCAN_MS", "STATE_LOAD_MS", "TEMP_WRITE_MS",
                     "FILE_FSYNC_MS", "RENAME_MS", "DIR_FSYNC_MS",
                     "OTHER_CRITICAL_MS"):
            assert name in fields

    def test_time_in_no_phase_is_reported_as_residual(self):
        """The number that separates a slow disk from a descheduled
        process -- the CMG incident could not tell them apart."""
        timer = rl._PhaseTimer()
        with timer.phase("FILE_FSYNC_MS"):
            pass
        fields = timer.as_log_fields(hold_ms=21252.0)
        residual = float(fields.split("OTHER_CRITICAL_MS=")[1])
        assert residual > 21000.0


# -- 8. a slow phase must name itself ------------------------------------

class TestLongHoldNamesThePhase:
    """8. The line the incident needed."""

    def test_a_slow_fsync_is_attributed_to_FILE_FSYNC_MS(self, state, clock,
                                                         monkeypatch, caplog):
        _as(monkeypatch, "S6_BUY_EXECUTION")
        limiter = _limiter(state, clock)

        real_fsync = os.fsync
        seen = {}

        def _slow_fsync(fd):
            # Advance the limiter's own clock, which is what the hold is
            # measured against -- no real sleeping in a unit test.
            clock.now += 21.252
            seen["fsynced"] = True
            return real_fsync(fd)

        monkeypatch.setattr(rl.os, "fsync", _slow_fsync)
        with caplog.at_level("ERROR", logger="brokers.kis_rate_limiter"):
            limiter.wait(category=READ)

        assert seen.get("fsynced") is True
        assert rl.LONG_LOCK_HOLD_EVENT in caplog.text
        assert "FILE_FSYNC_MS=" in caplog.text
        assert "owner=S6_BUY_EXECUTION" in caplog.text
        assert rl.metrics().as_dict()["limiter_long_holds"] == 1

    def test_a_hold_under_the_threshold_is_not_reported(self, state, clock,
                                                        monkeypatch, caplog):
        _as(monkeypatch, "S6_ENTRY")
        limiter = _limiter(state, clock)
        with caplog.at_level("ERROR", logger="brokers.kis_rate_limiter"):
            limiter.wait(category=READ)
        assert rl.LONG_LOCK_HOLD_EVENT not in caplog.text

    def test_the_threshold_is_below_the_acquisition_timeout(self):
        """It has to fire BEFORE it can make another caller fail, or it
        is only ever a post-mortem."""
        assert rl.long_lock_hold_ms() < rl._STATE_LOCK_TIMEOUT * 1000.0

    def test_the_threshold_override_falls_back_rather_than_disabling(
            self, monkeypatch):
        for bad in ("", "nonsense", "0", "-5"):
            monkeypatch.setenv(rl.LONG_LOCK_HOLD_ENV, bad)
            assert rl.long_lock_hold_ms() == rl.DEFAULT_LONG_LOCK_HOLD_MS

    def test_the_global_lock_timeout_was_not_increased(self):
        """B2 forbids solving this by waiting longer."""
        assert rl._STATE_LOCK_TIMEOUT == 10.0


# -- 9, 10, 14. fairness -------------------------------------------------

def _preload_queue(state, clock, depth):
    """Put `depth` reservations in front of whoever asks next."""
    import json

    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps(
        {"version": rl.STATE_VERSION, READ: clock.now + depth * INTERVAL}))


class TestExitIsNeverRefused:
    """9 and 14. The 2026-08-27 shape: a continuous low-priority consumer
    against a periodic safety reader."""

    @pytest.mark.parametrize("depth", [1, 2, 4, 8, 16])
    def test_exit_reserves_at_any_queue_depth(self, state, clock, monkeypatch,
                                              depth):
        _preload_queue(state, clock, depth)
        _as(monkeypatch, "S6_EXIT")
        limiter = _limiter(state, clock)
        slept = limiter.wait(category=READ)      # must not raise
        assert slept > 0
        assert rl.metrics().as_dict()["limiter_queue_deferrals"] == 0

    @pytest.mark.parametrize("owner", ["S6_EXIT", "RECONCILIATION",
                                       "S1_WATCHDOG"])
    def test_every_p0_owner_is_uncapped(self, owner):
        assert rl.priority_for_owner(owner) == rl.PRIORITY_P0
        assert rl.queue_cap_seconds(rl.PRIORITY_P0, INTERVAL) is None

    def test_a_scanner_declines_the_deep_queue_it_would_have_joined(
            self, state, clock, monkeypatch, caplog):
        """The mechanism that leaves the slot for the exit."""
        _preload_queue(state, clock, 8)          # 24s deep
        _as(monkeypatch, "SCANNER")
        limiter = _limiter(state, clock)
        with caplog.at_level("WARNING", logger="brokers.kis_rate_limiter"):
            with pytest.raises(rl.KISRateLimitQueueTooDeep) as caught:
                limiter.wait(category=READ)
        assert caught.value.reason_code == rl.REASON_QUEUE_TOO_DEEP
        assert rl.QUEUE_DEFERRED_EVENT in caplog.text
        assert rl.metrics().as_dict()["limiter_queue_deferrals"] == 1

    def test_declining_leaves_the_queue_exactly_as_it_was(self, state, clock,
                                                          monkeypatch):
        """The slot must still be there for the next caller -- otherwise
        this would be a refusal that also cost a reservation."""
        _preload_queue(state, clock, 8)
        before = state.read_text()
        _as(monkeypatch, "SCANNER")
        with pytest.raises(rl.KISRateLimitQueueTooDeep):
            _limiter(state, clock).wait(category=READ)
        assert state.read_text() == before

        # ...and the exit that arrives next still gets served.
        _as(monkeypatch, "S6_EXIT")
        assert _limiter(state, clock).wait(category=READ) > 0

    def test_the_scanner_still_makes_progress_on_a_shallow_queue(
            self, state, clock, monkeypatch):
        """B5: bounded progress. Discovery is deprioritised, not stopped."""
        _preload_queue(state, clock, 1)          # 3s deep, inside the P2 cap
        _as(monkeypatch, "SCANNER")
        assert _limiter(state, clock).wait(category=READ) > 0


class TestReconciliationIsNotStarved:
    """10. Reconciliation is P0 for the same reason an exit is: the order
    gates read its verdict to decide whether the account is even known."""

    def test_reconciliation_reserves_behind_a_full_discovery_queue(
            self, state, clock, monkeypatch):
        _preload_queue(state, clock, 16)
        _as(monkeypatch, "RECONCILIATION")
        assert _limiter(state, clock).wait(category=READ) > 0

    def test_a_buy_validation_loop_cannot_monopolise(self, state, clock,
                                                     monkeypatch):
        """B5: BUY cannot monopolise reservations. P1 is capped too --
        generously, so ordinary pre-submit validation never trips it."""
        _preload_queue(state, clock, 2)          # 6s: inside the P1 cap
        _as(monkeypatch, "S6_BUY_EXECUTION")
        assert _limiter(state, clock).wait(category=READ) > 0

        _preload_queue(state, clock, 8)          # 24s: beyond it
        with pytest.raises(rl.KISRateLimitQueueTooDeep):
            _limiter(state, clock).wait(category=READ)

    def test_an_unmapped_owner_is_not_treated_as_low_priority(self):
        """Being wrong towards P2 would let an unlabelled safety reader
        be refused; being wrong towards P1 only costs queue depth."""
        assert rl.priority_for_owner("SOME_NEW_JOB") == rl.PRIORITY_P1

    def test_health_and_reporting_yield_first(self):
        assert rl.priority_for_owner("HEALTH_CHECK") == rl.PRIORITY_P3
        assert (rl.queue_cap_seconds(rl.PRIORITY_P3, INTERVAL)
                < rl.queue_cap_seconds(rl.PRIORITY_P2, INTERVAL)
                < rl.queue_cap_seconds(rl.PRIORITY_P1, INTERVAL))


# -- 11. pacing is untouched ---------------------------------------------

class TestPacingUnchanged:
    """11. Priority reorders a queue; it never buys extra quota."""

    def test_the_read_interval_is_unchanged(self):
        assert rl.DEFAULT_READ_MIN_INTERVAL == 3.0

    def test_the_reservation_still_advances_by_exactly_one_interval(
            self, state, clock, monkeypatch):
        _as(monkeypatch, "S6_EXIT")
        limiter = _limiter(state, clock)
        limiter.wait(category=READ)
        import json

        first = json.loads(state.read_text())[READ]
        limiter.wait(category=READ)
        second = json.loads(state.read_text())[READ]
        assert second - first == pytest.approx(INTERVAL)

    def test_a_p0_caller_waits_out_its_slot_like_everyone_else(
            self, state, clock, monkeypatch):
        """Uncapped means "may join the queue", not "may skip it"."""
        _preload_queue(state, clock, 4)
        _as(monkeypatch, "S6_EXIT")
        slept = _limiter(state, clock).wait(category=READ)
        # The preloaded value is the slot the PREVIOUS caller took, so
        # the next free one is one interval beyond it: four ahead means
        # this caller waits five.
        assert slept == pytest.approx(5 * INTERVAL)

    def test_the_cap_is_counted_in_intervals_not_seconds(self):
        """TOKEN is paced at 60s, so an absolute second cap would refuse
        the first legitimately queued token caller."""
        token_cap = rl.queue_cap_seconds(rl.PRIORITY_P2,
                                         rl.DEFAULT_TOKEN_MIN_INTERVAL)
        assert token_cap >= rl.DEFAULT_TOKEN_MIN_INTERVAL


# -- 12. fail closed ------------------------------------------------------

class TestFailClosedUnchanged:
    """12. A caller that could not reserve must not issue the request."""

    def test_a_deferred_caller_raises_rather_than_returning(self, state, clock,
                                                            monkeypatch):
        _preload_queue(state, clock, 8)
        _as(monkeypatch, "SCANNER")
        with pytest.raises(rl.KISRateLimitStateUnavailable):
            _limiter(state, clock).wait(category=READ)

    def test_the_deferral_is_a_subclass_every_caller_already_handles(self):
        assert issubclass(rl.KISRateLimitQueueTooDeep,
                          rl.KISRateLimitStateUnavailable)

    def test_an_unacquirable_lock_still_fails_closed(self, state, clock,
                                                     monkeypatch):
        limiter = _limiter(state, clock)
        monkeypatch.setattr(limiter, "_acquire", lambda handle: False)
        with pytest.raises(rl.KISRateLimitStateUnavailable) as caught:
            limiter.wait(category=READ)
        assert caught.value.reason_code == rl.REASON_LOCK_FAILED
        assert rl.metrics().as_dict()["limiter_acquire_failures"] == 1


# -- 13. telemetry carries no secrets -------------------------------------

class TestTelemetryCarriesNoSecrets:
    """13."""

    def test_no_credential_or_account_field_is_logged(self, state, clock,
                                                      monkeypatch, caplog):
        monkeypatch.setenv("KIS_APP_KEY", "APPKEY-SHOULD-NEVER-APPEAR")
        monkeypatch.setenv("KIS_APP_SECRET", "SECRET-SHOULD-NEVER-APPEAR")
        monkeypatch.setenv("KIS_ACCOUNT_NO", "12345678")
        _as(monkeypatch, "S6_EXIT")
        limiter = _limiter(state, clock)
        with caplog.at_level("DEBUG", logger="brokers.kis_rate_limiter"):
            limiter.wait(category=READ)
        for forbidden in ("APPKEY-SHOULD-NEVER-APPEAR",
                          "SECRET-SHOULD-NEVER-APPEAR", "12345678"):
            assert forbidden not in caplog.text

    def test_the_telemetry_fields_are_a_closed_set(self):
        """Read off the CODE, not the prose around it.

        An owner label, a category, a pid, a threshold and durations --
        nothing that identifies the account. Checked against executable
        source only, because the docstring legitimately says the word
        "token" while explaining that no token is logged.
        """
        code = _executable_source(rl.KisRateLimiter._report_contention)
        for forbidden in ("app_key", "app_secret", "account_no", "token",
                          "CANO", "ACNT"):
            assert forbidden not in code.lower()

    def test_the_deferral_line_carries_no_account_data(self):
        code = _executable_source(rl.KisRateLimiter._enforce_queue_cap)
        for forbidden in ("app_key", "app_secret", "account_no", "cano"):
            assert forbidden not in code.lower()


# -- B4: the critical section holds only limiter state work ---------------

class TestCriticalSectionIsMinimal:
    """B4. Confirmed by reading it, not assumed."""

    def test_no_network_call_inside_the_lock(self):
        import inspect

        for fn in (rl.KisRateLimiter._wait_locked,
                   rl.KisRateLimiter._store_state,
                   rl.KisRateLimiter._load_state):
            body = inspect.getsource(fn)
            for forbidden in ("requests.", "urlopen", "http", "session.",
                              ".get(", ".post("):
                if forbidden == ".get(":
                    continue          # dict.get is not a network call
                assert forbidden not in body, f"{fn.__name__} -> {forbidden}"

    def test_the_pacing_sleep_happens_outside_the_lock(self):
        """Moved there after 2026-08-27; the sleep must never be back
        inside, or the lock becomes the pacing budget again."""
        import inspect

        body = inspect.getsource(rl.KisRateLimiter.wait)
        assert "OUTSIDE the lock" in body
        assert body.index("self._release") < body.index("self._sleeper(slept)")

    def test_the_sleep_is_not_called_while_locked(self, state, clock,
                                                  monkeypatch):
        _preload_queue(state, clock, 2)
        _as(monkeypatch, "S6_EXIT")
        limiter = _limiter(state, clock)
        limiter.wait(category=READ)
        assert clock.slept, "the caller still waits out its slot"
