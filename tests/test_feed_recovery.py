"""What is safe to do while the feed is broken, and how to come back.

A disconnect makes new entries unsafe immediately. It does NOT make it
safe to stop watching a position already held: an OPEN position has a
stop and an EXIT_PENDING one has a sell that must go out. Blocking
everything equally trades the risk of a bad entry for the risk of an
unmanaged holding, which is the worse of the two.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s6_live import feed_recovery as fr  # noqa: E402


class TestEntriesStopButExitsDoNot:
    def test_a_stale_feed_refuses_new_entries(self):
        assert fr.entries_permitted(fr.DATA_STALE) is False

    def test_degraded_data_also_refuses_new_entries(self):
        """A REST fallback price is good enough to manage a holding and
        not good enough to open one."""
        assert fr.entries_permitted(fr.DATA_DEGRADED) is False

    def test_only_a_live_feed_permits_entries(self):
        assert fr.entries_permitted(fr.DATA_LIVE) is True

    @pytest.mark.parametrize("status",
                             [fr.DATA_LIVE, fr.DATA_DEGRADED, fr.DATA_STALE])
    def test_exit_management_is_never_blocked(self, status):
        """The asymmetry that is the whole point of the module."""
        assert fr.exit_management_permitted(status) is True

    def test_the_asymmetry_holds_in_the_summary(self):
        out = fr.describe(feed_live=False, fallback_available=True)
        assert out["entries_permitted"] is False
        assert out["exit_management_permitted"] is True


class TestNamingWhatTheDataIsWorth:
    def test_a_live_feed_is_live(self):
        assert fr.data_quality(feed_live=True,
                               fallback_available=False) == fr.DATA_LIVE

    def test_a_fallback_is_degraded_not_stale(self):
        """Degraded is not an error. It is a statement about what the
        numbers are worth, so nothing downstream mistakes a REST price
        for a streaming one."""
        assert fr.data_quality(feed_live=False,
                               fallback_available=True) == fr.DATA_DEGRADED

    def test_no_feed_and_no_fallback_is_stale(self):
        assert fr.data_quality(feed_live=False,
                               fallback_available=False) == fr.DATA_STALE


class TestComingBackIsOrdered:
    """Reconnecting is not "the socket is up". A rebuilt subscription
    with indicators computed across the hole reads LIVE and is wrong."""

    def test_the_first_step_is_reconnecting(self):
        assert fr.next_step([]) == fr.STEP_RECONNECT

    def test_each_step_follows_the_last(self):
        assert fr.next_step([fr.STEP_RECONNECT]) == fr.STEP_RESUBSCRIBE
        assert fr.next_step([fr.STEP_RECONNECT, fr.STEP_RESUBSCRIBE]) \
            == fr.STEP_BACKFILL

    def test_a_finished_sequence_has_no_next_step(self):
        assert fr.next_step(list(fr.RECOVERY_ORDER)) is None
        assert fr.recovery_complete(list(fr.RECOVERY_ORDER)) is True

    def test_skipping_a_step_is_not_complete(self):
        """Backfilling without rebuilding subscriptions leaves a symbol
        whose stream is not actually flowing."""
        skipped = [fr.STEP_RECONNECT, fr.STEP_BACKFILL, fr.STEP_RECOMPUTE,
                   fr.STEP_VALIDATE]
        assert fr.recovery_complete(skipped) is False
        assert fr.next_step(skipped) == fr.STEP_RESUBSCRIBE

    def test_an_out_of_order_sequence_is_not_complete(self):
        reordered = [fr.STEP_RESUBSCRIBE, fr.STEP_RECONNECT, fr.STEP_BACKFILL,
                     fr.STEP_RECOMPUTE, fr.STEP_VALIDATE]
        assert fr.recovery_complete(reordered) is False

    def test_nothing_done_is_not_complete(self):
        assert fr.recovery_complete(None) is False


class TestWatchableNeedsBothTheSequenceAndASoundHistory:
    def test_a_full_sequence_with_sound_history_is_watchable(self):
        assert fr.watchable_after_recovery(list(fr.RECOVERY_ORDER),
                                           integrity_sound=True) is True

    def test_a_full_sequence_with_a_broken_history_is_not(self):
        """The backfill can complete and still leave a gappy history."""
        assert fr.watchable_after_recovery(list(fr.RECOVERY_ORDER),
                                           integrity_sound=False) is False

    def test_a_sound_history_without_the_sequence_is_not(self):
        assert fr.watchable_after_recovery([fr.STEP_RECONNECT],
                                           integrity_sound=True) is False
