"""The S6-applicability boundary: a watchlist entry is not a strategy claim.

`KIS_COLLECTOR_MEMBERSHIP` is set only for a symbol with no S6 discovery
behind it. It must keep its place in the collector's transport state and
must NOT enter S6 feature evaluation.
"""

import pytest

from s6_live import active_watch


class TestOwnershipHelper:
    """The canonical table, asked directly."""

    def test_collector_membership_is_transport_only(self):
        assert active_watch.is_transport_only(
            {"strategy_source": active_watch.COLLECTOR_MEMBERSHIP_SOURCE}) is True

    def test_provisional_pass_is_an_s6_input(self):
        """3. S6_PROVISIONAL_PASS still reaches feature evaluation."""
        assert active_watch.is_transport_only(
            {"strategy_source": active_watch.PROVISIONAL_SOURCE}) is False

    def test_full_discovery_is_an_s6_input(self):
        """4. S6_FULL_DISCOVERY still reaches feature evaluation."""
        assert active_watch.is_transport_only(
            {"strategy_source": active_watch.FULL_DISCOVERY_SOURCE}) is False

    def test_the_older_bare_source_spelling_is_understood(self):
        """Rows read straight off disk carry `source`, not
        `strategy_source` -- the live PREMARKET file did."""
        assert active_watch.is_transport_only(
            {"source": active_watch.COLLECTOR_MEMBERSHIP_SOURCE}) is True
        assert active_watch.is_transport_only(
            {"source": active_watch.PROVISIONAL_SOURCE}) is False

    def test_a_bare_source_string_asks_the_same_question(self):
        assert active_watch.is_transport_only(
            active_watch.COLLECTOR_MEMBERSHIP_SOURCE) is True

    def test_unknown_provenance_stays_eligible(self):
        """Deliberately NOT fail-closed. Dropping a row whose source is
        unreadable would silently starve a real candidate, which is worse
        than evaluating one extra symbol."""
        assert active_watch.is_transport_only({}) is False
        assert active_watch.is_transport_only({"strategy_source": None}) is False
        assert active_watch.is_transport_only({"strategy_source": ""}) is False

    def test_transport_source_does_not_decide_ownership(self):
        """TRANSPORT_WEBSOCKET is HOW data arrives, not WHY it is
        watched. A real S6 candidate is usually websocket-backed too."""
        assert active_watch.is_transport_only({
            "strategy_source": active_watch.PROVISIONAL_SOURCE,
            "transport_source": active_watch.TRANSPORT_WEBSOCKET}) is False


class _Source:
    """The real filter from fast_watch.symbols(), exercised over rows.

    The surrounding method needs a live store, a budget and a conn; the
    boundary itself is the two lines below, so they are exercised against
    the same helper production calls with the same row shapes.
    """

    def __init__(self, entries):
        self._entries = {e["symbol"]: e for e in entries}

    def _entry(self, symbol):
        return self._entries.get(symbol, {})

    def partition(self, offered):
        not_owned = [s for s in offered
                     if active_watch.is_transport_only(self._entry(s))]
        return [s for s in offered if s not in set(not_owned)], not_owned


def _rows():
    """The live 2026-09-15 PREMARKET shape: mostly collector membership."""
    collector = [{"symbol": s, "source": active_watch.COLLECTOR_MEMBERSHIP_SOURCE,
                  "transport_source": active_watch.TRANSPORT_WEBSOCKET}
                 for s in ("INLF", "RKT", "TRGP", "SUGP", "EDHL", "STX",
                           "GMM", "ALC")]
    owned = [
        {"symbol": "META", "source": active_watch.PROVISIONAL_SOURCE,
         "transport_source": active_watch.TRANSPORT_WEBSOCKET},
        {"symbol": "MSFT", "source": active_watch.FULL_DISCOVERY_SOURCE,
         "transport_source": active_watch.TRANSPORT_WEBSOCKET},
    ]
    return collector + owned


class TestBoundary:

    def test_collector_only_symbols_do_not_reach_evaluation(self):
        """2/5. They never reach realtime_features.build(), so they
        cannot emit an S6 DATA_ORIGIN_UNAVAILABLE for a strategy that
        never flagged them."""
        source = _Source(_rows())
        offered = [r["symbol"] for r in _rows()]
        evaluated, excluded = source.partition(offered)
        assert set(excluded) == {"INLF", "RKT", "TRGP", "SUGP", "EDHL",
                                 "STX", "GMM", "ALC"}
        assert "INLF" not in evaluated

    def test_real_s6_candidates_are_untouched(self):
        """3/4/7. Both S6-owned sources still evaluate."""
        source = _Source(_rows())
        evaluated, _ = source.partition([r["symbol"] for r in _rows()])
        assert evaluated == ["META", "MSFT"]

    def test_the_watchlist_itself_is_not_modified(self):
        """1/8. Transport state is untouched -- the rows are still there,
        still websocket-backed. Only entry into S6 evaluation is gated."""
        rows = _rows()
        source = _Source(rows)
        source.partition([r["symbol"] for r in rows])
        assert len(source._entries) == 10
        assert source._entry("INLF")["transport_source"] == \
            active_watch.TRANSPORT_WEBSOCKET
        assert source._entry("INLF")["source"] == \
            active_watch.COLLECTOR_MEMBERSHIP_SOURCE

    def test_exclusion_is_stable_across_ticks(self):
        """6. Not evaluated once and then retried: the same row yields
        the same verdict every tick, so it cannot re-enter."""
        source = _Source(_rows())
        offered = [r["symbol"] for r in _rows()]
        first, excluded_first = source.partition(offered)
        second, excluded_second = source.partition(offered)
        assert first == second
        assert excluded_first == excluded_second

    def test_an_all_collector_watchlist_evaluates_nothing(self):
        """The degenerate case that motivated this: a session whose
        watchlist is entirely transport spends no budget at all."""
        rows = [{"symbol": s,
                 "source": active_watch.COLLECTOR_MEMBERSHIP_SOURCE}
                for s in ("AAA", "BBB", "CCC")]
        evaluated, excluded = _Source(rows).partition(
            [r["symbol"] for r in rows])
        assert evaluated == []
        assert len(excluded) == 3

    def test_an_all_owned_watchlist_is_unchanged(self):
        rows = [{"symbol": s, "source": active_watch.FULL_DISCOVERY_SOURCE}
                for s in ("AAA", "BBB")]
        evaluated, excluded = _Source(rows).partition(
            [r["symbol"] for r in rows])
        assert evaluated == ["AAA", "BBB"]
        assert excluded == []


class TestFastWatchWiring:
    """The real source object, not a stand-in."""

    def _source(self):
        from s6_live.fast_watch import ActiveWatchSource

        return ActiveWatchSource(trading_day="2026-09-15", session="PREMARKET",
                                 rollout=None, env={"S6_ACTIVE_WATCH_DIR": "/tmp"})

    def test_not_s6_owned_defaults_to_empty_before_symbols_runs(self):
        """The funnel report reads this; an unset attribute would raise
        AttributeError on any tick that never reached symbols()."""
        assert self._source().not_s6_owned == []

    def test_the_source_uses_the_canonical_helper(self):
        """The boundary must not re-implement ownership locally -- one
        table, asked in one place."""
        import inspect

        from s6_live.fast_watch import ActiveWatchSource

        body = inspect.getsource(ActiveWatchSource.symbols)
        assert "is_transport_only" in body
        assert "KIS_COLLECTOR_MEMBERSHIP" not in body, (
            "ownership belongs to active_watch, not duplicated here")
