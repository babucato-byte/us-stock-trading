"""Every scanner says when its data was observed, or says it does not know.

`market_data_asof` began as an ORB-only field. Left that way it would
have meant that "no timestamp" carried two incompatible readings
depending on which scanner produced the row -- either "this producer
does not supply it" or "this producer supplies it and the data was
unavailable" -- and a consumer cannot fail closed on a value whose
absence means two things.

It is never substituted. Not `generated_at`, not `now()`, not the
scanner's run time. Unknown stays unknown, and a LIVE BUY refuses on
unknown.

No network access: frames are constructed directly.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scanners.base import scanner_base  # noqa: E402
from scanners.registry import build_scanners  # noqa: E402
from s6_live import precision_watch as pw  # noqa: E402
from s6_live import realtime_features as rf  # noqa: E402

ET = "America/New_York"
NOW = datetime(2026, 8, 26, 18, 0, tzinfo=timezone.utc)


class TestEveryScannerDeclaresItsBasis:
    def test_all_six_declare_one(self):
        bases = {s.scanner_name: s.market_data_basis for s in build_scanners()}
        assert len(bases) == 6
        assert set(bases.values()) <= {scanner_base.MARKET_DATA_BASIS_DAILY,
                                       scanner_base.MARKET_DATA_BASIS_INTRADAY}

    def test_the_intraday_scanners_say_intraday(self):
        """A daily timestamp for a scanner that gated on minute bars
        would be a confident answer about data it never read."""
        bases = {s.scanner_name: s.market_data_basis for s in build_scanners()}
        for name in ("orb", "gap_pullback", "premarket_momentum"):
            assert bases[name] == scanner_base.MARKET_DATA_BASIS_INTRADAY, name

    def test_the_daily_scanners_say_daily(self):
        bases = {s.scanner_name: s.market_data_basis for s in build_scanners()}
        for name in ("hma_early_trend", "accumulation", "breakout_ready"):
            assert bases[name] == scanner_base.MARKET_DATA_BASIS_DAILY, name

    def test_the_declared_set_matches_the_registry(self):
        """The registry already splits them; the two must not drift."""
        from scanners import registry

        bases = {s.scanner_name: s.market_data_basis for s in build_scanners()}
        for name in registry.INTRADAY_SCANNERS:
            assert bases[name] == scanner_base.MARKET_DATA_BASIS_INTRADAY
        for name in registry.DAILY_SCANNERS:
            assert bases[name] == scanner_base.MARKET_DATA_BASIS_DAILY


class TestTheSharedTimestampHelper:
    def _intraday(self, start="2026-08-26 15:55", periods=2):
        index = pd.date_range(start=start, periods=periods, freq="5min", tz=ET)
        return pd.DataFrame({"Close": [1.0] * periods}, index=index)

    def test_intraday_returns_the_newest_bar_in_utc(self):
        assert scanner_base.bar_timestamp(self._intraday()) == \
            "2026-08-26T20:00:00+00:00"

    def test_daily_returns_the_newest_date(self):
        frame = pd.DataFrame(
            {"Close": [1.0, 2.0]},
            index=pd.to_datetime(["2026-08-25", "2026-08-26"]))
        assert scanner_base.bar_timestamp(frame).startswith("2026-08-26")

    def test_empty_and_missing_are_None(self):
        assert scanner_base.bar_timestamp(None) is None
        assert scanner_base.bar_timestamp(self._intraday(periods=0)) is None

    def test_a_broken_frame_is_None_rather_than_raising(self):
        class _Bad:
            def __len__(self):
                return 1

            @property
            def index(self):
                raise RuntimeError("boom")

        assert scanner_base.bar_timestamp(_Bad()) is None

    def test_it_is_never_the_current_time(self):
        """The substitution this whole field exists to prevent."""
        frame = self._intraday(start="2026-08-26 09:30")
        stamp = scanner_base.bar_timestamp(frame)
        assert not stamp.startswith(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H"))


class TestUnknownIsFailClosedForLiveBuy:
    """§3 -- research may record it; a live BUY may not act on it."""

    def _features(self, **overrides):
        kwargs = dict(
            symbol="DT", session="REGULAR",
            market_data_asof=NOW - timedelta(minutes=1), built_at=NOW,
            price=51.5, vwap=51.0, ema9=51.4, ema21=51.2,
            volume=250_000.0, volume_status=rf.VOLUME_OK,
            volume_expansion=1.8, range_high=50.75, range_low=49.0,
            extension_pct=1.48, bar_count=40)
        kwargs.update(overrides)
        return rf.SessionFeatures(**kwargs)

    def test_a_known_asof_passes_the_condition(self):
        out = pw.evaluate("DT", session="REGULAR", now=NOW,
                          features=self._features())
        assert out.conditions[pw.C_MARKET_DATA_ASOF] == pw.PASS
        assert out.ready

    def test_an_unknown_asof_blocks_ready(self):
        out = pw.evaluate("DT", session="REGULAR", now=NOW,
                          features=self._features(market_data_asof=None))
        assert out.conditions[pw.C_MARKET_DATA_ASOF] == pw.UNAVAILABLE
        assert not out.ready
        assert out.detail["market_data_asof_unknown"] == \
            pw.MARKET_DATA_ASOF_UNKNOWN

    def test_unknown_is_distinct_from_stale(self):
        """One is a row that is old; the other is a row that cannot be
        judged. They call for different operator responses."""
        stale = pw.evaluate("DT", session="REGULAR", now=NOW,
                            features=self._features(
                                market_data_asof=NOW - timedelta(hours=3)))
        unknown = pw.evaluate("DT", session="REGULAR", now=NOW,
                              features=self._features(market_data_asof=None))
        assert stale.conditions[pw.C_MARKET_DATA_ASOF] == pw.PASS
        assert stale.conditions[pw.C_MARKET_DATA_FRESH] == pw.FAIL
        assert unknown.conditions[pw.C_MARKET_DATA_ASOF] == pw.UNAVAILABLE

    def test_both_appear_in_the_blocking_list(self):
        out = pw.evaluate("DT", session="REGULAR", now=NOW,
                          features=self._features(market_data_asof=None))
        assert pw.C_MARKET_DATA_ASOF in out.blocking


class TestTheTwoTimestampsAreNeverConflated:
    def test_a_fresh_publication_over_stale_data_is_still_refused(self):
        """§40 item 4, and the DT trade: generated_at fresh,
        market_data_asof hours old."""
        feats = rf.SessionFeatures(
            symbol="DT", session="AFTER_HOURS",
            market_data_asof=NOW - timedelta(hours=3),
            built_at=NOW, price=52.75, vwap=51.0, ema9=52.1, ema21=52.0,
            volume=1.0, volume_status=rf.VOLUME_OK, volume_expansion=1.8,
            range_high=50.75, range_low=49.0, extension_pct=4.01)
        out = pw.evaluate("DT", session="AFTER_HOURS", now=NOW,
                          features=feats,
                          candidate={"generated_at": NOW.isoformat()})
        assert not out.ready
        assert out.conditions[pw.C_MARKET_DATA_FRESH] == pw.FAIL

    def test_the_base_class_never_substitutes_generated_at(self):
        import inspect

        source = inspect.getsource(scanner_base.BaseScanner.evaluate)
        assert "market_data_asof" in source
        assert "bar_timestamp(frame)" in source
        # The substitution that would defeat the field.
        assert "market_data_asof\"] = stamp" not in source
