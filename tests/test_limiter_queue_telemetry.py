"""Who built the queue, recorded at the moment they built it.

The 2026-09-17 investigation could name the caller that was REFUSED --
S6_ENTRY, would_wait 12.8s against a 12.0s cap -- and could not name a
single caller that had built that 12.8s queue. `KIS_LOCK ... ACQUIRED`
reaches INFO only when wait or hold clears 500ms, and every reservation
in that window was sub-millisecond, so the queue's composition was
unrecoverable from the logs.

This closes that gap and nothing else: no cap, no priority, no interval
and no reservation rule is touched, and the tests below assert exactly
that as hard as they assert the new line.
"""

import json
import logging
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
def _paced(monkeypatch):
    # tests/conftest.py zeroes every interval so the suite never waits.
    # These tests are about the queue that pacing creates.
    monkeypatch.setenv("KIS_READ_MIN_INTERVAL_SECONDS", str(INTERVAL))
    monkeypatch.setenv("KIS_TOKEN_MIN_INTERVAL_SECONDS",
                       str(rl.DEFAULT_TOKEN_MIN_INTERVAL))
    monkeypatch.delenv(rl.OWNER_ENV, raising=False)
    for name in rl.QUEUE_CAP_ENV_BY_PRIORITY.values():
        monkeypatch.delenv(name, raising=False)
    rl.metrics().reset()


def _limiter(path, clock):
    inst = KisRateLimiter(path=path, clock=clock.time, sleeper=clock.sleep)
    inst._wall = clock.time
    return inst


def _as(monkeypatch, owner):
    monkeypatch.setenv(rl.OWNER_ENV, owner)


def _queue_lines(caplog):
    return [r for r in caplog.records
            if r.getMessage().startswith(rl.QUEUE_STATE_EVENT)]


def _fields(record):
    out = {}
    for token in record.getMessage().split():
        if "=" in token:
            key, _, value = token.partition("=")
            out[key] = value
    return out


# -- the gap that was closed ---------------------------------------------

class TestSuccessBelow500msIsVisible:

    def test_a_sub_threshold_read_acquisition_emits_at_info(
            self, state, clock, monkeypatch, caplog):
        """The whole point: this reservation is far below the 500ms
        notable threshold and must still be recorded."""
        _as(monkeypatch, "S6_ENTRY")
        with caplog.at_level(logging.INFO, logger="brokers.kis_rate_limiter"):
            _limiter(state, clock).wait(category=READ)

        lines = _queue_lines(caplog)
        assert len(lines) == 1
        assert lines[0].levelno == logging.INFO
        # and the OLD line stayed below its own threshold, unchanged
        acquired = [r for r in caplog.records
                    if "outcome=ACQUIRED" in r.getMessage()]
        assert acquired == [], "the existing notable-threshold line is untouched"

    def test_the_notable_threshold_itself_is_unchanged(self):
        assert rl._TELEMETRY_NOTABLE_MS == 500.0


# -- fields ---------------------------------------------------------------

