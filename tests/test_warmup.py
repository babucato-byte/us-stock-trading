"""Is there enough history behind this symbol to believe its indicators?

The failure being prevented is not a crash. A symbol subscribed at 15:42
has one bar, and every indicator can still be computed from it: an EMA
of one value is that value, a 20-bar volume baseline over one bar is
that bar, a session VWAP over one print is that print. Nothing raises,
nothing returns None, and every answer is meaningless -- an EMA9 equal
to the last price makes a symbol look like it is sitting exactly on its
average, and a volume baseline equal to the current bar makes expansion
either impossible or guaranteed depending on whether that first bar was
quiet.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import warmup_policy as policy  # noqa: E402
from market_data import realtime_bars as rb  # noqa: E402
from s6_live import warmup  # noqa: E402

ANCHOR = datetime(2026, 8, 31, 13, 30, tzinfo=timezone.utc)  # 09:30 ET


def _bars(count, *, start=ANCHOR, step=1, symbol="OWL", volume=100.0,
          close=10.0):
    return [rb.Bar(symbol=symbol, session="REGULAR",
                   minute=start + timedelta(minutes=i * step),
                   open=close, high=close, low=close, close=close,
                   volume=volume, trade_count=3,
                   first_trade_at=start, last_trade_at=start)
            for i in range(count)]


def _now_after(bars, seconds=30):
    return bars[-1].minute + timedelta(minutes=1, seconds=seconds)


class TestOnlyFinishedMinutesCount:
    def test_the_bar_in_progress_is_excluded(self):
        """Counting it satisfies the requirement a minute early, on a
        bar whose volume is a fraction of its final value."""
        bars = _bars(3)
        now = bars[-1].minute + timedelta(seconds=20)
        assert len(warmup.completed_bars(bars, now=now)) == 2

    def test_it_counts_once_the_minute_closes(self):
        bars = _bars(3)
        now = bars[-1].minute + timedelta(minutes=1)
        assert len(warmup.completed_bars(bars, now=now)) == 3

    def test_a_partial_bar_cannot_complete_a_warmup(self):
        """One bar short, with the in-progress bar making up the count."""
        needed = policy.longest_requirement()
        bars = _bars(needed)
        result = warmup.evaluate("OWL", bars=bars,
                                 now=bars[-1].minute + timedelta(seconds=30),
                                 session_anchor=ANCHOR)
        assert result["state"] == policy.STATE_WARMING_UP
        assert result["completed_bars"] == needed - 1


class TestANewSymbolDoesNotGoStraightToWatching:
    def test_one_bar_is_warming_up_not_watching(self):
        bars = _bars(1)
        result = warmup.evaluate("OWL", bars=bars, now=_now_after(bars),
                                 session_anchor=ANCHOR)
        assert result["state"] == policy.STATE_WARMING_UP
        assert policy.INSUFFICIENT_HISTORY in result["reasons"]

    def test_it_says_how_far_short_it_is(self):
        bars = _bars(10)
        result = warmup.evaluate("OWL", bars=bars, now=_now_after(bars),
                                 session_anchor=ANCHOR)
        assert result["short_by"] == policy.longest_requirement() - 10

    def test_a_full_sound_history_reaches_watching(self):
        bars = _bars(policy.longest_requirement())
        result = warmup.evaluate("OWL", bars=bars, now=_now_after(bars),
                                 session_anchor=ANCHOR)
        assert result["state"] == policy.STATE_WATCHING
        assert result["reasons"] == []
        assert warmup.may_watch(result) is True

    def test_waiting_for_bars_keeps_the_slot(self):
        """Short history is fixed by waiting. A slot given up here would
        be re-acquired a minute later having lost its stream."""
        bars = _bars(5)
        result = warmup.evaluate("OWL", bars=bars, now=_now_after(bars),
                                 session_anchor=ANCHOR)
        assert result["release_slot"] is False

    def test_each_feature_reports_its_own_sufficiency(self):
        bars = _bars(policy.required_bars("volume_baseline"))
        result = warmup.evaluate("OWL", bars=bars, now=_now_after(bars),
                                 session_anchor=ANCHOR)
        assert result["per_feature"]["volume_baseline"] is True
        assert result["per_feature"]["ema21"] is False


class TestABrokenHistoryFailsRatherThanWaits:
    """Waiting fixes a short history. It does not fix a wrong one, and a
    slot held by a symbol whose indicators cannot be trusted is worse
    than an empty one because it looks occupied."""

    def test_duplicate_timestamps_fail(self):
        bars = _bars(policy.longest_requirement())
        bars.append(bars[-1])
        result = warmup.evaluate("OWL", bars=bars, now=_now_after(bars),
                                 session_anchor=ANCHOR)
        assert result["state"] == policy.STATE_WARMUP_FAILED
        assert policy.DUPLICATE_TIMESTAMPS in result["reasons"]
        assert result["release_slot"] is True

    def test_out_of_order_timestamps_fail(self):
        bars = _bars(policy.longest_requirement())
        bars[10], bars[20] = bars[20], bars[10]
        result = warmup.evaluate("OWL", bars=bars, now=_now_after(bars),
                                 session_anchor=ANCHOR)
        assert policy.NON_MONOTONIC in result["reasons"]

    def test_a_high_below_its_low_fails(self):
        bars = _bars(policy.longest_requirement())
        bars[5] = rb.Bar(symbol="OWL", session="REGULAR",
                         minute=bars[5].minute, open=10.0, high=9.0, low=11.0,
                         close=10.0, volume=100.0, trade_count=1,
                         first_trade_at=bars[5].minute,
                         last_trade_at=bars[5].minute)
        result = warmup.evaluate("OWL", bars=bars, now=_now_after(bars),
                                 session_anchor=ANCHOR)
        assert policy.OHLC_INCONSISTENT in result["reasons"]

    def test_a_close_outside_its_range_fails(self):
        bars = _bars(policy.longest_requirement())
        bars[5] = rb.Bar(symbol="OWL", session="REGULAR",
                         minute=bars[5].minute, open=10.0, high=10.5, low=9.5,
                         close=99.0, volume=100.0, trade_count=1,
                         first_trade_at=bars[5].minute,
                         last_trade_at=bars[5].minute)
        result = warmup.evaluate("OWL", bars=bars, now=_now_after(bars),
                                 session_anchor=ANCHOR)
        assert policy.OHLC_INCONSISTENT in result["reasons"]

    def test_a_gappy_history_fails(self):
        """Every other minute missing: an average over it describes a
        different symbol than the one trading."""
        bars = _bars(policy.longest_requirement(), step=2)
        result = warmup.evaluate("OWL", bars=bars, now=_now_after(bars),
                                 session_anchor=ANCHOR)
        assert policy.GAP_IN_HISTORY in result["reasons"]
        assert result["state"] == policy.STATE_WARMUP_FAILED

    def test_a_small_gap_is_tolerated(self):
        bars = _bars(policy.longest_requirement() + 2)
        del bars[50]
        result = warmup.evaluate("OWL", bars=bars, now=_now_after(bars),
                                 session_anchor=ANCHOR)
        assert policy.GAP_IN_HISTORY not in result["reasons"]

    def test_a_stale_last_bar_fails(self):
        bars = _bars(policy.longest_requirement())
        late = bars[-1].minute + timedelta(minutes=30)
        result = warmup.evaluate("OWL", bars=bars, now=late,
                                 session_anchor=ANCHOR)
        assert policy.STALE_LAST_BAR in result["reasons"]

    def test_every_problem_is_reported_together(self):
        """Three problems reported one per cycle takes three cycles to
        understand."""
        bars = _bars(policy.longest_requirement(), step=2)
        bars.append(bars[-1])
        result = warmup.evaluate("OWL", bars=bars, now=_now_after(bars),
                                 session_anchor=ANCHOR)
        assert policy.GAP_IN_HISTORY in result["reasons"]
        assert policy.DUPLICATE_TIMESTAMPS in result["reasons"]


class TestTheSessionAnchorIsNotWhenWeStartedListening:
    def test_history_starting_after_the_anchor_fails(self):
        """A symbol subscribed mid-session has no opening range and
        cannot acquire one later."""
        late = ANCHOR + timedelta(minutes=40)
        bars = _bars(policy.longest_requirement(), start=late)
        result = warmup.evaluate("OWL", bars=bars, now=_now_after(bars),
                                 session_anchor=ANCHOR)
        assert policy.ANCHOR_NOT_COVERED in result["reasons"]
        assert result["state"] == policy.STATE_WARMUP_FAILED

    def test_missing_the_whole_opening_range_is_named_separately(self):
        late = ANCHOR + timedelta(minutes=policy.ORB_MINUTES + 5)
        bars = _bars(policy.longest_requirement(), start=late)
        result = warmup.evaluate("OWL", bars=bars, now=_now_after(bars),
                                 session_anchor=ANCHOR)
        assert policy.ORB_WINDOW_MISSED in result["reasons"]

    def test_starting_inside_the_range_is_not_a_missed_window(self):
        inside = ANCHOR + timedelta(minutes=5)
        bars = _bars(policy.longest_requirement(), start=inside)
        result = warmup.evaluate("OWL", bars=bars, now=_now_after(bars),
                                 session_anchor=ANCHOR)
        assert policy.ANCHOR_NOT_COVERED in result["reasons"]
        assert policy.ORB_WINDOW_MISSED not in result["reasons"]


class TestVWAPIsNotTheLastNBars:
    """A VWAP over recent bars is not a session VWAP. Substituting one
    answers a different question than the strategy asked -- silently,
    and with a plausible number."""

    def test_coverage_from_the_anchor_is_required(self):
        bars = _bars(policy.longest_requirement(),
                     start=ANCHOR + timedelta(minutes=30))
        assert warmup.vwap_available(bars, session_anchor=ANCHOR) is False

    def test_full_coverage_is_available(self):
        bars = _bars(policy.longest_requirement())
        assert warmup.vwap_available(bars, session_anchor=ANCHOR) is True

    def test_it_is_reported_as_a_reason(self):
        bars = _bars(policy.longest_requirement(),
                     start=ANCHOR + timedelta(minutes=30))
        result = warmup.evaluate("OWL", bars=bars, now=_now_after(bars),
                                 session_anchor=ANCHOR)
        assert policy.VWAP_UNAVAILABLE in result["reasons"]

    def test_no_bars_means_no_vwap(self):
        assert warmup.vwap_available([], session_anchor=ANCHOR) is False


class TestAnUnjudgeableWarmupIsNotAPass:
    def test_a_broken_bar_object_fails_closed(self):
        class _Bad:
            minute = "not a datetime"

        result = warmup.evaluate("OWL", bars=[_Bad()], now=ANCHOR,
                                 session_anchor=ANCHOR)
        assert result["state"] == policy.STATE_WARMUP_FAILED
        assert warmup.may_watch(result) is False

    def test_no_bars_at_all_does_not_watch(self):
        result = warmup.evaluate("OWL", bars=[], now=ANCHOR,
                                 session_anchor=ANCHOR)
        assert warmup.may_watch(result) is False

    def test_may_watch_rejects_nothing(self):
        assert warmup.may_watch(None) is False
        assert warmup.may_watch({}) is False
