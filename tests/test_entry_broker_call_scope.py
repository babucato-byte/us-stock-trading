"""The one-minute watch runs on market data; KIS is touched only to
place an order.

§3 and §4 of the recovery directive, and the shape that made the
2026-08-27 starvation possible. Every KIS read is paced against a
shared, account-wide budget, so the number of them an entry tick issues
is not a performance question -- it is how much of S1's ability to
manage a real open position the entry consumes.

The strategy conditions a candidate must satisfy every minute -- ORB
range, VWAP, EMA structure, volume expansion, freshness, extension --
are all computed from the market-data layer. None of them needs the
broker. The broker is needed for exactly three things, all of them at
the moment of ordering: what the account can afford for THIS candidate,
whether an order for it is already open, and the submission itself.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

WATCH = (REPO_ROOT / "s6_live" / "precision_watch.py").read_text(encoding="utf-8")
FEATURES = (REPO_ROOT / "s6_live" / "realtime_features.py").read_text(encoding="utf-8")
CYCLE = (REPO_ROOT / "kis_live_trading.py").read_text(encoding="utf-8")


def _cycle_body():
    """The entry cycle from its candidate resolution to its per-symbol
    loop -- everything that runs whether or not there is a candidate."""
    start = CYCLE.index("allowed_symbols = source.allowed_symbols()")
    return CYCLE[start:CYCLE.index("for symbol in watchlist:", start)]


class TestTheWatchDoesNotUseTheBroker:
    def test_it_takes_no_broker_and_asks_for_no_price(self):
        code = "\n".join(l for l in WATCH.splitlines()
                         if not l.strip().startswith("#"))
        for forbidden in ("KISBroker", "get_current_price", "get_orderable_usd",
                          "get_positions", "get_open_orders"):
            assert forbidden not in code, forbidden

    def test_the_features_come_from_the_market_data_layer(self):
        assert "market_data_provider" in FEATURES

    def test_the_features_module_does_not_reach_for_the_broker(self):
        code = "\n".join(l for l in FEATURES.splitlines()
                         if not l.strip().startswith("#"))
        for forbidden in ("KISBroker", "get_orderable_usd", "get_positions"):
            assert forbidden not in code, forbidden


class TestNoCandidateMeansNoBrokerCall:
    def test_nothing_polls_the_account_before_the_per_symbol_loop(self):
        """A tick with no READY candidate must cost the shared budget
        nothing at all. Most ticks are this tick."""
        body = _cycle_body()
        for forbidden in ("broker.get_positions(", "broker.get_open_orders(",
                          "broker.get_orderable_usd(", "broker.get_current_price("):
            assert forbidden not in body, forbidden

    def test_the_account_reads_are_inside_the_loop(self):
        """Per candidate, not per tick -- orderable cash is answered by
        KIS for a specific symbol at a specific limit price, so there is
        no account-level figure to fetch once and reuse."""
        loop = CYCLE[CYCLE.index("for symbol in watchlist:"):]
        assert "broker.get_orderable_usd(" in loop
        assert "broker.get_open_orders(" in loop


class TestOrderingOfTheExpensiveSteps:
    def test_the_strategy_verdict_is_settled_before_the_broker_is_asked(self):
        """The watch decides READY from market data; only what survives
        it reaches the code that spends the KIS budget."""
        runner = (REPO_ROOT / "scripts" / "run_live_buy_entry.py").read_text(
            encoding="utf-8")
        assert "WatchedCandidateSource" in runner
        assert "candidate_source=source" in runner

    def test_sizing_precedes_submission(self):
        loop = CYCLE[CYCLE.index("for symbol in watchlist:"):]
        assert loop.index('"SIZING %s') < loop.index("execution_engine.submit_buy_order(")