class TestFields:

    def test_every_required_field_is_present(self, state, clock, monkeypatch,
                                             caplog):
        _as(monkeypatch, "S6_ENTRY")
        with caplog.at_level(logging.INFO, logger="brokers.kis_rate_limiter"):
            _limiter(state, clock).wait(category=READ)
        fields = _fields(_queue_lines(caplog)[0])
        for name in ("owner", "category", "priority", "pid", "wait_s",
                     "hold_ms", "reserved_before", "reserved_after",
                     "queue_depth_seconds", "queue_depth_slots", "interval_s"):
            assert name in fields, name
        assert fields["category"] == READ
        assert int(fields["pid"]) == os.getpid()
        assert float(fields["interval_s"]) == INTERVAL

    @pytest.mark.parametrize("owner,priority", [
        ("S6_ENTRY", "P1"), ("S6_BUY_EXECUTION", "P1"), ("SCANNER", "P2"),
        ("S6_COLLECTOR", "P2"), ("RECONCILIATION", "P0"), ("S6_EXIT", "P0"),
        ("HEALTH_CHECK", "P3")])
    def test_owner_and_priority_are_reported_correctly(
            self, state, clock, monkeypatch, caplog, owner, priority):
        _as(monkeypatch, owner)
        with caplog.at_level(logging.INFO, logger="brokers.kis_rate_limiter"):
            _limiter(state, clock).wait(category=READ)
        fields = _fields(_queue_lines(caplog)[0])
        assert fields["owner"] == owner
        assert fields["priority"] == priority

    def test_an_empty_queue_reports_no_predecessor(self, state, clock,
                                                   monkeypatch, caplog):
        _as(monkeypatch, "S6_ENTRY")
        with caplog.at_level(logging.INFO, logger="brokers.kis_rate_limiter"):
            _limiter(state, clock).wait(category=READ)
        fields = _fields(_queue_lines(caplog)[0])
        assert fields["reserved_before"] == "none"
        assert float(fields["reserved_after"]) == pytest.approx(clock.now)
        assert float(fields["queue_depth_seconds"]) == 0.0
        assert float(fields["queue_depth_slots"]) == 0.0

    def test_reserved_before_and_after_are_one_interval_apart(
            self, state, clock, monkeypatch, caplog):
        """The second caller sees the first caller's slot as `before` and
        leaves its own, one interval later, as `after`."""
        _as(monkeypatch, "S6_ENTRY")
        _limiter(state, clock).wait(category=READ)
        first = json.loads(state.read_text())[READ]

        _as(monkeypatch, "SCANNER")
        with caplog.at_level(logging.INFO, logger="brokers.kis_rate_limiter"):
            _limiter(state, clock).wait(category=READ)
        fields = _fields(_queue_lines(caplog)[-1])

        assert float(fields["reserved_before"]) == pytest.approx(first)
        assert float(fields["reserved_after"]) == pytest.approx(first + INTERVAL)
        assert float(fields["queue_depth_seconds"]) == pytest.approx(INTERVAL)
        assert float(fields["queue_depth_slots"]) == pytest.approx(1.0)

    def test_the_reported_depth_matches_the_persisted_state(
            self, state, clock, monkeypatch, caplog):
        """Emitted AFTER the state update, so it describes the queue this
        caller actually left behind."""
        _as(monkeypatch, "SCANNER")
        with caplog.at_level(logging.INFO, logger="brokers.kis_rate_limiter"):
            for _ in range(3):
                _limiter(state, clock).wait(category=READ)
        persisted = json.loads(state.read_text())[READ]
        fields = _fields(_queue_lines(caplog)[-1])
        assert float(fields["reserved_after"]) == pytest.approx(persisted)

    @pytest.mark.parametrize("depth", [1, 2, 3])
    def test_slots_track_the_queue_the_caps_are_expressed_in(
            self, state, clock, monkeypatch, caplog, depth):
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps(
            {"version": rl.STATE_VERSION, READ: clock.now + depth * INTERVAL}))
        _as(monkeypatch, "S6_ENTRY")
        with caplog.at_level(logging.INFO, logger="brokers.kis_rate_limiter"):
            _limiter(state, clock).wait(category=READ)
        fields = _fields(_queue_lines(caplog)[0])
        assert float(fields["queue_depth_slots"]) == pytest.approx(depth + 1)


# -- scope ----------------------------------------------------------------

