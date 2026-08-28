"""S6's features from KIS bars, and what it refuses to report.

The premarket and after-hours half of the feature layer. It is the SAME
strategy: the conditions, thresholds and arithmetic are S6's existing
ones and only the bars underneath differ. A separate "simplified
extended-hours strategy" would be a second thing to verify and a second
thing to be wrong about, in a family whose whole premise is that a
breakout has the same shape in every session.

Most of these tests are about abstaining. A fabricated VWAP is a price a
strategy compares against; a defaulted expansion ratio of 1.0 reads as
"average"; a frozen feed reads as a calm market. Each of those is worse
than saying the number is not available.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_data import kis_hdfscnt0 as wire  # noqa: E402
from market_data import realtime_bars as rb  # noqa: E402
from s6_live import kis_bar_features as kbf  # noqa: E402
from s6_live.realtime_features import (  # noqa: E402
    VOLUME_DATA_UNAVAILABLE,
    VOLUME_OK,
)

PRE = "PREMARKET"


def _record(symbol="AAPL", price="100", size="10", local_time="081500"):
    record = {name: "" for name in wire.FIELDS}
    record.update({"SYMB": symbol, wire.FIELD_PRICE: price,
                   wire.FIELD_TRADE_SIZE: size,
                   wire.FIELD_LOCAL_DATE: "20260828",
                   wire.FIELD_LOCAL_TIME: local_time,
                   "layout_mismatch": False})
    return record


def _store(trades, session=PRE):
    store = rb.RealtimeBarStore()
    for price, size, at in trades:
        store.add_trade(_record(price=price, size=size, local_time=at),
                        session=session)
    return store


#: A moment just after the last trade below, so the feed reads LIVE.
NOW = datetime(2026, 8, 28, 12, 30, tzinfo=timezone.utc)


def _now_after(local_time):
    hour, minute = int(local_time[:2]), int(local_time[2:4])
    return datetime(2026, 8, 28, hour + 4, minute, 30, tzinfo=timezone.utc)


class TestItReportsWhatTheBarsSupport:
    def test_price_is_the_latest_close(self):
        store = _store([("100", "10", "081500"), ("104", "5", "081600")])
        features = kbf.build_from_bars("AAPL", store=store, session=PRE,
                                       now=_now_after("0816"))
        assert features.price == 104.0
        assert features.bar_count == 2

    def test_vwap_is_volume_weighted_over_this_session(self):
        store = _store([("100", "10", "081500"), ("110", "10", "081600")])
        features = kbf.build_from_bars("AAPL", store=store, session=PRE,
                                       now=_now_after("0816"))
        assert features.vwap == pytest.approx(105.0)

    def test_volume_is_reported_as_ok_when_there_is_any(self):
        store = _store([("100", "10", "081500"), ("101", "3", "081600")])
        features = kbf.build_from_bars("AAPL", store=store, session=PRE,
                                       now=_now_after("0816"))
        assert features.volume == 13.0
        assert features.volume_status == VOLUME_OK

    def test_the_opening_range_comes_from_this_sessions_first_bars(self):
        """A premarket ORB measured from regular-session bars would be a
        different market's range."""
        store = _store([("100", "5", "081500"), ("108", "5", "081600"),
                        ("120", "5", "084000")])
        features = kbf.build_from_bars("AAPL", store=store, session=PRE,
                                       now=_now_after("0840"),
                                       range_minutes=2)
        assert features.range_high == 108.0
        assert features.range_low == 100.0

    def test_extension_is_measured_from_the_range_high(self):
        store = _store([("100", "5", "081500"), ("110", "5", "081600")])
        features = kbf.build_from_bars("AAPL", store=store, session=PRE,
                                       now=_now_after("0816"),
                                       range_minutes=1)
        assert features.extension_pct == pytest.approx(10.0)


