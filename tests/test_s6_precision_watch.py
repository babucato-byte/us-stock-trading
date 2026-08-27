"""Being on the hourly list is no longer a reason to buy.

The trade this file is about
----------------------------
DT, 2026-08-26. The scanner republished a candidate every fifteen
minutes whose price, volume, VWAP and EMAs were bit-identical for three
hours -- it was serving regular-session data under a new
`generated_at`. The entry path saw a fresh timestamp, bought at 52.75 in
a zero-volume after-hours book, and landed 4.01% above the breakout
range. Every gate passed; none of them asked what the market was doing
at that moment.

The four tests in TestTheDTSituationCannotReachReady are that trade,
frozen. Each of the four independently prevents it.

No network access and no orders: features are constructed directly.
"""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s6_live import precision_watch as pw  # noqa: E402
from s6_live import realtime_features as rf  # noqa: E402

NOW = datetime(2026, 8, 26, 18, 0, tzinfo=timezone.utc)


def _features(**overrides):
    """A candidate that passes everything, unless overridden."""
    kwargs = dict(
        symbol="DT", session="REGULAR",
        market_data_asof=NOW - timedelta(minutes=1), built_at=NOW,
        price=51.5, vwap=51.0, ema9=51.4, ema21=51.2,
        volume=250_000.0, volume_status=rf.VOLUME_OK, volume_expansion=1.8,
        range_high=50.75, range_low=49.0, extension_pct=1.48, bar_count=40,
    )
    kwargs.update(overrides)
    return rf.SessionFeatures(**kwargs)


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def _eval(features=None, **kwargs):
    return pw.evaluate("DT", session="REGULAR", now=NOW,
                       features=features if features is not None else _features(),
                       **kwargs)


class TestAHealthyCandidateBecomesReady:
    def test_all_conditions_passing_is_ready(self):
        out = _eval()
        assert out.state == pw.READY_TO_BUY
        assert out.ready is True
        assert out.blocking == []

    def test_every_condition_is_reported(self):
        out = _eval()
        assert set(out.conditions) == set(pw.CONDITION_ORDER)
        assert all(v == pw.PASS for v in out.conditions.values())


class TestTheDTSituationCannotReachReady:
    """§42 -- four independent reasons that BUY never happens again."""

    def test_stale_market_data_blocks_ready(self):
        """`generated_at` fresh, market data hours old -- the exact
        deceit in the DT candidate."""
        out = _eval(_features(market_data_asof=NOW - timedelta(hours=3)))
        assert not out.ready
        assert out.conditions[pw.C_MARKET_DATA_FRESH] == pw.FAIL

    def test_unavailable_volume_blocks_ready(self):
        """The after-hours book: every bar zero, so the volume condition
        cannot be judged at all."""
        out = _eval(_features(volume_status=rf.VOLUME_DATA_UNAVAILABLE,
                              volume_expansion=None))
        assert not out.ready
        assert out.conditions[pw.C_VOLUME_VALID] == pw.UNAVAILABLE
        assert out.conditions[pw.C_VOLUME_EXPANSION] == pw.UNAVAILABLE

    def test_zero_volume_confirmed_also_blocks_ready(self):
        out = _eval(_features(volume_status=rf.VOLUME_ZERO_CONFIRMED,
                              volume_expansion=None))
        assert not out.ready
        assert out.conditions[pw.C_VOLUME_VALID] == pw.UNAVAILABLE

    def test_extension_is_measured_at_the_price_we_would_pay(self):
        """DT was 1.76% extended when scanned and 4.01% when bought.
        Both are under the 6.0% ceiling, so this alone would NOT have
        stopped that trade -- the test states the wiring, not a fix."""
        out = _eval(_features(price=52.75, extension_pct=4.01))
        assert out.conditions[pw.C_EXTENSION] == pw.PASS
        assert out.detail["extension_pct"] == 4.01
        assert out.detail["max_extension_pct"] == 6.0

    def test_an_extension_past_the_ceiling_does_block(self):
        out = _eval(_features(extension_pct=7.5))
        assert not out.ready
        assert out.conditions[pw.C_EXTENSION] == pw.FAIL
        assert out.state == pw.INVALIDATED

    def test_a_same_day_exit_blocks_ready(self, conn):
        from s6_live import position_store as s6ps

        pid = s6ps.record_submission(conn, symbol="DT", variant="S6-R",
                                     entry_session="REGULAR",
                                     client_order_id="k1", now=NOW)
        s6ps.open_from_fill(conn, pid, quantity=1, average_fill_price=50.79,
                            venue="NYSE", now=NOW)
        s6ps.close_position(conn, pid, reason="RANGE_REENTRY",
                            exit_price=50.87, now=NOW)
        out = _eval(conn=conn)
        assert not out.ready
        assert out.conditions[pw.C_REENTRY] == pw.FAIL

    def test_the_whole_DT_after_hours_view_is_refused(self):
        """Everything as it actually was at 20:41Z, together."""
        out = _eval(_features(
            session="AFTER_HOURS", market_data_asof=NOW - timedelta(hours=3),
            price=52.75, volume_status=rf.VOLUME_DATA_UNAVAILABLE,
            volume_expansion=None, extension_pct=4.01))
        assert not out.ready
        assert pw.C_MARKET_DATA_FRESH in out.blocking
        assert pw.C_VOLUME_VALID in out.blocking


