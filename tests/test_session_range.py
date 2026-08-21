"""Per-session opening ranges, and the midnight wrap.

Two properties carry this file.

Sessions must not bleed into each other. A breakout of 09:30's range
means nothing at 20:00, so REGULAR's range must never appear in
another session's answer -- and the test that matters is the one where
both sessions' bars are in the SAME frame, because that is the shape the
scanner actually gets.

And OVERNIGHT_DAYTIME wraps midnight. `start <= t < end` is false for
every bar of a 20:00->04:00 window, so a naive implementation returns
nothing and reports it as a quiet session. Both sides of midnight are
tested, and so is the rule that a 01:00 bar belongs to the session that
opened the previous evening -- filing it under its own date would split
one session in two and build each half a range from part of the data.
"""

import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_hours import EASTERN  # noqa: E402
from scanners.base import session_range as sr  # noqa: E402


def bars(*specs):
    """(ET timestamp, high, low) triples as a minute frame."""
    index = [pd.Timestamp(t, tz=EASTERN) for t, _h, _l in specs]
    return pd.DataFrame(
        {"Open": [h for _t, h, _l in specs],
         "High": [h for _t, h, _l in specs],
         "Low": [l for _t, _h, l in specs],
         "Close": [h for _t, h, _l in specs],
         "Volume": [1000] * len(specs)},
        index=index)


class TestEachSessionHasItsOwnWindow:
    @pytest.mark.parametrize("session,start,end", [
        ("PREMARKET", "04:00", "09:30"),
        ("REGULAR", "09:30", "16:00"),
        ("AFTER_HOURS", "16:00", "20:00"),
        ("OVERNIGHT_DAYTIME", "20:00", "04:00"),
    ])
    def test_the_windows_are_the_market_hours_boundaries(self, session, start,
                                                         end):
        window = sr.window_for(session)
        assert window[0].strftime("%H:%M") == start
        assert window[1].strftime("%H:%M") == end

    def test_only_the_overnight_window_wraps(self):
        assert sr.wraps_midnight("OVERNIGHT_DAYTIME") is True
        for session in ("PREMARKET", "REGULAR", "AFTER_HOURS"):
            assert sr.wraps_midnight(session) is False

    def test_an_unknown_session_has_no_window(self):
        assert sr.window_for("DAILY") is None
        assert sr.window_for(None) is None


class TestSessionsDoNotBleedIntoEachOther:
    def frame(self):
        """One frame holding four sessions -- the shape a scanner gets."""
        return bars(
            ("2026-08-21 05:00", 101, 100),   # PREMARKET
            ("2026-08-21 05:10", 102,  99),
            ("2026-08-21 09:35", 111, 110),   # REGULAR
            ("2026-08-21 09:40", 112, 109),
            ("2026-08-21 16:30", 121, 120),   # AFTER_HOURS
            ("2026-08-21 20:30", 131, 130),   # OVERNIGHT (this evening)
            ("2026-08-22 01:00", 132, 129),   # OVERNIGHT (past midnight)
        )

    @pytest.mark.parametrize("session,high,low", [
        ("PREMARKET", 102, 99),
        ("REGULAR", 112, 109),
        ("AFTER_HOURS", 121, 120),
    ])
    def test_a_session_sees_only_its_own_bars(self, session, high, low):
        result = sr.opening_range(self.frame(), session, minutes=30)
        assert result.range_high == high
        assert result.range_low == low

    def test_regulars_range_never_appears_in_another_session(self):
        """The failure this whole module exists to prevent."""
        for session in ("PREMARKET", "AFTER_HOURS", "OVERNIGHT_DAYTIME"):
            result = sr.opening_range(self.frame(), session, minutes=30)
            assert result.range_high != 112, session
            assert result.range_low != 109, session

    def test_every_session_produces_a_distinct_range(self):
        highs = {s: sr.opening_range(self.frame(), s, minutes=60).range_high
                 for s in ("PREMARKET", "REGULAR", "AFTER_HOURS")}
        assert len(set(highs.values())) == 3, highs

    def test_an_unknown_session_produces_nothing(self):
        result = sr.opening_range(self.frame(), "DAILY", minutes=15)
        assert result.complete is False