class TestItRefusesRatherThanInvents:
    def test_no_bars_returns_none_so_the_caller_can_fall_back(self):
        """An empty snapshot would look like a measured emptiness. None
        says "I have nothing", which is a different statement."""
        assert kbf.build_from_bars("AAPL", store=rb.RealtimeBarStore(),
                                   session=PRE, now=NOW) is None

    def test_a_stale_feed_produces_no_features(self):
        """The bars may be perfectly good and simply old. An entry
        decided on a frozen view of a moving market is the failure this
        layer exists to prevent."""
        store = _store([("100", "10", "081500")])
        features = kbf.build_from_bars("AAPL", store=store, session=PRE,
                                       now=NOW)
        assert features.feed_status == rb.FEED_STALE
        assert features.price is None
        assert features.vwap is None
        assert "price" in features.unavailable

    def test_a_disconnected_feed_produces_no_features(self):
        store = _store([("100", "10", "081500")])
        store.mark_disconnected(now=_now_after("0815"))
        features = kbf.build_from_bars("AAPL", store=store, session=PRE,
                                       now=_now_after("0815"))
        assert features.feed_status == rb.FEED_DISCONNECTED
        assert features.price is None

    def test_expansion_is_unavailable_with_a_single_bar(self):
        """Not 1.0. A defaulted ratio reads as "average volume", which is
        a claim, and it is exactly the claim S6's condition tests."""
        store = _store([("100", "10", "081500")])
        features = kbf.build_from_bars("AAPL", store=store, session=PRE,
                                       now=_now_after("0815"))
        assert features.volume_expansion is None
        assert "volume_expansion" in features.unavailable

    def test_expansion_compares_the_latest_bar_with_the_earlier_ones(self):
        store = _store([("100", "10", "081500"), ("100", "10", "081600"),
                        ("100", "40", "081700")])
        features = kbf.build_from_bars("AAPL", store=store, session=PRE,
                                       now=_now_after("0817"))
        assert features.volume_expansion == pytest.approx(4.0)

    def test_the_existing_expansion_threshold_is_not_restated_here(self):
        """Attaching a new source is not a reason to change what counts
        as expansion. The threshold stays where S6 already keeps it."""
        source = (REPO_ROOT / "s6_live" / "kis_bar_features.py").read_text(
            encoding="utf-8")
        assert "1.2" not in source


class TestProvenance:
    def test_every_snapshot_names_its_source(self):
        store = _store([("100", "10", "081500"), ("101", "3", "081600")])
        features = kbf.build_from_bars("AAPL", store=store, session=PRE,
                                       now=_now_after("0816"))
        assert features.price_source == wire.SOURCE
        assert features.volume_source == wire.SOURCE
        assert features.feed_status == rb.FEED_LIVE

    def test_the_source_survives_into_the_observability_record(self):
        store = _store([("100", "10", "081500"), ("101", "3", "081600")])
        record = kbf.build_from_bars("AAPL", store=store, session=PRE,
                                     now=_now_after("0816")).as_record()
        assert record["price_source"] == wire.SOURCE
        assert record["volume_source"] == wire.SOURCE
        assert record["feed_status"] == rb.FEED_LIVE

    def test_market_data_asof_is_the_last_trade_not_the_build_time(self):
        store = _store([("100", "10", "081500")])
        features = kbf.build_from_bars("AAPL", store=store, session=PRE,
                                       now=_now_after("0815"))
        assert features.market_data_asof == datetime(
            2026, 8, 28, 12, 15, tzinfo=timezone.utc)


class TestSessionScoping:
    def test_another_sessions_bars_are_not_used(self):
        store = _store([("100", "10", "081500")], session="REGULAR")
        assert kbf.build_from_bars("AAPL", store=store, session=PRE,
                                   now=_now_after("0815")) is None

    def test_a_snapshot_for_another_day_is_not_loaded(self, tmp_path):
        """The keying is what stops yesterday's regular-session volume
        from becoming this morning's premarket volume."""
        (tmp_path / "realtime_bars").mkdir()
        (tmp_path / "realtime_bars" / "2026-08-27-PREMARKET.json").write_text("{}")
        assert kbf.load_store("PREMARKET", "2026-08-28",
                              env={"REALTIME_BAR_DIR": str(tmp_path)}) is None

    def test_the_matching_snapshot_is_loaded(self, tmp_path):
        import json

        store = _store([("100", "10", "081500")])
        (tmp_path / "realtime_bars").mkdir()
        (tmp_path / "realtime_bars" / "2026-08-28-PREMARKET.json").write_text(
            json.dumps(store.snapshot()))
        loaded = kbf.load_store("PREMARKET", "2026-08-28",
                                env={"REALTIME_BAR_DIR": str(tmp_path)})
        assert loaded is not None
        assert loaded.accumulator("AAPL", PRE).volume == 10.0

    def test_an_unreadable_snapshot_returns_none_rather_than_raising(self, tmp_path):
        (tmp_path / "realtime_bars").mkdir()
        (tmp_path / "realtime_bars" / "2026-08-28-PREMARKET.json").write_text("{{{")
        assert kbf.load_store("PREMARKET", "2026-08-28",
                              env={"REALTIME_BAR_DIR": str(tmp_path)}) is None


class TestItIsNotASecondStrategy:
    def test_no_thresholds_are_defined_here(self):
        source = (REPO_ROOT / "s6_live" / "kis_bar_features.py").read_text(
            encoding="utf-8")
        for forbidden in ("score_threshold", "SCORE_THRESHOLD",
                          "min_volume_expansion", "THRESHOLD"):
            assert forbidden not in source, forbidden

    def test_it_produces_the_same_snapshot_type_the_rest_of_s6_reads(self):
        store = _store([("100", "10", "081500"), ("101", "3", "081600")])
        features = kbf.build_from_bars("AAPL", store=store, session=PRE,
                                       now=_now_after("0816"))
        from s6_live.realtime_features import SessionFeatures

        assert isinstance(features, SessionFeatures)
