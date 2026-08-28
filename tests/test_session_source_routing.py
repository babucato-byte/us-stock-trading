"""Which source answers for which session, and what happens when it can't.

The daily-bar provider reports ZERO volume outside regular hours. That
is not a weaker answer than the trade stream, it is a wrong one: a
fabricated zero reads as "nobody traded", and S6's volume condition
cannot tell that apart from a quiet market. So for premarket,
after-hours and the daytime session the KIS stream is the authority, and
when it has nothing the answer is "unavailable" -- never the provider's
zero, and never another session's bars.

Refusing costs a missed opportunity. Falling back costs a trade placed
on a number nobody measured.
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
from s6_live import realtime_features as rf  # noqa: E402


def _record(price="100", size="10", local_time="081500", symbol="AAPL"):
    record = {name: "" for name in wire.FIELDS}
    record.update({"SYMB": symbol, wire.FIELD_PRICE: price,
                   wire.FIELD_TRADE_SIZE: size,
                   wire.FIELD_LOCAL_DATE: "20260828",
                   wire.FIELD_LOCAL_TIME: local_time,
                   "layout_mismatch": False})
    return record


def _store(trades, session="PREMARKET"):
    store = rb.RealtimeBarStore()
    for price, size, at in trades:
        store.add_trade(_record(price=price, size=size, local_time=at),
                        session=session)
    return store


def _now_after(local_time):
    hour, minute = int(local_time[:2]), int(local_time[2:4])
    return datetime(2026, 8, 28, hour + 4, minute, 30, tzinfo=timezone.utc)


class TestTheStreamIsTheAuthorityForExtendedSessions:
    def test_premarket_after_hours_and_daytime_are_routed_to_the_stream(self):
        assert rf.KIS_AUTHORITATIVE_SESSIONS == frozenset(
            {"PREMARKET", "AFTER_HOURS", "OVERNIGHT_DAYTIME"})

    def test_regular_is_deliberately_not_rerouted(self):
        """Its existing source is validated and in use. Swapping it here
        would be an unrelated change to the one session that works."""
        assert "REGULAR" not in rf.KIS_AUTHORITATIVE_SESSIONS


class TestNoStreamMeansUnavailableNotZero:
    def test_a_missing_stream_refuses_rather_than_falling_back(self, monkeypatch):
        monkeypatch.setattr(rf, "_build_from_kis_stream",
                            lambda *a, **k: None)
        features = rf.build("AAPL", session="PREMARKET",
                            now=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))
        assert features.error.startswith(rf.NO_REALTIME_STREAM)
        assert features.price is None
        assert features.volume is None
        assert features.volume_source == rf.KIS_STREAM_SOURCE

    def test_the_daily_provider_is_not_consulted_for_those_sessions(self, monkeypatch):
        """The provider's zero volume is the entire reason this layer
        exists; reaching for it on failure would reintroduce it."""
        called = []

        def _boom(*a, **k):
            called.append(True)
            raise AssertionError("the daily provider must not be used here")

        monkeypatch.setattr(rf, "_build_from_kis_stream", lambda *a, **k: None)
        monkeypatch.setattr(
            "scanners.base.market_data_provider.default_provider", _boom)
        rf.build("AAPL", session="AFTER_HOURS",
                 now=datetime(2026, 8, 28, 21, 0, tzinfo=timezone.utc))
        assert called == []

    def test_a_broken_stream_read_is_treated_as_no_stream(self, monkeypatch):
        monkeypatch.setattr(
            "s6_live.kis_bar_features.load_store",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk gone")))
        features = rf.build("AAPL", session="PREMARKET",
                            now=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))
        assert features.error.startswith(rf.NO_REALTIME_STREAM)


class TestNoWrongSessionFallback:
    def test_regular_bars_never_answer_a_premarket_question(self):
        store = _store([("100", "10", "100000")], session="REGULAR")
        assert kbf.build_from_bars("AAPL", store=store, session="PREMARKET",
                                   now=_now_after("1000")) is None

    def test_premarket_bars_never_answer_an_after_hours_question(self):
        store = _store([("100", "10", "081500")], session="PREMARKET")
        assert kbf.build_from_bars("AAPL", store=store, session="AFTER_HOURS",
                                   now=_now_after("0815")) is None


class TestStaleFeedBlocksReadiness:
    def test_a_stale_feed_reports_no_usable_features(self):
        store = _store([("100", "10", "081500")])
        features = kbf.build_from_bars("AAPL", store=store, session="PREMARKET",
                                       now=datetime(2026, 8, 28, 13, 0,
                                                    tzinfo=timezone.utc))
        assert features.feed_status == rb.FEED_STALE
        assert features.price is None
        assert features.vwap is None

    def test_the_stale_marker_is_named_so_a_funnel_can_count_it(self):
        assert rf.REALTIME_FEED_STALE == "REALTIME_FEED_STALE"


class TestDataIncompleteBlocksExpansion:
    """A gap inside the calculation window deflates the denominator of
    the expansion ratio, inflating it and pushing a candidate towards
    READY for a reason that is an artefact of our own downtime."""

    def _store_with_gap(self, gap_from, gap_to):
        store = _store([("100", "10", "081500"), ("100", "10", "081600"),
                        ("100", "40", "081700")])
        store.gaps.append({"from": gap_from, "to": gap_to,
                           "seconds": 60.0, "kind": "DATA_GAP"})
        return store

    def test_a_gap_inside_the_window_suppresses_expansion(self):
        store = self._store_with_gap("2026-08-28T12:15:30+00:00",
                                     "2026-08-28T12:16:30+00:00")
        features = kbf.build_from_bars("AAPL", store=store, session="PREMARKET",
                                       now=_now_after("0817"))
        assert features.gap_detected is True
        assert features.volume_expansion is None
        assert features.unavailable["volume_expansion"] == rf.DATA_INCOMPLETE

    def test_the_other_features_survive_a_gap(self):
        """Price and VWAP are still real measurements of real trades. It
        is the RATIO that a partial denominator invalidates."""
        store = self._store_with_gap("2026-08-28T12:15:30+00:00",
                                     "2026-08-28T12:16:30+00:00")
        features = kbf.build_from_bars("AAPL", store=store, session="PREMARKET",
                                       now=_now_after("0817"))
        assert features.price == 100.0
        assert features.vwap is not None

    def test_a_gap_that_has_aged_out_of_the_window_recovers(self):
        """Recovery is automatic once the gap is no longer among the bars
        being compared -- standing the strategy down for a whole session
        over a blip would be its own failure."""
        store = self._store_with_gap("2026-08-28T11:00:00+00:00",
                                     "2026-08-28T11:01:00+00:00")
        features = kbf.build_from_bars("AAPL", store=store, session="PREMARKET",
                                       now=_now_after("0817"))
        assert features.gap_detected is False
        assert features.volume_expansion == pytest.approx(4.0)

    def test_an_unplaceable_gap_is_treated_as_overlapping(self):
        """A gap we cannot place is a gap we cannot rule out."""
        store = self._store_with_gap(None, None)
        features = kbf.build_from_bars("AAPL", store=store, session="PREMARKET",
                                       now=_now_after("0817"))
        assert features.gap_detected is True

    def test_the_flag_reaches_the_observability_record(self):
        store = self._store_with_gap("2026-08-28T12:15:30+00:00",
                                     "2026-08-28T12:16:30+00:00")
        record = kbf.build_from_bars("AAPL", store=store, session="PREMARKET",
                                     now=_now_after("0817")).as_record()
        assert record["gap_detected"] is True


class TestTheSnapshotCarriesEverythingAFunnelNeeds:
    def test_every_field_the_directive_lists_is_present(self):
        store = _store([("100", "10", "081500"), ("104", "5", "081600")])
        record = kbf.build_from_bars("AAPL", store=store, session="PREMARKET",
                                     now=_now_after("0816")).as_record()
        for key in ("symbol", "session", "price", "vwap", "ema9", "ema21",
                    "range_high", "range_low", "volume", "volume_expansion",
                    "market_data_asof", "price_source", "volume_source",
                    "bar_count", "feed_status", "volume_cross_check",
                    "gap_detected"):
            assert key in record, key
