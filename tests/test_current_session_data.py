"""Yesterday's session is never today's answer.

2026-08-27, 05:42 ET. The provider had published nothing for the day.
`slice_session_bars` with no date takes the most recent date that HAS
bars for the session, so a PREMARKET slice returned eight bars from
2026-08-26 09:25 ET and the scanner published PTC as a current premarket
candidate carrying an eighteen-hour-old market.

The Watch refused it -- correctly, on staleness -- but a producer that
describes yesterday as today is a defect regardless of who catches it.
A missing session is now NO_CURRENT_SESSION_DATA, which is a different
statement from "old" and from "the feed is broken".
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scanners.base import session_range as srange  # noqa: E402
from s6_live import realtime_features as rf  # noqa: E402

ET = "America/New_York"
# 05:42 ET on 2026-08-27 -- inside premarket, before the provider filled in.
NOW = datetime(2026, 8, 27, 9, 42, tzinfo=timezone.utc)


def _bars(*specs):
    index, closes, volumes = [], [], []
    for stamp, close, volume in specs:
        index.append(pd.Timestamp(stamp, tz=ET))
        closes.append(close)
        volumes.append(volume)
    return pd.DataFrame({"Open": closes, "High": closes, "Low": closes,
                         "Close": closes, "Volume": volumes},
                        index=pd.DatetimeIndex(index))


class TestTheCurrentSessionDate:
    def test_a_non_wrapping_session_is_todays_eastern_date(self):
        assert srange.current_session_date("PREMARKET", NOW) == date(2026, 8, 27)
        assert srange.current_session_date("REGULAR", NOW) == date(2026, 8, 27)

    def test_a_wrapping_session_after_midnight_belongs_to_yesterday(self):
        """A bar at 02:00 belongs to the session that opened at 20:00."""
        two_am_et = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)
        assert srange.current_session_date(
            "OVERNIGHT_DAYTIME", two_am_et) == date(2026, 8, 26)

    def test_a_wrapping_session_before_midnight_is_today(self):
        nine_pm_et = datetime(2026, 8, 27, 1, 0, tzinfo=timezone.utc)
        assert srange.current_session_date(
            "OVERNIGHT_DAYTIME", nine_pm_et) == date(2026, 8, 26)


class TestYesterdayIsNeverSubstituted:
    def _yesterday_only(self):
        """The exact live shape: yesterday's bars, nothing for today."""
        return _bars(
            ("2026-08-26 09:25", 151.61, 1000),   # yesterday premarket
            ("2026-08-26 15:55", 151.64, 5000),   # yesterday regular
            ("2026-08-26 19:30", 151.61, 0),      # yesterday after-hours
        )

    def test_item7_no_current_bars_does_not_fall_back_to_yesterday(self):
        frame = self._yesterday_only()
        today = srange.current_session_date("PREMARKET", NOW)
        scoped = srange.slice_session_bars(frame, "PREMARKET",
                                           session_date=today)
        assert len(scoped) == 0

    def test_without_a_date_it_would_have_returned_yesterday(self):
        """The behaviour being corrected, pinned so it cannot return."""
        frame = self._yesterday_only()
        unscoped = srange.slice_session_bars(frame, "PREMARKET")
        assert len(unscoped) == 1
        assert str(unscoped.index[-1])[:10] == "2026-08-26"

    def test_todays_bars_are_returned_when_they_exist(self):
        frame = _bars(("2026-08-26 09:25", 151.61, 1000),
                      ("2026-08-27 05:30", 152.00, 2000))
        today = srange.current_session_date("PREMARKET", NOW)
        scoped = srange.slice_session_bars(frame, "PREMARKET",
                                           session_date=today)
        assert len(scoped) == 1
        assert str(scoped.index[-1])[:10] == "2026-08-27"


class _Data:
    def __init__(self, intraday):
        self.symbol, self.intraday, self.daily = "PTC", intraday, None


class _Provider:
    def __init__(self, intraday):
        self._intraday = intraday

    def get_symbol_data(self, symbol, **kwargs):
        return _Data(self._intraday)


class TestTheFeatureEngineReportsIt:
    def test_item9_no_current_session_data_is_named(self):
        frame = _bars(("2026-08-26 09:25", 151.61, 1000),
                      ("2026-08-26 19:30", 151.61, 0))
        feats = rf.build("PTC", session="PREMARKET", now=NOW,
                         provider=_Provider(frame))
        assert rf.NO_CURRENT_SESSION_DATA in feats.error
        assert feats.unavailable["price"] == rf.NO_CURRENT_SESSION_DATA

    def test_item8_asof_is_None_not_yesterdays_timestamp(self):
        """Reporting yesterday's bar time would make the row look merely
        stale, when in fact nothing about today is known."""
        frame = _bars(("2026-08-26 19:30", 151.61, 0))
        feats = rf.build("PTC", session="PREMARKET", now=NOW,
                         provider=_Provider(frame))
        assert feats.market_data_asof is None

    def test_a_live_buy_is_refused_on_it(self):
        from s6_live import precision_watch as pw

        frame = _bars(("2026-08-26 19:30", 151.61, 0))
        feats = rf.build("PTC", session="PREMARKET", now=NOW,
                         provider=_Provider(frame))
        out = pw.evaluate("PTC", session="PREMARKET", now=NOW, features=feats)
        assert not out.ready
        assert pw.C_MARKET_DATA_ASOF in out.blocking

    def test_current_session_bars_produce_a_usable_view(self):
        frame = _bars(("2026-08-27 04:05", 151.0, 1000),
                      ("2026-08-27 05:30", 152.0, 2000))
        feats = rf.build("PTC", session="PREMARKET", now=NOW,
                         provider=_Provider(frame))
        assert feats.error is None
        assert feats.market_data_asof is not None
        assert str(feats.market_data_asof)[:10] == "2026-08-27"


class TestTheScannerRefusesToo:
    def test_the_orb_scanner_scopes_to_the_current_session(self):
        import inspect

        from scanners.orb import scanner

        source = inspect.getsource(scanner)
        assert "current_session_date(requested)" in source
        assert "NO_CURRENT_SESSION_DATA" in source
