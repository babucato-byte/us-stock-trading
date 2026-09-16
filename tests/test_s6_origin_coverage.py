"""No bar at the origin has two causes, and only one is a refusal.

A quiet minute and an unwatched minute both arrive as "no bar at
01:00". The first must not stop a range being built from the real
trades that followed; the second is the absence of evidence this layer
exists to refuse. Everything here is about telling them apart, and
about fetching the opening minutes the collector connected too late to
hear.
"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from s6_live import kis_bar_features as kbf
from s6_live import origin_backfill

UTC = timezone.utc
#: 10:00 KST on 2026-09-16 -- the daytime open, fixed in UTC year-round.
ORIGIN = datetime(2026, 9, 16, 1, 0, tzinfo=UTC)
SESSION = "OVERNIGHT_DAYTIME"
SESSION_DATE = date(2026, 9, 15)


def _bar(symbol, minute, *, price=100.0, volume=500.0):
    from market_data.realtime_bars import Bar

    return Bar(symbol=symbol, session=SESSION, minute=minute, open=price,
               high=price + 0.5, low=price - 0.5, close=price, volume=volume,
               trade_count=3, first_trade_at=minute, last_trade_at=minute,
               price_volume=price * volume, source="KIS_HDFSCNT0")


def _store(minutes, *, coverage_started, symbol="MRVL"):
    from market_data.realtime_bars import RealtimeBarStore, SessionAccumulator

    store = RealtimeBarStore(stale_after_seconds=36000)
    store.coverage_started_at = coverage_started
    acc = SessionAccumulator(symbol=symbol, session=SESSION)
    for i, m in enumerate(minutes):
        bar = _bar(symbol, m, price=100.0 + i * 0.1)
        acc.bars[m] = bar
        acc.volume += bar.volume
        acc.price_volume += bar.price_volume
    acc.trade_count = len(minutes)
    if minutes:
        acc.last_trade_at = max(minutes)
    store._accumulators[(symbol, SESSION)] = acc
    return store


def _build(store, *, now=None, symbol="MRVL", range_minutes=5):
    return kbf.build_from_bars(
        symbol, store=store, session=SESSION,
        now=now or (ORIGIN + timedelta(minutes=40)),
        range_minutes=range_minutes, closed_bar_only=True)


class _Chart:
    """Stands in for kis_minute_chart, with an explicit reach."""

    BARS_PER_CALL = 120

    def __init__(self, rows):
        self.rows = rows
        self.asked = []

    def fetch(self, broker, *, symbol, exchange, session=None, trading_day=None):
        self.asked.append((symbol, exchange, session))
        return self.rows

    def to_bars(self, records, *, symbol, session):
        from market_data.realtime_bars import Bar

        return [Bar(symbol=symbol, session=session, minute=r["at"],
                    open=r["open"], high=r["high"], low=r["low"],
                    close=r["close"], volume=r["volume"], trade_count=0,
                    first_trade_at=r["at"], last_trade_at=r["at"],
                    source="KIS_REST_CHART") for r in records]


def _rows(start, count, price=100.0):
    return [{"at": start + timedelta(minutes=i), "open": price, "high": price + 0.5,
             "low": price - 0.5, "close": price, "volume": 300.0}
            for i in range(count)]


class TestA_ExactOriginBar:
    """A. A bar at 01:00 -> normal success."""

    def test_the_range_builds(self):
        store = _store([ORIGIN + timedelta(minutes=i) for i in range(10)],
                       coverage_started=ORIGIN - timedelta(seconds=5))
        feats = _build(store)
        assert feats.error is None
        assert feats.range_high is not None
        assert feats.origin_status == kbf.ORIGIN_AVAILABLE


class TestB_CoveredButNoTradeAtOrigin:
    """B. Continuous coverage from before the open, first trade 01:01."""

    def test_the_range_builds_from_real_trades(self):
        store = _store([ORIGIN + timedelta(minutes=i) for i in range(1, 10)],
                       coverage_started=ORIGIN - timedelta(minutes=2))
        feats = _build(store)
        assert feats.origin_status == kbf.ORIGIN_COVERED_NO_TRADE
        assert feats.error is None, "a quiet minute is not a coverage gap"
        assert feats.range_high is not None

    def test_the_range_uses_only_bars_inside_the_window(self):
        store = _store([ORIGIN + timedelta(minutes=i) for i in range(1, 10)],
                       coverage_started=ORIGIN - timedelta(minutes=2))
        feats = _build(store)
        # 01:01-01:04 are inside [01:00, 01:05); 01:05+ are not.
        inside = [100.0 + i * 0.1 for i in range(0, 4)]
        assert feats.range_high == pytest.approx(max(inside) + 0.5)

    def test_an_entirely_empty_window_is_still_refused(self):
        """Covered, but not one trade in the whole opening range."""
        store = _store([ORIGIN + timedelta(minutes=i) for i in range(20, 30)],
                       coverage_started=ORIGIN - timedelta(minutes=2))
        feats = _build(store)
        assert feats.error == kbf.OFFICIAL_ORIGIN_NOT_COVERED
        assert "origin_coverage_claim" in feats.unavailable


class TestC_IncompleteCoverage:
    """C. The collector connected at 01:00:20 -- it was not listening."""

    def test_absence_is_not_proof_of_no_trade(self):
        store = _store([ORIGIN + timedelta(minutes=i) for i in range(1, 10)],
                       coverage_started=ORIGIN + timedelta(seconds=20))
        feats = _build(store)
        assert feats.origin_status == kbf.ORIGIN_NOT_COVERED
        assert feats.error == kbf.OFFICIAL_ORIGIN_NOT_COVERED
        assert feats.range_high is None

    def test_this_is_the_live_daytime_shape(self):
        """Production starts the collector AT the boundary, so it is
        always a few seconds late -- 19.85s on 2026-09-16. This is the
        case the backfill exists for, not case B."""
        store = _store([ORIGIN + timedelta(minutes=i) for i in range(1, 6)],
                       coverage_started=ORIGIN + timedelta(seconds=19, milliseconds=850))
        assert _build(store).origin_status == kbf.ORIGIN_NOT_COVERED


class TestD_BackfillMergesUnderTheStream:
    """D. Incomplete stream + REST origin available -> merge succeeds."""

    def test_the_origin_is_restored_and_the_range_builds(self):
        store = _store([ORIGIN + timedelta(minutes=i) for i in range(1, 10)],
                       coverage_started=ORIGIN + timedelta(seconds=20))
        assert _build(store).error == kbf.OFFICIAL_ORIGIN_NOT_COVERED

        chart = _Chart(_rows(ORIGIN - timedelta(minutes=5), 60))
        report = origin_backfill.restore_origin(
            store, "MRVL", session=SESSION, official_origin=ORIGIN,
            range_minutes=5, broker=object(), exchange="NASDAQ", chart=chart)

        assert report["status"] == origin_backfill.BACKFILLED
        assert report["bars_added"] >= 1
        feats = _build(store)
        assert feats.error is None
        assert feats.origin_status == kbf.ORIGIN_BACKFILLED
        assert feats.range_high is not None

    def test_the_stream_wins_every_minute_it_already_had(self):
        """REST is a summary of the same trades; it may only fill gaps."""
        streamed = [ORIGIN + timedelta(minutes=i) for i in range(1, 10)]
        store = _store(streamed, coverage_started=ORIGIN + timedelta(seconds=20))
        before = {m: store.bars("MRVL", SESSION)[i].close
                  for i, m in enumerate(sorted(streamed))}

        chart = _Chart(_rows(ORIGIN - timedelta(minutes=5), 60, price=999.0))
        origin_backfill.restore_origin(
            store, "MRVL", session=SESSION, official_origin=ORIGIN,
            range_minutes=5, broker=object(), exchange="NASDAQ", chart=chart)

        acc = store.accumulator("MRVL", SESSION)
        for minute, close in before.items():
            assert acc.bars[minute].close == close, "stream bar was overwritten"
        assert acc.bars[ORIGIN].source == "KIS_REST_CHART"

    def test_it_uses_the_daytime_venue(self):
        store = _store([ORIGIN + timedelta(minutes=1)],
                       coverage_started=ORIGIN + timedelta(seconds=20))
        chart = _Chart(_rows(ORIGIN, 10))
        origin_backfill.restore_origin(
            store, "MRVL", session=SESSION, official_origin=ORIGIN,
            range_minutes=5, broker=object(), exchange="NASDAQ", chart=chart)
        assert chart.asked == [("MRVL", "NASDAQ", SESSION)], (
            "the session must be passed so EXCD resolves to the daytime venue")


class TestTheVenueIsResolvable:
    """The gap that shipped: every test passed `exchange=` explicitly,
    so nothing exercised the lookup, and in production it returned None
    for every symbol -- the backfill failed before reaching the wire."""

    @pytest.mark.parametrize("symbol", ["MRNA", "STX", "GS", "LRCX", "HPE"])
    def test_a_real_symbol_resolves_to_a_usable_code(self, symbol):
        from market_data import kis_minute_chart as mc

        venue = origin_backfill._exchange_for(symbol, None, SESSION)
        assert venue, f"{symbol} must resolve to a venue"
        # The value must survive the KIS code table -- a repr like
        # "USExchange.NASDAQ" does not.
        assert mc.excd_for_session(venue, SESSION) in ("BAQ", "BAY", "BAA")

    def test_an_unknown_symbol_is_a_reason_not_a_crash(self):
        assert origin_backfill._exchange_for(
            "NOSUCHSYMBOL", None, SESSION) is None

    def test_restore_origin_resolves_the_venue_itself(self):
        """No explicit exchange -- the production call shape."""
        store = _store([ORIGIN + timedelta(minutes=1)],
                       coverage_started=ORIGIN + timedelta(seconds=20),
                       symbol="MRNA")
        chart = _Chart(_rows(ORIGIN - timedelta(minutes=2), 30))
        report = origin_backfill.restore_origin(
            store, "MRNA", session=SESSION, official_origin=ORIGIN,
            range_minutes=5, broker=object(), chart=chart)
        assert report["status"] == origin_backfill.BACKFILLED
        assert chart.asked and chart.asked[0][1] == "NASDAQ"


class TestE_LateAdmissionWithinReach:
    """E. Symbol admitted at 04:00, REST can still reach 01:00."""

    def test_the_origin_is_restored(self):
        late = ORIGIN + timedelta(hours=3)
        store = _store([late + timedelta(minutes=i) for i in range(5)],
                       coverage_started=late)
        chart = _Chart(_rows(ORIGIN - timedelta(minutes=2), 120))
        report = origin_backfill.restore_origin(
            store, "MRVL", session=SESSION, official_origin=ORIGIN,
            range_minutes=5, broker=object(), exchange="NASDAQ", chart=chart)
        assert report["status"] == origin_backfill.BACKFILLED
        assert _build(store, now=late + timedelta(minutes=10)).error is None


class TestF_BeyondTheChartsReach:
    """F. The 120-row limit cannot reach the origin -> fail safe."""

    def test_it_refuses_with_its_own_reason(self):
        late = ORIGIN + timedelta(hours=5)
        store = _store([late + timedelta(minutes=i) for i in range(5)],
                       coverage_started=late)
        # Oldest REST row is 02:00, an hour AFTER the origin.
        chart = _Chart(_rows(ORIGIN + timedelta(hours=1), 120))
        report = origin_backfill.restore_origin(
            store, "MRVL", session=SESSION, official_origin=ORIGIN,
            range_minutes=5, broker=object(), exchange="NASDAQ", chart=chart)

        assert report["status"] == origin_backfill.OUT_OF_REACH
        assert report["bars_added"] == 0
        assert report["oldest_rest_bar"].startswith("2026-09-16T02:00")
        assert report["bars_per_call"] == 120

    def test_the_store_is_not_mutated(self):
        late = ORIGIN + timedelta(hours=5)
        store = _store([late + timedelta(minutes=i) for i in range(5)],
                       coverage_started=late)
        before = len(store.bars("MRVL", SESSION))
        chart = _Chart(_rows(ORIGIN + timedelta(hours=1), 120))
        origin_backfill.restore_origin(
            store, "MRVL", session=SESSION, official_origin=ORIGIN,
            range_minutes=5, broker=object(), exchange="NASDAQ", chart=chart)
        assert len(store.bars("MRVL", SESSION)) == before
        feats = _build(store, now=late + timedelta(minutes=6))
        assert feats.error == kbf.OFFICIAL_ORIGIN_NOT_COVERED
        assert feats.origin_status == kbf.ORIGIN_NOT_COVERED

    def test_no_broker_is_its_own_reason(self):
        store = _store([ORIGIN + timedelta(minutes=1)],
                       coverage_started=ORIGIN + timedelta(seconds=20))
        report = origin_backfill.restore_origin(
            store, "MRVL", session=SESSION, official_origin=ORIGIN,
            range_minutes=5, broker=None)
        assert report["status"] == origin_backfill.NO_BROKER

    def test_a_raising_chart_never_propagates(self):
        class _Boom(_Chart):
            def fetch(self, *a, **k):
                raise RuntimeError("KIS said no")

        store = _store([ORIGIN + timedelta(minutes=1)],
                       coverage_started=ORIGIN + timedelta(seconds=20))
        report = origin_backfill.restore_origin(
            store, "MRVL", session=SESSION, official_origin=ORIGIN,
            range_minutes=5, broker=object(), exchange="NASDAQ",
            chart=_Boom([]))
        assert report["status"] == origin_backfill.FAILED
        assert report["bars_added"] == 0


class TestG_SessionDateLookup:
    """G. The provider wrapper must find the DAYTIME store."""

    def test_collected_store_passes_the_session_date(self):
        import inspect

        from market_data import kis_bar_provider as kbp

        body = inspect.getsource(kbp._collected_store)
        assert "session_date=" in body, (
            "a wrapping session's bars are keyed to the evening it opened, "
            "not to the day it precedes")
        assert "current_session_date" in body


class TestH_OtherSessionsUnchanged:
    """H. REGULAR / PREMARKET / AFTER_HOURS behave exactly as before."""

    @pytest.mark.parametrize("session,origin", [
        ("PREMARKET", datetime(2026, 9, 16, 8, 0, tzinfo=UTC)),
        ("REGULAR", datetime(2026, 9, 16, 13, 30, tzinfo=UTC)),
        ("AFTER_HOURS", datetime(2026, 9, 16, 20, 0, tzinfo=UTC)),
    ])
    def test_a_bar_at_the_origin_still_succeeds(self, session, origin):
        from market_data.realtime_bars import Bar, RealtimeBarStore, SessionAccumulator

        store = RealtimeBarStore(stale_after_seconds=36000)
        store.coverage_started_at = origin - timedelta(seconds=5)
        acc = SessionAccumulator(symbol="AAPL", session=session)
        for i in range(10):
            m = origin + timedelta(minutes=i)
            acc.bars[m] = Bar(symbol="AAPL", session=session, minute=m,
                              open=10.0, high=10.5, low=9.5, close=10.0,
                              volume=100.0, trade_count=1, first_trade_at=m,
                              last_trade_at=m, price_volume=1000.0,
                              source="KIS_HDFSCNT0")
        acc.trade_count = 10
        acc.last_trade_at = origin + timedelta(minutes=9)
        store._accumulators[("AAPL", session)] = acc

        feats = kbf.build_from_bars(
            "AAPL", store=store, session=session,
            now=origin + timedelta(minutes=40), range_minutes=5,
            closed_bar_only=True)
        assert feats.error is None
        assert feats.origin_status == kbf.ORIGIN_AVAILABLE

    def test_an_uncovered_origin_is_still_refused_everywhere(self):
        origin = datetime(2026, 9, 16, 13, 30, tzinfo=UTC)
        from market_data.realtime_bars import Bar, RealtimeBarStore, SessionAccumulator

        store = RealtimeBarStore(stale_after_seconds=36000)
        store.coverage_started_at = origin + timedelta(minutes=30)
        acc = SessionAccumulator(symbol="AAPL", session="REGULAR")
        for i in range(30, 40):
            m = origin + timedelta(minutes=i)
            acc.bars[m] = Bar(symbol="AAPL", session="REGULAR", minute=m,
                              open=10.0, high=10.5, low=9.5, close=10.0,
                              volume=100.0, trade_count=1, first_trade_at=m,
                              last_trade_at=m, price_volume=1000.0,
                              source="KIS_HDFSCNT0")
        acc.trade_count = 10
        acc.last_trade_at = origin + timedelta(minutes=39)
        store._accumulators[("AAPL", "REGULAR")] = acc
        feats = kbf.build_from_bars(
            "AAPL", store=store, session="REGULAR",
            now=origin + timedelta(minutes=40), range_minutes=5,
            closed_bar_only=True)
        assert feats.error == kbf.OFFICIAL_ORIGIN_NOT_COVERED
        assert feats.origin_status == kbf.ORIGIN_NOT_COVERED


class TestTheFastWatchReachesTheBackfill:
    """The fallback condition itself: the old branch never fired."""

    def test_an_origin_refusal_now_reaches_a_fallback(self):
        import inspect

        from s6_live import fast_watch

        body = inspect.getsource(fast_watch.ActiveWatchSource.symbols)
        assert "OFFICIAL_ORIGIN_NOT_COVERED" in body, (
            "these features carry a market_data_asof, so the "
            "asof-is-None branch alone can never reach them")
        assert "_with_backfilled_origin" in body