class TestScope:

    def test_non_read_categories_stay_quiet(self, state, clock, monkeypatch,
                                            caplog):
        """TOKEN/ORDER/CANCEL are low volume and already legible; adding
        them would be noise without an answer."""
        _as(monkeypatch, "S6_ENTRY")
        with caplog.at_level(logging.INFO, logger="brokers.kis_rate_limiter"):
            _limiter(state, clock).wait(category=rl.CATEGORY_TOKEN)
        assert _queue_lines(caplog) == []

    def test_a_refused_caller_reports_no_queue_state(self, state, clock,
                                                     monkeypatch, caplog):
        """It changed nothing, so it has no resulting queue to describe --
        the QUEUE_DEFERRED line already covers it."""
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps(
            {"version": rl.STATE_VERSION, READ: clock.now + 8 * INTERVAL}))
        _as(monkeypatch, "SCANNER")
        with caplog.at_level(logging.INFO, logger="brokers.kis_rate_limiter"):
            with pytest.raises(rl.KISRateLimitQueueTooDeep):
                _limiter(state, clock).wait(category=READ)
        assert _queue_lines(caplog) == []
        assert any(rl.QUEUE_DEFERRED_EVENT in r.getMessage()
                   for r in caplog.records)

    def test_telemetry_failure_cannot_fail_the_request(self, state, clock,
                                                       monkeypatch):
        _as(monkeypatch, "S6_ENTRY")
        limiter = _limiter(state, clock)
        monkeypatch.setattr(rl, "priority_for_owner",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        assert limiter.wait(category=READ) == 0.0     # must not raise


# -- nothing else moved ---------------------------------------------------

class TestNoBehaviourChange:

    def test_no_cap_changed(self):
        assert rl.LOW_PRIORITY_QUEUE_CAP_INTERVALS == {1: 4.0, 2: 2.0, 3: 1.0}
        assert rl.queue_cap_seconds(rl.PRIORITY_P0, INTERVAL) is None
        assert rl.queue_cap_seconds(rl.PRIORITY_P1, INTERVAL) == 12.0
        assert rl.queue_cap_seconds(rl.PRIORITY_P2, INTERVAL) == 6.0
        assert rl.queue_cap_seconds(rl.PRIORITY_P3, INTERVAL) == 3.0

    def test_no_priority_mapping_changed(self):
        assert rl.PRIORITY_BY_OWNER == {
            "S6_EXIT": 0, "S1_WATCHDOG": 0, "RECONCILIATION": 0,
            "S6_BUY_EXECUTION": 1, "S6_ENTRY": 1, "S1_EXECUTOR": 1,
            "SCANNER": 2, "S6_COLLECTOR": 2,
            "HEALTH_CHECK": 3, "TRADING_REPORT": 3}
        assert rl.priority_for_owner("SOMETHING_NEW") == rl.PRIORITY_P1

    def test_no_rate_window_changed(self):
        assert rl.DEFAULT_READ_MIN_INTERVAL == 3.0
        assert rl.DEFAULT_TOKEN_MIN_INTERVAL == 60.0
        assert rl.DEFAULT_ORDER_MIN_INTERVAL == 1.0
        assert rl.DEFAULT_MAX_RESERVATION_DEPTH == 16
        assert rl._STATE_LOCK_TIMEOUT == 10.0
        assert rl.long_lock_hold_ms() == 1000.0

    def test_reservation_arithmetic_is_unchanged(self, state, clock,
                                                 monkeypatch):
        """Spacing is still exactly one interval per caller."""
        _as(monkeypatch, "S6_ENTRY")
        limiter = _limiter(state, clock)
        limiter.wait(category=READ)
        first = json.loads(state.read_text())[READ]
        limiter.wait(category=READ)
        second = json.loads(state.read_text())[READ]
        assert second - first == pytest.approx(INTERVAL)

    def test_telemetry_adds_no_reservation(self, state, clock, monkeypatch,
                                           caplog):
        """The line is written from values already computed; it must not
        move the queue by even one slot."""
        _as(monkeypatch, "SCANNER")
        with caplog.at_level(logging.INFO, logger="brokers.kis_rate_limiter"):
            _limiter(state, clock).wait(category=READ)
        after_one = json.loads(state.read_text())[READ]
        assert after_one == pytest.approx(clock.now)
        assert len(_queue_lines(caplog)) == 1, "one line, one reservation"

    def test_the_emitter_takes_no_lock_and_makes_no_call(self):
        """Checked against EXECUTABLE code: the docstring legitimately
        says "no lock, no file, no KIS call, no sleep" while explaining
        that none of them happen."""
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(
            inspect.getsource(KisRateLimiter._report_queue_state)))
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body \
                    and isinstance(body[0], ast.Expr) \
                    and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
        code = ast.dump(ast.fix_missing_locations(tree))
        for banned in ("flock", "_store_state", "_load_state", "requests",
                       "sleep", "os.replace", "mkdir"):
            assert banned not in code, banned

    def test_the_long_hold_alert_is_intact(self, state, clock, monkeypatch,
                                           caplog):
        _as(monkeypatch, "S6_BUY_EXECUTION")
        limiter = _limiter(state, clock)
        real_fsync = os.fsync

        def _slow(fd):
            clock.now += 21.252
            return real_fsync(fd)

        monkeypatch.setattr(rl.os, "fsync", _slow)
        with caplog.at_level(logging.INFO, logger="brokers.kis_rate_limiter"):
            limiter.wait(category=READ)
        assert any(rl.LONG_LOCK_HOLD_EVENT in r.getMessage()
                   for r in caplog.records)
        assert len(_queue_lines(caplog)) == 1, "both lines, not one instead"

    def test_fail_closed_on_an_unacquirable_lock_is_unchanged(
            self, state, clock, monkeypatch):
        limiter = _limiter(state, clock)
        monkeypatch.setattr(limiter, "_acquire", lambda handle: False)
        with pytest.raises(rl.KISRateLimitStateUnavailable) as caught:
            limiter.wait(category=READ)
        assert caught.value.reason_code == rl.REASON_LOCK_FAILED