class TestStrategyConditionsAreRecheckedNotAssumed:
    def test_price_falling_below_vwap_blocks_ready(self):
        out = _eval(_features(price=50.9, vwap=51.0))
        assert not out.ready
        assert out.conditions[pw.C_PRICE_ABOVE_VWAP] == pw.FAIL
        assert out.state == pw.INVALIDATED

    def test_ema_structure_turning_over_blocks_ready(self):
        out = _eval(_features(ema9=51.1, ema21=51.2))
        assert not out.ready
        assert out.conditions[pw.C_EMA_STRUCTURE] == pw.FAIL

    def test_falling_back_inside_the_range_blocks_ready(self):
        out = _eval(_features(price=50.5, range_high=50.75, vwap=50.0))
        assert not out.ready
        assert out.conditions[pw.C_BREAKOUT] == pw.FAIL

    def test_volume_expansion_below_the_scanners_minimum_blocks_ready(self):
        out = _eval(_features(volume_expansion=1.0))
        assert not out.ready
        assert out.conditions[pw.C_VOLUME_EXPANSION] == pw.FAIL

    def test_the_thresholds_come_from_the_scanner_config(self):
        """Not invented here: the watch and the scanner must agree."""
        out = _eval()
        assert out.detail["volume_expansion_min"] == 1.2
        assert out.detail["max_extension_pct"] == 6.0


class TestUnavailableBlocksReadyLikeFailure:
    """The lesson of the exit-rule defect, applied to entry."""

    @pytest.mark.parametrize("override,condition", [
        (dict(vwap=None), pw.C_VWAP_AVAILABLE),
        (dict(ema9=None), pw.C_EMA_AVAILABLE),
        (dict(ema21=None), pw.C_EMA_AVAILABLE),
        (dict(price=None), pw.C_PRICE),
        (dict(range_high=None, extension_pct=None), pw.C_BREAKOUT),
    ])
    def test_a_missing_input_is_unavailable_and_blocks(self, override, condition):
        out = _eval(_features(**override))
        assert not out.ready
        assert out.conditions[condition] == pw.UNAVAILABLE

    def test_unreadable_reentry_history_is_unavailable_not_permission(
            self, conn, monkeypatch):
        from execution import reentry_policy

        monkeypatch.setattr(
            reentry_policy, "blocked_symbols",
            lambda *a, **k: (_ for _ in ()).throw(
                reentry_policy.ReentryStateUnavailable("unreadable")))
        out = _eval(conn=conn)
        assert not out.ready
        assert out.conditions[pw.C_REENTRY] == pw.UNAVAILABLE

    def test_a_broken_evaluation_invalidates_rather_than_raises(self):
        class _Boom:
            def __getattr__(self, name):
                raise RuntimeError("boom")

        out = pw.evaluate("DT", session="REGULAR", now=NOW, features=_Boom())
        assert out.state == pw.INVALIDATED
        assert "evaluation failed" in out.reason


class TestWatchingVersusInvalidated:
    """§5 -- 'not ready yet' and 'thesis broken' are different."""

    def test_a_missing_input_is_only_watching(self):
        out = _eval(_features(vwap=None))
        assert out.state == pw.WATCHING

    def test_a_broken_thesis_is_invalidated(self):
        out = _eval(_features(price=50.9, vwap=51.0))
        assert out.state == pw.INVALIDATED

    def test_stale_data_alone_is_watching_not_invalidated(self):
        """The market did not break; we simply cannot see it."""
        out = _eval(_features(market_data_asof=NOW - timedelta(hours=3)))
        assert out.state == pw.WATCHING


