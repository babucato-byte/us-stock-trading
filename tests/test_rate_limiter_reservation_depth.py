"""The limiter reported its own correct behaviour as corruption.

2026-09-01. Seven READ consumers ran concurrently -- four scanners, two
reconciliation passes, the collector -- and the shared limiter began
raising:

    KIS rate-limit timestamp for READ is 8.6s in the future

The clock was fine: NTP synchronised and active. Every one of those
timestamps was the limiter's own work. Each reservation moves the stored
time forward by exactly one interval, so N queued callers put it N
intervals ahead; the guard allowed one interval plus skew -- 8s for READ
-- which tolerates two callers and rejects the third at 9s.

It aborted 81 reconciliation passes, 24 scans and one exit-monitor tick.
An exit that cannot read is the one failure this system cannot accept,
which is why the horizon is now derived from reservation depth rather
than from a tolerance chosen to silence the error.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from brokers import kis_rate_limiter as rl  # noqa: E402

READ = rl.CATEGORY_READ
INTERVAL = rl.DEFAULT_READ_MIN_INTERVAL   # 3.0
SKEW = rl.DEFAULT_MAX_CLOCK_SKEW          # 5.0


class TestTheHorizonIsDerivedNotGuessed:
    def test_it_comes_from_depth_times_interval_plus_skew(self):
        assert rl._future_horizon(INTERVAL) == pytest.approx(
            INTERVAL * rl.max_reservation_depth() + SKEW)

    def test_the_old_bound_admitted_only_two_queued_callers(self):
        """The defect, stated as arithmetic: the third caller was rejected."""
        old_bound = INTERVAL + SKEW
        assert 2 * INTERVAL < old_bound      # two queued: accepted
        assert 3 * INTERVAL > old_bound      # three queued: rejected at 9s

    def test_the_observed_failures_are_inside_the_new_horizon(self):
        """8.1s and 8.6s were real, legitimate reservations."""
        for observed in (8.1, 8.6, 9.0):
            assert observed < rl._future_horizon(INTERVAL)

    def test_seven_concurrent_consumers_fit(self):
        """The exact production concurrency that broke it."""
        assert 7 * INTERVAL < rl._future_horizon(INTERVAL)

    def test_the_depth_is_configurable_and_sane(self, monkeypatch):
        monkeypatch.setenv(rl.RESERVATION_DEPTH_ENV, "4")
        assert rl.max_reservation_depth() == 4
        assert rl._future_horizon(INTERVAL) == pytest.approx(4 * INTERVAL + SKEW)

    def test_a_nonsense_depth_falls_back(self, monkeypatch):
        monkeypatch.setenv(rl.RESERVATION_DEPTH_ENV, "0")
        assert rl.max_reservation_depth() == rl.DEFAULT_MAX_RESERVATION_DEPTH


class TestCorruptionIsStillDetected:
    def test_a_timestamp_beyond_any_possible_queue_still_fails(self):
        """Bounded, not disabled. An hour ahead is a clock, not a queue."""
        horizon = rl._future_horizon(INTERVAL)
        assert 3600 > horizon
        assert 86400 > horizon

    def test_raising_skew_was_not_the_fix(self):
        """max_clock_skew still means clock skew, and is untouched."""
        assert rl.max_clock_skew() == SKEW

    def test_the_horizon_scales_with_the_category_interval(self):
        """TOKEN paces at 60s; its horizon must not be READ's."""
        assert rl._future_horizon(60.0) > rl._future_horizon(INTERVAL)


class TestPacingIsUnchanged:
    def test_the_read_interval_is_not_relaxed(self):
        """The fix must not increase the request rate."""
        assert rl.DEFAULT_READ_MIN_INTERVAL == 3.0

    def test_reservation_still_advances_by_exactly_one_interval(self):
        """`reserved = last + interval` -- spacing is the invariant."""
        source = (REPO_ROOT / "brokers" / "kis_rate_limiter.py").read_text()
        assert "reserved = last + interval" in source

    def test_the_slot_is_stored_before_the_request_goes_out(self):
        source = (REPO_ROOT / "brokers" / "kis_rate_limiter.py").read_text()
        block = source[source.index("reserved = last + interval"):]
        assert "self._store_state(path, state, category)" in block[:900]


class TestQueuedCallersAcrossTheDepth:
    @pytest.mark.parametrize("queued", [1, 2, 3, 5, 7, 10, 16])
    def test_a_legitimate_queue_never_reads_as_corruption(self, queued):
        """N reservations put the stored time N intervals ahead."""
        ahead = queued * INTERVAL
        assert ahead <= rl._future_horizon(INTERVAL), (
            f"{queued} queued callers ({ahead}s ahead) would be rejected")

    def test_one_past_the_believed_depth_is_refused(self):
        beyond = (rl.max_reservation_depth() + 1) * INTERVAL + SKEW + 1
        assert beyond > rl._future_horizon(INTERVAL)


def test_the_error_names_the_horizon_it_exceeded():
    """An operator seeing this must be able to tell queue depth from a
    broken clock without reading the source."""
    source = (REPO_ROOT / "brokers" / "kis_rate_limiter.py").read_text()
    assert "reservation horizon" in source
    assert 'detail="future_timestamp"' in source
