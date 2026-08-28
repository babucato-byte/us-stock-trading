"""One-minute bars from KIS trades, and what happens when data is missing.

The bars S6's premarket and after-hours features are computed from. Every
test here is about a decision made when the data is not what we hoped,
because those are the decisions that turn into wrong trades:

  * volume is SUMMED from EVOL, never differenced from KIS's cumulative
    counter, so a reconnect or a session-boundary reset cannot silently
    change it;
  * VWAP comes from this session's own trades, because TAMT/TVOL is only
    a session VWAP if KIS scopes those counters to the session, and that
    is someone else's semantics;
  * a gap is recorded, never backfilled, because a minute with no data
    and a minute in which nothing traded are different facts and only
    one of them is information.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_data import kis_hdfscnt0 as wire  # noqa: E402
from market_data.realtime_bars import (  # noqa: E402
    FEED_DISCONNECTED,
    FEED_LIVE,
    FEED_STALE,
    RealtimeBarStore,
    SessionAccumulator,
    parse_trade_time,
)

PRE = "PREMARKET"
REG = "REGULAR"
AH = "AFTER_HOURS"


def _record(symbol="AAPL", price="231.50", size="10", date="20260828",
            local_time="081500", cumulative=None, amount=None):
    record = {name: "" for name in wire.FIELDS}
    record.update({
        "SYMB": symbol, wire.FIELD_PRICE: price, wire.FIELD_TRADE_SIZE: size,
        wire.FIELD_LOCAL_DATE: date, wire.FIELD_LOCAL_TIME: local_time,
        "layout_mismatch": False, "source": wire.SOURCE,
    })
    if cumulative is not None:
        record[wire.FIELD_CUMULATIVE] = str(cumulative)
    if amount is not None:
        record[wire.FIELD_AMOUNT] = str(amount)
    return record


class TestBarsAreKeyedOnTheTradesOwnTime:
    def test_the_local_stamp_becomes_utc(self):
        """08:15:00 Eastern in August is 12:15 UTC."""
        at = parse_trade_time(_record(local_time="081500"))
        assert at == datetime(2026, 8, 28, 12, 15, tzinfo=timezone.utc)

    def test_arrival_time_is_not_used(self):
        """A trade must land in the minute it HAPPENED. Keying on arrival
        would move it whenever the socket is behind, which is exactly
        when the extra volume matters."""
        store = RealtimeBarStore()
        minute = store.add_trade(_record(local_time="081537"), session=PRE)
        assert minute == datetime(2026, 8, 28, 12, 15, tzinfo=timezone.utc)

    def test_an_unparsable_stamp_is_dropped_and_counted(self):
        store = RealtimeBarStore()
        assert store.add_trade(_record(local_time="xx"), session=PRE) is None
        assert store.dropped_unparsable == 1


class TestOHLCV:
    def test_a_single_trade_makes_a_one_trade_bar(self):
        store = RealtimeBarStore()
        store.add_trade(_record(price="100", size="5"), session=PRE)
        bar = store.bars("AAPL", PRE)[0]
        assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 100.0, 100.0, 100.0)
        assert bar.volume == 5.0
        assert bar.trade_count == 1

    def test_several_trades_in_one_minute_make_one_bar(self):
        store = RealtimeBarStore()
        for price, size, t in (("100", "5", "081501"), ("103", "2", "081530"),
                               ("99", "3", "081559")):
            store.add_trade(_record(price=price, size=size, local_time=t),
                            session=PRE)
        bars = store.bars("AAPL", PRE)
        assert len(bars) == 1
        bar = bars[0]
        assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 103.0, 99.0, 99.0)
        assert bar.volume == 10.0
        assert bar.trade_count == 3

    def test_the_next_minute_starts_a_new_bar(self):
        store = RealtimeBarStore()
        store.add_trade(_record(local_time="081559"), session=PRE)
        store.add_trade(_record(local_time="081600"), session=PRE)
        assert len(store.bars("AAPL", PRE)) == 2

    def test_bars_come_back_in_time_order(self):
        store = RealtimeBarStore()
        for t in ("081700", "081500", "081600"):
            store.add_trade(_record(local_time=t), session=PRE)
        minutes = [b.minute for b in store.bars("AAPL", PRE)]
        assert minutes == sorted(minutes)

    def test_a_zero_size_print_is_not_a_trade(self):
        store = RealtimeBarStore()
        assert store.add_trade(_record(size="0"), session=PRE) is None
        assert store.bars("AAPL", PRE) == []


class TestVolumeIsSummedNotDifferenced:
    def test_volume_is_the_sum_of_trade_sizes(self):
        store = RealtimeBarStore()
        store.add_trade(_record(size="10", cumulative=1000), session=PRE)
        store.add_trade(_record(size="7", local_time="081530", cumulative=1007),
                        session=PRE)
        assert store.accumulator("AAPL", PRE).volume == 17.0

    def test_a_cumulative_counter_reset_does_not_change_volume(self):
        """A counter that restarts mid-session -- which is what a session
        boundary or a feed restart can look like -- would make a
        difference-based volume go negative. The sum does not care."""
        store = RealtimeBarStore()
        store.add_trade(_record(size="10", cumulative=5000), session=PRE)
        store.add_trade(_record(size="6", local_time="081530", cumulative=6),
                        session=PRE)
        assert store.accumulator("AAPL", PRE).volume == 16.0

    def test_the_disagreement_is_reported_not_hidden(self):
        store = RealtimeBarStore()
        store.add_trade(_record(size="10", cumulative=1000), session=PRE)
        store.add_trade(_record(size="10", local_time="081530", cumulative=1500),
                        session=PRE)
        check = store.accumulator("AAPL", PRE).volume_cross_check()
        assert check["summed_volume"] == 20.0
        assert check["kis_cumulative_delta"] == 500.0
        assert check["agrees"] is False

    def test_agreement_is_recognised(self):
        store = RealtimeBarStore()
        store.add_trade(_record(size="10", cumulative=1000), session=PRE)
        store.add_trade(_record(size="10", local_time="081530", cumulative=1010),
                        session=PRE)
        assert store.accumulator("AAPL", PRE).volume_cross_check()["agrees"] is True


class TestVWAP:
    def test_it_is_computed_from_this_sessions_trades(self):
        store = RealtimeBarStore()
        store.add_trade(_record(price="100", size="10"), session=PRE)
        store.add_trade(_record(price="110", size="10", local_time="081530"),
                        session=PRE)
        assert store.accumulator("AAPL", PRE).vwap == pytest.approx(105.0)

    def test_it_is_none_without_volume(self):
        """A fabricated denominator would produce a price a strategy
        compares against, and being wrong there is worse than abstaining."""
        assert SessionAccumulator(symbol="AAPL", session=PRE).vwap is None

    def test_the_kis_ratio_is_kept_beside_it_not_instead_of_it(self):
        store = RealtimeBarStore()
        store.add_trade(_record(price="100", size="10", cumulative=1000,
                                amount=90000), session=PRE)
        accumulator = store.accumulator("AAPL", PRE)
        assert accumulator.vwap == pytest.approx(100.0)
        assert accumulator.vwap_from_kis_cumulative == pytest.approx(90.0)

    def test_the_kis_ratio_is_none_when_its_inputs_are_missing(self):
        store = RealtimeBarStore()
        store.add_trade(_record(price="100", size="10"), session=PRE)
        assert store.accumulator("AAPL", PRE).vwap_from_kis_cumulative is None


class TestSessionIsolation:
    def test_two_sessions_keep_separate_books(self):
        store = RealtimeBarStore()
        store.add_trade(_record(price="100", size="10"), session=PRE)
        store.add_trade(_record(price="200", size="10", local_time="100000"),
                        session=REG)
        assert store.accumulator("AAPL", PRE).vwap == pytest.approx(100.0)
        assert store.accumulator("AAPL", REG).vwap == pytest.approx(200.0)

    def test_a_premarket_print_never_reaches_the_regular_vwap(self):
        store = RealtimeBarStore()
        store.add_trade(_record(price="100", size="1000"), session=PRE)
        store.add_trade(_record(price="200", size="10", local_time="100000"),
                        session=REG)
        assert store.accumulator("AAPL", REG).volume == 10.0

    def test_after_hours_is_its_own_session_too(self):
        store = RealtimeBarStore()
        store.add_trade(_record(price="300", size="4", local_time="170000"),
                        session=AH)
        assert store.accumulator("AAPL", AH).vwap == pytest.approx(300.0)
        assert store.accumulator("AAPL", REG) is None

    def test_bars_are_not_shared_between_sessions(self):
        store = RealtimeBarStore()
        store.add_trade(_record(local_time="081500"), session=PRE)
        store.add_trade(_record(local_time="081500"), session=REG)
        assert len(store.bars("AAPL", PRE)) == 1
        assert len(store.bars("AAPL", REG)) == 1


class TestGapsAreRecordedNeverFilled:
    def test_a_reconnect_records_the_gap(self):
        store = RealtimeBarStore()
        base = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        store.mark_disconnected(now=base)
        store.mark_connected(now=base + timedelta(seconds=90))
        assert len(store.gaps) == 1
        assert store.gaps[0]["seconds"] == 90.0
        assert store.gaps[0]["kind"] == "DATA_GAP"

    def test_no_empty_bar_is_manufactured_for_the_gap(self):
        """The one reading missing data must never have is "nothing
        traded"."""
        store = RealtimeBarStore()
        base = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        store.add_trade(_record(local_time="080000"), session=PRE)
        store.mark_disconnected(now=base)
        store.mark_connected(now=base + timedelta(minutes=5))
        assert len(store.bars("AAPL", PRE)) == 1

    def test_a_first_connection_is_not_a_gap(self):
        store = RealtimeBarStore()
        store.mark_connected(now=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))
        assert store.gaps == []


