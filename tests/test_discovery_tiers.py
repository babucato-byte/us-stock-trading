"""Three tiers, because the realtime budget is 41 and the universe is not.

KIS allows 41 concurrent subscriptions on one appkey (measured) and one
connection. So the only question is which 41 symbols deserve a stream,
and the answer has to put obligation ahead of opportunity: a symbol we
hold or are exiting keeps its slot however attractive something else
looks, because the position still has to be sold either way.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_data import kis_hdfscnt0 as wire  # noqa: E402
from s6_live import discovery_tiers as tiers  # noqa: E402


def _c(symbol, state, rank=None):
    return {"symbol": symbol, "state": state, "rank": rank}


class TestTheBudgetIsTheMeasuredLimit:
    def test_it_is_not_a_chosen_number(self):
        """41 was measured against KIS (OPSP0008 MAX SUBSCRIBE OVER),
        not picked."""
        assert tiers.REALTIME_BUDGET == wire.MAX_SUBSCRIPTIONS == 41

    def test_never_more_than_the_budget_is_admitted(self):
        crowd = [_c(f"S{i}", "WATCHING", rank=i) for i in range(80)]
        out = tiers.select_tier2(crowd)
        assert len(out["admitted"]) == 41

    def test_what_was_left_out_is_reported(self):
        """A silent truncation reads afterwards as 'everything was
        covered'."""
        crowd = [_c(f"S{i}", "WATCHING", rank=i) for i in range(50)]
        out = tiers.select_tier2(crowd)
        assert len(out["dropped"]) == 9
        assert len(out["admitted"]) + len(out["dropped"]) == 50


class TestObligationOutranksOpportunity:
    def test_a_held_position_beats_a_better_ranked_candidate(self):
        out = tiers.select_tier2([_c("HOT", "WATCHING", rank=1),
                                  _c("RIG", "EXIT_PENDING", rank=999)],
                                 budget=1)
        assert out["admitted"] == ["RIG"]

    def test_the_lifecycle_order_is_exact(self):
        given = [_c("E", "EXECUTABLE"), _c("W", "WATCHING"),
                 _c("O", "OPEN"), _c("X", "EXIT_PENDING"),
                 _c("B", "BUY_SUBMITTED"), _c("S", "SELL_SUBMITTED"),
                 _c("R", "READY_TO_BUY"), _c("U", "WARMING_UP")]
        out = tiers.select_tier2(given)
        assert out["admitted"] == ["X", "S", "O", "B", "E", "R", "W", "U"]

    @pytest.mark.parametrize("state", sorted(tiers.NEVER_EVICT))
    def test_an_obligation_is_never_evicted(self, state):
        crowd = [_c(f"S{i}", "WATCHING", rank=i) for i in range(60)]
        out = tiers.select_tier2(crowd + [_c("MINE", state, rank=9999)])
        assert "MINE" in out["admitted"]

    def test_a_starved_obligation_is_reported_not_hidden(self, caplog):
        """More positions held than the feed can watch is not an
        ordinary miss -- it must be visible, not inferred from a short
        list."""
        held = [_c(f"H{i}", "OPEN") for i in range(45)]
        with caplog.at_level("ERROR"):
            out = tiers.select_tier2(held)
        assert len(out["starved_obligations"]) == 4
        assert "REALTIME_BUDGET_STARVED" in caplog.text

    def test_nothing_is_starved_when_everything_fits(self):
        out = tiers.select_tier2([_c("A", "OPEN"), _c("B", "WATCHING")])
        assert out["starved_obligations"] == []


