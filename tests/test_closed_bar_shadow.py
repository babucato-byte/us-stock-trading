"""The same features computed twice: as they are, and on closed bars only.

Every live feature is computed over ALL bars, and the last of those is
the minute in progress -- its close is whatever the latest print was and
its volume a fraction of what the minute will finish with. A breakout
read off a partial bar can un-break before the minute ends.

Whether that matters here has never been measured. These tests are about
the measurement being honest: that the shadow really excludes the
in-progress minute, that fields it cannot recompute say so instead of
comparing against themselves, and that computing it changes nothing.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_data import realtime_bars as rb  # noqa: E402
from s6_live import closed_bar_shadow as shadow  # noqa: E402

NOW = datetime(2026, 8, 28, 15, 42, 30, tzinfo=timezone.utc)


def _bar(minute_offset, *, close, volume=100.0, symbol="OWL"):
    minute = NOW.replace(second=0, microsecond=0) + timedelta(minutes=minute_offset)
    return rb.Bar(symbol=symbol, session="REGULAR", minute=minute,
                  open=close, high=close, low=close, close=close,
                  volume=volume, trade_count=5,
                  first_trade_at=minute, last_trade_at=minute)


class _Store:
    def __init__(self, bars):
        self._bars = bars
        self.feed = rb.FEED_LIVE

    def bars(self, symbol, session):
        return list(self._bars)

    def accumulator(self, symbol, session):
        acc = rb.SessionAccumulator(symbol=symbol, session=session)
        for bar in self._bars:
            acc.add(price=bar.close, size=bar.volume, at=bar.minute)
        return acc

    def feed_status(self, *, now=None):
        return self.feed


class TestTheMinuteInProgressIsExcluded:
    def test_the_current_minute_is_not_closed(self):
        """15:42 is still accumulating at every moment inside 15:42."""
        bars = [_bar(-2, close=10.0), _bar(-1, close=11.0), _bar(0, close=12.0)]
        kept = shadow.closed_bars(bars, now=NOW)
        assert [b.close for b in kept] == [10.0, 11.0]

    def test_it_becomes_closed_once_the_minute_passes(self):
        bars = [_bar(-1, close=11.0), _bar(0, close=12.0)]
        kept = shadow.closed_bars(bars, now=NOW + timedelta(minutes=1))
        assert [b.close for b in kept] == [11.0, 12.0]

    def test_a_single_in_progress_bar_leaves_nothing(self):
        """Early-session decisions, which a closed-bar rule would defer."""
        assert shadow.closed_bars([_bar(0, close=12.0)], now=NOW) == []

    def test_no_bars_is_not_an_error(self):
        assert shadow.closed_bars([], now=NOW) == []


class TestTheTwoReadingsAreActuallyDifferent:
    def test_the_partial_bar_moves_the_price(self):
        """The whole premise, made concrete: the live reading is 12.0
        because the minute in progress last printed there, and the last
        finished minute closed at 11.0."""
        store = _Store([_bar(-2, close=10.0), _bar(-1, close=11.0),
                        _bar(0, close=12.0)])
        out = shadow.compare("OWL", store=store, session="REGULAR", now=NOW)
        price = out["fields"]["price"]
        assert price["live"] == pytest.approx(12.0)
        assert price["shadow"] == pytest.approx(11.0)
        assert price["delta"] == pytest.approx(1.0)
        assert price["delta_bps"] == pytest.approx(909.09, abs=1.0)

    def test_the_bar_counts_differ_by_the_open_minute(self):
        store = _Store([_bar(-2, close=10.0), _bar(-1, close=11.0),
                        _bar(0, close=12.0)])
        out = shadow.compare("OWL", store=store, session="REGULAR", now=NOW)
        assert out["live_bar_count"] == 3
        assert out["shadow_bar_count"] == 2

    def test_when_the_shadow_has_nothing_it_says_so(self):
        store = _Store([_bar(0, close=12.0)])
        out = shadow.compare("OWL", store=store, session="REGULAR", now=NOW)
        assert out["shadow_has_nothing"] is True
        assert out["fields"]["price"]["shadow"] is None
        assert out["fields"]["price"]["delta"] is None

    def test_nothing_at_all_is_None(self):
        assert shadow.compare("OWL", store=_Store([]), session="REGULAR",
                              now=NOW) is None


class TestFieldsItCannotRecomputeSayNotComparable:
    """`build_from_bars` takes volume and VWAP from the session
    accumulator, which aggregates trades as they arrive and cannot be
    replayed without the in-progress minute. Comparing those against
    themselves would yield a difference of zero and read as evidence that
    the partial bar does not matter."""

    def test_vwap_and_volume_are_not_compared(self):
        store = _Store([_bar(-1, close=11.0), _bar(0, close=12.0)])
        out = shadow.compare("OWL", store=store, session="REGULAR", now=NOW)
        for field in ("vwap", "volume"):
            assert out["fields"][field]["shadow"] == shadow.NOT_COMPARABLE
            assert out["fields"][field]["delta"] is None

    def test_the_live_value_is_still_recorded(self):
        store = _Store([_bar(-1, close=11.0), _bar(0, close=12.0)])
        out = shadow.compare("OWL", store=store, session="REGULAR", now=NOW)
        assert out["fields"]["volume"]["live"] is not None


class TestComputingItChangesNothing:
    def test_the_view_does_not_write_to_the_store(self):
        source = (REPO_ROOT / "s6_live" / "closed_bar_shadow.py").read_text()
        for forbidden in ("add_trade", "mark_connected", "mark_disconnected",
                          "restore(", "snapshot("):
            assert forbidden not in source, forbidden

    def test_it_places_no_orders_and_reads_no_broker(self):
        source = (REPO_ROOT / "s6_live" / "closed_bar_shadow.py").read_text()
        for forbidden in ("submit_buy", "submit_sell", "execution_engine",
                          "order_gate", "kis_broker", "from brokers"):
            assert forbidden not in source, forbidden

    def test_the_live_reading_is_unaffected_by_running_the_shadow(self):
        """Guarded by comparing the live half before and after."""
        from s6_live import kis_bar_features

        store = _Store([_bar(-1, close=11.0), _bar(0, close=12.0)])
        before = kis_bar_features.build_from_bars(
            "OWL", store=store, session="REGULAR", now=NOW)
        shadow.compare("OWL", store=store, session="REGULAR", now=NOW)
        after = kis_bar_features.build_from_bars(
            "OWL", store=store, session="REGULAR", now=NOW)
        assert before.price == after.price
        assert before.bar_count == after.bar_count

    def test_a_broken_feature_build_returns_None_rather_than_raising(
            self, monkeypatch):
        from s6_live import kis_bar_features

        monkeypatch.setattr(kis_bar_features, "build_from_bars",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError))
        assert shadow.compare("OWL", store=_Store([_bar(-1, close=11.0)]),
                              session="REGULAR", now=NOW) is None


class TestTheSummaryDoesNotHideTheEarlySessionGap:
    @pytest.fixture
    def env(self, tmp_path):
        return {"CLOSED_BAR_SHADOW_DIR": str(tmp_path)}

    def test_rows_with_no_shadow_are_counted_apart(self, env):
        """"No closed bar yet" is not "the two readings agreed", and
        pooling them understates the gap at exactly the moments it is
        largest."""
        shadow.append({"shadow_has_nothing": True, "fields": {}},
                      trading_day="D", env=env)
        shadow.append({"shadow_has_nothing": False,
                       "fields": {"price": {"delta_bps": 900.0}}},
                      trading_day="D", env=env)
        out = shadow.disagreements("D", env=env)
        assert out["observations"] == 2
        assert out["compared"] == 1
        assert out["shadow_had_nothing"] == 1
        assert out["median_abs_bps"] == pytest.approx(900.0)

    def test_direction_does_not_cancel_out(self, env):
        """Two equal gaps in opposite directions are two gaps, not none."""
        for bps in (-500.0, 500.0):
            shadow.append({"shadow_has_nothing": False,
                           "fields": {"price": {"delta_bps": bps}}},
                          trading_day="D", env=env)
        out = shadow.disagreements("D", env=env)
        assert out["median_abs_bps"] == pytest.approx(500.0)
        assert out["max_abs_bps"] == pytest.approx(500.0)

    def test_an_empty_day_reports_nothing_rather_than_zero(self, env):
        out = shadow.disagreements("D", env=env)
        assert out["compared"] == 0
        assert out["median_abs_bps"] is None

    def test_a_corrupt_line_does_not_lose_the_others(self, env):
        shadow.append({"shadow_has_nothing": False,
                       "fields": {"price": {"delta_bps": 100.0}}},
                      trading_day="D", env=env)
        with open(shadow.log_path("D", env=env), "a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        assert shadow.disagreements("D", env=env)["compared"] == 1


class TestWhetherTheDifferenceReachesTheDecision:
    """The feature deltas say the two readings differ. This says whether
    the difference changes the answer -- a 900bps gap in a field no gate
    consults changes nothing, and a small one that crosses a threshold
    changes everything."""

    def test_both_ready_is_agreement(self):
        assert shadow.classify(live_ready=True, shadow_ready=True) \
            == shadow.CLASS_BOTH

    def test_neither_ready_is_also_agreement(self):
        assert shadow.classify(live_ready=False, shadow_ready=False) \
            == shadow.CLASS_NEITHER

    def test_a_signal_the_partial_bar_created(self):
        """The shape that matters: a breakout that can un-break before
        the minute closes."""
        assert shadow.classify(live_ready=True, shadow_ready=False) \
            == shadow.CLASS_LIVE_ONLY

    def test_a_signal_the_partial_bar_is_suppressing(self):
        assert shadow.classify(live_ready=False, shadow_ready=True) \
            == shadow.CLASS_SHADOW_ONLY

    def test_the_comparison_records_both_verdicts(self, monkeypatch):
        from s6_live import precision_watch

        class _Eval:
            def __init__(self, ready):
                self.state = "READY_TO_BUY" if ready else "WATCHING"
                self.blocking = [] if ready else ["volume_expansion"]

            @property
            def ready(self):
                return self.state == "READY_TO_BUY"

        seen = []

        def _fake(symbol, *, session, now, features, conn=None, **k):
            # The live view has three bars, the shadow two.
            ready = features.bar_count == 3
            seen.append(features.bar_count)
            return _Eval(ready)

        monkeypatch.setattr(precision_watch, "evaluate", _fake)
        store = _Store([_bar(-2, close=10.0), _bar(-1, close=11.0),
                        _bar(0, close=12.0)])
        out = shadow.compare_readiness("OWL", store=store, session="REGULAR",
                                       now=NOW)
        assert sorted(seen) == [2, 3]
        assert out["live_ready"] is True
        assert out["shadow_ready"] is False
        assert out["classification"] == shadow.CLASS_LIVE_ONLY
        assert out["shadow_blocking"] == ["volume_expansion"]

    def test_a_failing_evaluation_does_not_raise(self, monkeypatch):
        from s6_live import precision_watch

        monkeypatch.setattr(
            precision_watch, "evaluate",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        store = _Store([_bar(-1, close=11.0), _bar(0, close=12.0)])
        out = shadow.compare_readiness("OWL", store=store, session="REGULAR",
                                       now=NOW)
        assert out["live_ready"] is False
        assert out["classification"] == shadow.CLASS_NEITHER

    def test_no_bars_is_None(self):
        assert shadow.compare_readiness("OWL", store=_Store([]),
                                        session="REGULAR", now=NOW) is None


class TestCountingWhoSignalled:
    @pytest.fixture
    def env(self, tmp_path):
        return {"CLOSED_BAR_SHADOW_DIR": str(tmp_path)}

    def test_it_counts_each_classification(self, env):
        for name in (shadow.CLASS_BOTH, shadow.CLASS_LIVE_ONLY,
                     shadow.CLASS_LIVE_ONLY, shadow.CLASS_SHADOW_ONLY):
            shadow.append({"classification": name}, trading_day="D", env=env)
        counts = shadow.classification_counts("D", env=env)
        assert counts[shadow.CLASS_LIVE_ONLY] == 2
        assert counts[shadow.CLASS_SHADOW_ONLY] == 1
        assert counts[shadow.CLASS_BOTH] == 1
        assert counts[shadow.CLASS_NEITHER] == 0

    def test_feature_only_rows_are_not_counted_as_agreement(self, env):
        """A plain feature comparison carries no readiness. Folding those
        into NEITHER would report the readings agreeing every time
        nobody asked the question."""
        shadow.append({"fields": {"price": {"delta_bps": 10.0}}},
                      trading_day="D", env=env)
        assert shadow.classification_counts("D", env=env) == {
            shadow.CLASS_BOTH: 0, shadow.CLASS_LIVE_ONLY: 0,
            shadow.CLASS_SHADOW_ONLY: 0, shadow.CLASS_NEITHER: 0}


class TestScoringTheSignalsAfterTheFact:
    """Classification says the two readings disagreed. This says which
    of them was right -- the entire point of collecting the dataset."""

    def test_forward_returns_at_each_mark(self):
        store = _Store([_bar(0, close=10.0), _bar(5, close=11.0),
                        _bar(15, close=9.0)])
        out = shadow.forward_returns("OWL", store=store, session="REGULAR",
                                     signal_at=NOW.replace(second=0),
                                     signal_price=10.0)
        assert out["T+5"] == pytest.approx(10.0)
        assert out["T+15"] == pytest.approx(-10.0)

    def test_a_mark_with_no_nearby_bar_is_None_not_flat(self):
        """Substituting a distant bar would let a quiet symbol look like
        it held its move."""
        store = _Store([_bar(0, close=10.0)])
        out = shadow.forward_returns("OWL", store=store, session="REGULAR",
                                     signal_at=NOW.replace(second=0),
                                     signal_price=10.0)
        assert out["T+60"] is None

    def test_no_signal_price_scores_nothing(self):
        store = _Store([_bar(5, close=11.0)])
        out = shadow.forward_returns("OWL", store=store, session="REGULAR",
                                     signal_at=NOW, signal_price=None)
        assert all(v is None for v in out.values())

    def test_an_unreadable_store_does_not_raise(self):
        class _Broken:
            def bars(self, *a):
                raise RuntimeError("gone")

        out = shadow.forward_returns("OWL", store=_Broken(), session="REGULAR",
                                     signal_at=NOW, signal_price=10.0)
        assert set(out) == {"T+5", "T+15", "T+30", "T+60"}


class TestTheScoreboardDoesNotFlattenTheGaps:
    @pytest.fixture
    def env(self, tmp_path):
        return {"CLOSED_BAR_SHADOW_DIR": str(tmp_path)}

    def test_each_classification_is_scored_separately(self, env):
        """LIVE_ONLY signals are the ones the partial bar created;
        pooling them with BOTH hides exactly what is being asked."""
        for name, ret in ((shadow.CLASS_LIVE_ONLY, -2.0),
                          (shadow.CLASS_LIVE_ONLY, -1.0),
                          (shadow.CLASS_BOTH, 3.0)):
            shadow.append({"classification": name,
                           "forward_returns": {"T+15": ret}},
                          trading_day="D", env=env)
        out = shadow.score_classifications("D", env=env)["by_classification"]
        assert out[shadow.CLASS_LIVE_ONLY]["signals"] == 2
        assert out[shadow.CLASS_LIVE_ONLY]["median_return_pct"] == pytest.approx(-1.0)
        assert out[shadow.CLASS_LIVE_ONLY]["win_rate"] == pytest.approx(0.0)
        assert out[shadow.CLASS_BOTH]["win_rate"] == pytest.approx(1.0)

    def test_an_unreadable_mark_is_counted_but_not_scored(self, env):
        """A mark that could not be read is not a flat return, and
        averaging it in as zero drags every group toward 'no difference'."""
        shadow.append({"classification": shadow.CLASS_LIVE_ONLY,
                       "forward_returns": {"T+15": None}},
                      trading_day="D", env=env)
        out = shadow.score_classifications("D", env=env)["by_classification"]
        assert out[shadow.CLASS_LIVE_ONLY]["signals"] == 1
        assert out[shadow.CLASS_LIVE_ONLY]["scored"] == 0
        assert out[shadow.CLASS_LIVE_ONLY]["unscored"] == 1
        assert out[shadow.CLASS_LIVE_ONLY]["median_return_pct"] is None

    def test_an_empty_day_scores_nothing(self, env):
        assert shadow.score_classifications("D", env=env)["by_classification"] == {}