class TestFeedStatus:
    def test_a_recent_trade_is_live(self):
        store = RealtimeBarStore()
        store.add_trade(_record(local_time="081500"), session=PRE)
        now = datetime(2026, 8, 28, 12, 15, 30, tzinfo=timezone.utc)
        assert store.feed_status(now=now) == FEED_LIVE

    def test_an_old_trade_is_stale(self):
        store = RealtimeBarStore()
        store.add_trade(_record(local_time="081500"), session=PRE)
        now = datetime(2026, 8, 28, 12, 25, tzinfo=timezone.utc)
        assert store.feed_status(now=now) == FEED_STALE

    def test_a_disconnect_outranks_a_recent_trade(self):
        store = RealtimeBarStore()
        store.add_trade(_record(local_time="081500"), session=PRE)
        now = datetime(2026, 8, 28, 12, 15, 30, tzinfo=timezone.utc)
        store.mark_disconnected(now=now)
        assert store.feed_status(now=now) == FEED_DISCONNECTED

    def test_no_trades_yet_is_unknown_not_stale(self):
        """Before the first print there is nothing to be stale about."""
        assert RealtimeBarStore().feed_status(
            now=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)) == "UNKNOWN"


class TestRestartRecovery:
    def test_a_restart_does_not_read_as_an_empty_session(self):
        """The failure this exists for: restarting the collector mid
        session and having the strategy see volume=0 and VWAP=None, which
        is indistinguishable from a session in which nothing traded."""
        store = RealtimeBarStore()
        store.add_trade(_record(price="100", size="10", cumulative=1000,
                                amount=100000), session=PRE)
        store.add_trade(_record(price="110", size="10", local_time="081600"),
                        session=PRE)

        restored = RealtimeBarStore.restore(store.snapshot())
        assert restored.accumulator("AAPL", PRE).volume == 20.0
        assert restored.accumulator("AAPL", PRE).vwap == pytest.approx(105.0)
        assert len(restored.bars("AAPL", PRE)) == 2

    def test_bar_shape_survives_the_round_trip(self):
        store = RealtimeBarStore()
        for price, size, t in (("100", "5", "081501"), ("103", "2", "081530")):
            store.add_trade(_record(price=price, size=size, local_time=t),
                            session=PRE)
        restored = RealtimeBarStore.restore(store.snapshot())
        bar = restored.bars("AAPL", PRE)[0]
        assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 103.0, 100.0, 103.0)
        assert bar.volume == 7.0
        assert bar.trade_count == 2

    def test_gaps_survive_the_round_trip(self):
        store = RealtimeBarStore()
        base = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        store.mark_disconnected(now=base)
        store.mark_connected(now=base + timedelta(seconds=30))
        assert RealtimeBarStore.restore(store.snapshot()).gaps[0]["seconds"] == 30.0

    def test_an_unusable_snapshot_restores_empty_rather_than_wrong(self):
        assert RealtimeBarStore.restore({"version": 99}).bars("AAPL", PRE) == []
        assert RealtimeBarStore.restore(None).bars("AAPL", PRE) == []


class TestALayoutMismatchIsNotIngested:
    def test_a_flagged_record_never_becomes_a_bar(self):
        store = RealtimeBarStore()
        assert store.add_trade({"layout_mismatch": True}, session=PRE) is None
        assert store.layout_mismatches == 1
        assert store.bars("AAPL", PRE) == []