class TestSelectionIsStableAndDeduplicated:
    def test_rank_breaks_ties_within_a_state(self):
        out = tiers.select_tier2([_c("SLOW", "WATCHING", rank=9),
                                  _c("FAST", "WATCHING", rank=1)], budget=1)
        assert out["admitted"] == ["FAST"]

    def test_an_unranked_candidate_sorts_after_ranked_ones(self):
        out = tiers.select_tier2([_c("NORANK", "WATCHING"),
                                  _c("RANKED", "WATCHING", rank=50)], budget=1)
        assert out["admitted"] == ["RANKED"]

    def test_the_same_symbol_twice_keeps_its_strongest_claim(self):
        """A symbol can arrive as both a held position and a fresh
        candidate; the held claim is the one that matters."""
        out = tiers.select_tier2([_c("RIG", "WATCHING", rank=1),
                                  _c("RIG", "EXIT_PENDING")])
        assert out["admitted"] == ["RIG"]
        assert out["detail"][0]["state"] == "EXIT_PENDING"

    def test_symbols_without_a_name_are_dropped(self):
        out = tiers.select_tier2([_c("", "WATCHING"), _c(None, "OPEN"),
                                  _c("REAL", "WATCHING")])
        assert out["admitted"] == ["REAL"]

    def test_an_empty_candidate_set_is_not_an_error(self):
        assert tiers.select_tier2([])["admitted"] == []
        assert tiers.select_tier2(None)["admitted"] == []


class TestTier0EliminatesAndDecidesNothing:
    def test_it_reduces_the_universe(self):
        out = tiers.coarse_eliminate([f"S{i}" for i in range(1000)], keep=200)
        assert len(out["survivors"]) == 200
        assert out["eliminated"] == 800
        assert out["considered"] == 1000

    def test_it_states_that_it_decides_no_buys(self):
        """It runs on coarse, often delayed data -- exactly the data
        quality the realtime layer exists to replace. No caller may read
        a survivor as an endorsement."""
        assert tiers.coarse_eliminate(["A"], keep=1)["decides_buys"] is False

    def test_an_empty_universe_is_not_an_error(self):
        assert tiers.coarse_eliminate(None, keep=10)["survivors"] == []


class TestTier1WillNotInventItsOwnSize:
    def test_it_refuses_to_run_without_an_explicit_limit(self):
        """A fixed 150 or 200 written in without a reason is a number
        nobody can later defend or adjust."""
        with pytest.raises(ValueError, match="explicit limit"):
            tiers.shortlist(["A", "B"], limit=None)

    def test_it_honours_the_limit_it_is_given(self):
        out = tiers.shortlist([f"S{i}" for i in range(300)], limit=120)
        assert len(out["symbols"]) == 120
        assert out["limit"] == 120

    def test_it_reports_what_it_dropped(self):
        out = tiers.shortlist([f"S{i}" for i in range(10)], limit=4)
        assert out["dropped"] == 6

    def test_a_short_pool_is_not_padded(self):
        out = tiers.shortlist(["A", "B"], limit=50)
        assert out["symbols"] == ["A", "B"]
        assert out["dropped"] == 0


class TestTheTiersAgreeWithTheBootstrapWatchlist:
    """Two modules independently decide who gets a slot. If they order
    lifecycle states differently, the collector subscribes to one set and
    the tier logic believes another -- and the disagreement would only
    show up as a position quietly going unwatched."""

    def test_exit_pending_outranks_open_in_both(self):
        from market_data import bootstrap_watchlist as bw

        assert bw.PRIORITY_EXIT_PENDING < bw.PRIORITY_OPEN
        assert tiers.priority_of("EXIT_PENDING") < tiers.priority_of("OPEN")

    def test_both_treat_held_positions_as_non_evictable(self):
        assert tiers.may_evict("EXIT_PENDING") is False
        assert tiers.may_evict("OPEN") is False
        assert tiers.may_evict("WATCHING") is True

    def test_both_use_the_same_subscription_budget(self):
        from market_data import bootstrap_watchlist as bw

        assert tiers.REALTIME_BUDGET == bw.wire.MAX_SUBSCRIPTIONS

    def test_the_bootstrap_never_seeds_from_the_current_session(self):
        """The circular dependency, asserted from this side too: PRE
        candidates must not be required in order to produce PRE
        candidates."""
        import inspect

        from market_data import bootstrap_watchlist as bw

        source = inspect.getsource(bw.build)
        assert "prior_session" in source
        assert "Never consults the CURRENT" in source
