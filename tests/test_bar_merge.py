"""One minute, one bar, and a record of where it came from.

A symbol joining Tier2 mid-session has a live stream from that moment
and a REST history for everything before. Both describe the minute it
joined in. Appending both gives two bars for one minute: nothing
raises, the warmup bar count goes up, and every average over that
history double-counts the minute's volume.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_data import bar_merge, realtime_bars as rb  # noqa: E402

START = datetime(2026, 8, 31, 13, 30, tzinfo=timezone.utc)


def _bar(offset, *, volume=100.0, close=10.0, source=None):
    minute = START + timedelta(minutes=offset)
    bar = rb.Bar(symbol="OWL", session="REGULAR", minute=minute, open=close,
                 high=close, low=close, close=close, volume=volume,
                 trade_count=3, first_trade_at=minute, last_trade_at=minute)
    if source:
        bar.source = source
    return bar


class TestOneMinuteGetsOneBar:
    def test_an_overlapping_minute_is_not_duplicated(self):
        merged = bar_merge.merge(stream_bars=[_bar(5), _bar(6)],
                                 rest_bars=[_bar(4), _bar(5)])
        assert [b.minute for b in merged] == [
            START + timedelta(minutes=i) for i in (4, 5, 6)]
        assert bar_merge.duplicate_minutes(merged) == []

    def test_the_result_is_ordered(self):
        merged = bar_merge.merge(stream_bars=[_bar(9), _bar(7)],
                                 rest_bars=[_bar(3), _bar(1)])
        minutes = [b.minute for b in merged]
        assert minutes == sorted(minutes)

    def test_volume_is_not_double_counted(self):
        """The failure that matters: the same minute's volume counted
        twice in every average over the history."""
        merged = bar_merge.merge(stream_bars=[_bar(5, volume=500.0)],
                                 rest_bars=[_bar(5, volume=500.0)])
        assert len(merged) == 1
        assert sum(b.volume for b in merged) == pytest.approx(500.0)

    def test_either_side_may_be_empty(self):
        assert len(bar_merge.merge(stream_bars=[], rest_bars=[_bar(1)])) == 1
        assert len(bar_merge.merge(stream_bars=[_bar(1)], rest_bars=[])) == 1
        assert bar_merge.merge(stream_bars=None, rest_bars=None) == []


class TestWhichCopyWins:
    def test_the_stream_wins_a_minute_it_saw_whole(self):
        """We watched every print in it; REST is a summary of the same
        trades and can only agree or be staler."""
        merged = bar_merge.merge(
            stream_bars=[_bar(5, close=11.0)],
            rest_bars=[_bar(5, close=99.0)],
            stream_joined_at=START)
        assert merged[0].close == pytest.approx(11.0)
        assert merged[0].source == bar_merge.SOURCE_MERGED

    def test_REST_wins_the_minute_the_stream_joined_partway_through(self):
        """The stream has the tail of that minute; REST has all of it.
        Keeping the partial copy understates its volume permanently."""
        joined = START + timedelta(minutes=5, seconds=40)
        merged = bar_merge.merge(
            stream_bars=[_bar(5, volume=20.0), _bar(6, volume=300.0)],
            rest_bars=[_bar(5, volume=450.0)],
            stream_joined_at=joined)
        by_minute = {b.minute: b for b in merged}
        partial = by_minute[START + timedelta(minutes=5)]
        assert partial.volume == pytest.approx(450.0)
        assert partial.source == bar_merge.SOURCE_REST

    def test_later_minutes_still_come_from_the_stream(self):
        joined = START + timedelta(minutes=5, seconds=40)
        merged = bar_merge.merge(
            stream_bars=[_bar(5, volume=20.0), _bar(6, volume=300.0)],
            rest_bars=[_bar(5, volume=450.0)],
            stream_joined_at=joined)
        by_minute = {b.minute: b for b in merged}
        assert by_minute[START + timedelta(minutes=6)].source \
            == bar_merge.SOURCE_STREAM

    def test_without_a_join_time_the_stream_simply_wins(self):
        merged = bar_merge.merge(stream_bars=[_bar(5, close=11.0)],
                                 rest_bars=[_bar(5, close=99.0)])
        assert merged[0].close == pytest.approx(11.0)


class TestProvenanceIsRecorded:
    """A warmup completed almost entirely on REST data is a different
    claim from one the stream filled in, and they should not look
    identical afterwards."""

    def test_each_bar_is_labelled(self):
        merged = bar_merge.merge(stream_bars=[_bar(6)], rest_bars=[_bar(4)])
        sources = {b.minute: b.source for b in merged}
        assert sources[START + timedelta(minutes=4)] == bar_merge.SOURCE_REST
        assert sources[START + timedelta(minutes=6)] == bar_merge.SOURCE_STREAM

    def test_the_counts_add_up(self):
        merged = bar_merge.merge(stream_bars=[_bar(5), _bar(6)],
                                 rest_bars=[_bar(4), _bar(5)])
        counts = bar_merge.provenance_counts(merged)
        assert counts[bar_merge.SOURCE_REST] == 1
        assert counts[bar_merge.SOURCE_STREAM] == 1
        assert counts[bar_merge.SOURCE_MERGED] == 1
        assert sum(counts.values()) == len(merged)

    def test_counting_an_unlabelled_history_does_not_raise(self):
        assert sum(bar_merge.provenance_counts([_bar(1)]).values()) == 0


class TestAMergedHistoryPassesTheWarmupIntegrityCheck:
    def test_the_merge_output_has_no_duplicates_for_warmup(self):
        """The two modules have to agree: what merge produces is what
        warmup will judge."""
        from config import warmup_policy as policy
        from s6_live import warmup

        needed = policy.longest_requirement()
        rest = [_bar(i) for i in range(needed - 5)]
        stream = [_bar(i) for i in range(needed - 10, needed)]
        merged = bar_merge.merge(stream_bars=stream, rest_bars=rest,
                                 stream_joined_at=START + timedelta(
                                     minutes=needed - 10))
        now = merged[-1].minute + timedelta(minutes=1, seconds=10)
        result = warmup.evaluate("OWL", bars=merged, now=now,
                                 session_anchor=START)
        assert result["state"] == policy.STATE_WATCHING, result["reasons"]