class TestRankingIsTheScanners:
    def test_ready_candidates_come_back_in_scanner_rank_order(self):
        made = [
            pw.WatchEvaluation(symbol=s, session="REGULAR",
                               state=pw.READY_TO_BUY)
            for s in ("CCC", "AAA", "BBB")]
        candidates = [{"symbol": "AAA", "rank": 1, "score": 90.0},
                      {"symbol": "BBB", "rank": 2, "score": 80.0},
                      {"symbol": "CCC", "rank": 3, "score": 70.0}]
        assert [e.symbol for e in pw.rank_ready(made, candidates)] == \
            ["AAA", "BBB", "CCC"]

    def test_non_ready_candidates_are_excluded(self):
        made = [pw.WatchEvaluation(symbol="AAA", session="R", state=pw.WATCHING),
                pw.WatchEvaluation(symbol="BBB", session="R",
                                   state=pw.READY_TO_BUY)]
        assert [e.symbol for e in pw.rank_ready(made, [])] == ["BBB"]


class TestTheRecord:
    def test_as_record_carries_lineage_and_conditions(self):
        out = _eval(candidate={"rank": 4, "score": 72.82,
                               "generated_at": "2026-08-26T20:38:49+00:00",
                               "generation_id": "gen-1",
                               "candidate_id": "cand-1"})
        record = out.as_record(NOW)
        assert record["state"] == pw.READY_TO_BUY
        assert record["detail"]["candidate"]["rank"] == 4
        assert record["detail"]["candidate"]["generation_id"] == "gen-1"
        assert "features" in record
        # The two timestamps that DT conflated, both present.
        assert record["features"]["market_data_asof"]
        assert record["evaluated_at"]


class TestTheWatchGatesTheLiveEntryPath:
    """A watch nothing calls is a library, not a safeguard."""

    class _Inner:
        name = "s6_orb_breakout"

        def __init__(self, symbols, rows=None):
            self._symbols = list(symbols)
            self._rows = rows or {}
            self.described = {"source": "s6_orb_breakout"}

        def symbols(self):
            return list(self._symbols)

        def allowed_symbols(self):
            return frozenset(self._symbols)

        def candidate_row(self, symbol):
            return self._rows.get(symbol)

        def describe(self):
            return dict(self.described)

        def qualify(self, symbol, **kwargs):
            return "QUALIFIED"

    def _wrapped(self, monkeypatch, ready_map):
        inner = self._Inner(sorted(ready_map))
        monkeypatch.setattr(
            pw, "evaluate",
            lambda symbol, **kw: pw.WatchEvaluation(
                symbol=symbol, session="REGULAR",
                state=pw.READY_TO_BUY if ready_map[symbol] else pw.WATCHING,
                conditions={n: (pw.PASS if ready_map[symbol] else pw.FAIL)
                            for n in pw.CONDITION_ORDER}))
        return pw.WatchedCandidateSource(inner, session="REGULAR", now=NOW)

    def test_only_ready_candidates_are_offered(self, monkeypatch):
        source = self._wrapped(monkeypatch, {"AAA": True, "BBB": False})
        assert source.symbols() == ["AAA"]

    def test_nothing_ready_offers_nothing(self, monkeypatch):
        source = self._wrapped(monkeypatch, {"AAA": False, "BBB": False})
        assert source.symbols() == []

    def test_it_can_never_offer_a_symbol_the_source_did_not(self, monkeypatch):
        source = self._wrapped(monkeypatch, {"AAA": True})
        assert set(source.symbols()) <= set(source._inner.symbols())

    def test_the_operator_allow_list_is_untouched(self, monkeypatch):
        """Readiness is the market's question; the allow-list is the
        operator's. Folding one into the other would hide both."""
        source = self._wrapped(monkeypatch, {"AAA": True, "BBB": False})
        assert source.allowed_symbols() == frozenset({"AAA", "BBB"})

    def test_the_route_matching_name_is_delegated_not_shadowed(self, monkeypatch):
        """kis_live_trading routes S6 through the capability resolver by
        matching on `name`; a wrapper answering None changes the route."""
        source = self._wrapped(monkeypatch, {"AAA": True})
        assert source.name == "s6_orb_breakout"

    def test_qualify_still_reaches_the_inner_source(self, monkeypatch):
        source = self._wrapped(monkeypatch, {"AAA": True})
        assert source.qualify("AAA") == "QUALIFIED"

    def test_describe_reports_why_each_candidate_was_dropped(self, monkeypatch):
        source = self._wrapped(monkeypatch, {"AAA": True, "BBB": False})
        source.symbols()
        described = source.describe()["precision_watch"]
        assert described["AAA"]["state"] == pw.READY_TO_BUY
        assert described["BBB"]["state"] == pw.WATCHING
        assert described["BBB"]["blocking"]

    def test_the_entry_script_wraps_the_source(self):
        source = (REPO_ROOT / "scripts" / "run_live_buy_entry.py").read_text(
            encoding="utf-8")
        assert "WatchedCandidateSource(" in source