class TestTheMidnightWrap:
    def evening_and_morning(self):
        return bars(
            ("2026-08-21 20:05", 131, 130),
            ("2026-08-21 23:50", 133, 129),
            ("2026-08-22 00:10", 134, 128),
            ("2026-08-22 03:50", 135, 127),
        )

    def test_bars_on_both_sides_of_midnight_are_one_session(self):
        """`start <= t < end` is false for every one of these. A naive
        implementation returns nothing and calls it a quiet session."""
        result = sr.opening_range(self.evening_and_morning(),
                                  "OVERNIGHT_DAYTIME", minutes=600)
        assert result.bars == 4
        assert result.range_high == 135
        assert result.range_low == 127

    @pytest.mark.parametrize("moment,expected", [
        ("2026-08-21 20:05", date(2026, 8, 21)),
        ("2026-08-21 23:59", date(2026, 8, 21)),
        ("2026-08-22 00:01", date(2026, 8, 21)),
        ("2026-08-22 03:59", date(2026, 8, 21)),
    ])
    def test_the_session_date_is_when_it_opened(self, moment, expected):
        """A 01:00 bar belongs to the session that opened at 20:00 the
        previous evening. Filing it under its own date would split one
        session in half."""
        stamp = pd.Timestamp(moment, tz=EASTERN)
        assert sr.session_start_date(stamp, "OVERNIGHT_DAYTIME") == expected

    def test_a_non_wrapping_session_keeps_its_own_date(self):
        stamp = pd.Timestamp("2026-08-21 10:00", tz=EASTERN)
        assert sr.session_start_date(stamp, "REGULAR") == date(2026, 8, 21)

    def test_two_consecutive_overnight_sessions_stay_apart(self):
        frame = bars(
            ("2026-08-20 20:05", 91, 90),    # previous evening's session
            ("2026-08-21 01:00", 92, 89),
            ("2026-08-21 20:05", 131, 130),  # tonight's session
            ("2026-08-22 01:00", 132, 129),
        )
        latest = sr.opening_range(frame, "OVERNIGHT_DAYTIME", minutes=600)
        assert latest.range_high == 132 and latest.range_low == 129

        earlier = sr.opening_range(frame, "OVERNIGHT_DAYTIME", minutes=600,
                                   session_date=date(2026, 8, 20))
        assert earlier.range_high == 92 and earlier.range_low == 89

    def test_the_range_window_is_measured_from_the_first_bar(self):
        result = sr.opening_range(self.evening_and_morning(),
                                  "OVERNIGHT_DAYTIME", minutes=15)
        assert result.bars == 1, "only 20:05 falls inside 15 minutes"
        assert result.range_high == 131


class TestBreakoutAndReentry:
    def range15(self):
        return sr.opening_range(
            bars(("2026-08-21 09:31", 110, 108),
                 ("2026-08-21 09:40", 112, 109),
                 ("2026-08-21 10:00", 120, 118)),
            "REGULAR", minutes=15)

    def test_a_price_above_the_high_breaks_out(self):
        assert self.range15().breaks_out(113) is True

    def test_a_price_at_the_high_has_not_broken_out(self):
        """Equality is not a breakout; treating it as one would fire on a
        rounding artefact."""
        assert self.range15().breaks_out(112) is False

    def test_falling_back_inside_is_a_reentry(self):
        """The S6 exit signal."""
        assert self.range15().reenters(111) is True
        assert self.range15().reenters(113) is False

    def test_an_incomplete_range_answers_none_not_false(self):
        """"no breakout" and "no range to break" are different facts, and
        the second means the scan has nothing to say yet."""
        empty = sr.opening_range(pd.DataFrame(), "REGULAR", minutes=15)
        assert empty.complete is False
        assert empty.breaks_out(999) is None
        assert empty.reenters(1) is None

    def test_an_unreadable_price_answers_none(self):
        assert self.range15().breaks_out(None) is None
        assert self.range15().breaks_out("high") is None


class TestTheShadowComparison:
    def test_all_three_windows_are_produced_side_by_side(self):
        frame = bars(("2026-08-21 09:31", 110, 108),
                     ("2026-08-21 09:40", 115, 107),
                     ("2026-08-21 09:55", 120, 105))
        ranges = sr.shadow_ranges(frame, "REGULAR", windows=(5, 15, 30))
        assert set(ranges) == {5, 15, 30}
        assert ranges[5].range_high == 110
        assert ranges[15].range_high == 115
        assert ranges[30].range_high == 120

    def test_nothing_here_picks_a_winner(self):
        """Choosing a window is what the comparison is FOR; picking one
        now would answer the question before the data exists."""
        import inspect

        body = inspect.getsource(sr.shadow_ranges)
        for word in ("best", "preferred", "default", "chosen"):
            assert word not in body.lower().split("--")[0]

    def test_the_configured_windows_are_the_ones_measured(self):
        from config import s6_sessions

        assert s6_sessions.SHADOW_RANGE_MINUTES == (5, 15, 30)
        assert s6_sessions.REGULAR_ORB_MINUTES == 15


class TestTheRecordIsComplete:
    def test_a_range_reports_what_it_was_built_from(self):
        result = sr.opening_range(
            bars(("2026-08-21 09:31", 110, 108),
                 ("2026-08-21 09:40", 112, 109)),
            "REGULAR", minutes=15)
        row = result.as_dict()
        for field in ("session", "range_start", "range_end", "range_high",
                      "range_low", "range_bars", "range_minutes"):
            assert field in row, field
        assert row["range_bars"] == 2
        assert row["range_minutes"] == 15
        assert row["session"] == "REGULAR"

    def test_a_high_without_bars_is_not_a_range(self):
        """A pair of Nones would compare False against every price and
        silently never break out."""
        empty = sr.opening_range(pd.DataFrame(), "REGULAR", minutes=15)
        assert empty.complete is False
        assert empty.bars == 0
