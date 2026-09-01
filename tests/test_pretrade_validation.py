"""Live eligibility belongs to the strategy, not to a watchlist.

On 2026-09-01 in PREMARKET, 30 of 32 S6 candidates sat at zero open data
gates. Not one had been judged on its merits: the realtime stream carries
at most 41 symbols, chosen before the session opened from the PRIOR
session's candidates, and `realtime_features.build` reads the stream alone
in the extended sessions. The two candidates that WERE subscribed
evaluated normally -- LLY blocked only on VOLUME_EXPANSION, SAIC only on
ORB_BREAKOUT_HOLDS.

The same universe in REGULAR, where the provider fallback already applied,
put 31 of 51 unsubscribed candidates at every gate open. So the stream was
deciding tradeability in three sessions out of four.

These tests pin the architecture: realtime is a delivery mechanism, the
strategy decides, and a candidate the tick could not afford to ask about
is WAITING_FOR_DATA rather than a rejection.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from s6_live import precision_watch, pretrade_validation as ptv  # noqa: E402


class _Inner:
    """A candidate source with candidates and nothing else."""

    _session = "PREMARKET"

    def __init__(self, rows):
        self._rows = {r["symbol"]: r for r in rows}

    def symbols(self):
        return list(self._rows)

    def candidate_row(self, symbol):
        return self._rows.get(symbol)

    def allowed_symbols(self):
        return None

    def describe(self):
        return {}


def _row(symbol, rank=None):
    return {"symbol": symbol, "rank": rank}


class TestSubscriptionCannotDecideTradeability:
    def test_a_candidate_outside_every_watchlist_still_reaches_validation(
            self, monkeypatch):
        """The proven failure: absent from the stream meant never asked."""
        asked = []

        def fake_evaluate(symbol, **kwargs):
            asked.append(symbol)
            return precision_watch.WatchEvaluation(
                symbol=symbol, session="PREMARKET", state="WATCHING")

        monkeypatch.setattr(precision_watch, "evaluate", fake_evaluate)
        source = precision_watch.WatchedCandidateSource(
            _Inner([_row("NOTSUBSCRIBED", 1)]), session="PREMARKET")
        source.symbols()

        assert asked == ["NOTSUBSCRIBED"]

    def test_the_entry_path_supplies_a_provider_for_every_session(self):
        """A provider is what steers `build` off its stream-only branch."""
        text = (REPO_ROOT / "scripts" / "run_live_buy_entry.py").read_text()
        assert "pretrade_validation" in text
        assert "provider=ptv.provider_for(" in text

    def test_the_extended_sessions_get_a_kis_provider_not_the_stream(self):
        from market_data.kis_bar_provider import KISBarMarketDataProvider

        class _Broker:
            pass

        for session in ("PREMARKET", "AFTER_HOURS", "OVERNIGHT_DAYTIME"):
            provider = ptv.provider_for(session, broker=_Broker(),
                                        fallback=object())
            assert isinstance(provider, KISBarMarketDataProvider), session

    def test_regular_keeps_the_fallback_it_already_had(self):
        fallback = object()
        assert ptv.provider_for("REGULAR", broker=object(),
                                fallback=fallback) is fallback


class TestTheStrategyStillDecides:
    def test_validation_order_comes_from_the_strategy_ranking(self):
        order = ptv.ordered(["C", "A", "B"],
                            rank_of={"A": 3, "B": 1, "C": 2}.get)
        assert order == ["B", "C", "A"]

    def test_an_unranked_candidate_does_not_jump_the_queue(self):
        order = ptv.ordered(["NORANK", "RANKED"],
                            rank_of={"RANKED": 5}.get)
        assert order == ["RANKED", "NORANK"]

    def test_validation_does_not_decide_readiness_itself(self):
        """It supplies evidence; `evaluate` returns the verdict."""
        source = (REPO_ROOT / "s6_live" / "pretrade_validation.py").read_text()
        for decision in ("READY", "submit_buy_order", "ORB", "VWAP >",
                         "EMA9 >"):
            assert decision not in source


class TestWaitingForDataIsNotARejection:
    def test_a_candidate_the_budget_never_reached_is_not_rejected(
            self, monkeypatch):
        def fake_evaluate(symbol, **kwargs):
            return precision_watch.WatchEvaluation(
                symbol=symbol, session="PREMARKET", state="WATCHING")

        monkeypatch.setattr(precision_watch, "evaluate", fake_evaluate)
        source = precision_watch.WatchedCandidateSource(
            _Inner([_row("A", 1), _row("B", 2)]),
            session="PREMARKET", budget_seconds=0.0)
        source.symbols()

        assert source.waiting_for_data == ["A", "B"]
        assert source.evaluations == {}
        assert source.validation_report["waiting_for_data"] == 2
        assert source.validation_report["validated"] == 0

    def test_a_generous_budget_evaluates_everything(self, monkeypatch):
        monkeypatch.setattr(
            precision_watch, "evaluate",
            lambda symbol, **k: precision_watch.WatchEvaluation(
                symbol=symbol, session="PREMARKET", state="WATCHING"))
        source = precision_watch.WatchedCandidateSource(
            _Inner([_row("A", 1), _row("B", 2)]),
            session="PREMARKET", budget_seconds=600.0)
        source.symbols()

        assert source.waiting_for_data == []
        assert source.validation_report["validated"] == 2

    def test_the_three_outcomes_are_distinguishable(self):
        assert len({ptv.STRATEGY_REJECTED, ptv.WAITING_FOR_DATA,
                    ptv.DATA_UNAVAILABLE}) == 3


class TestNoCandidateMeansNoSubscriptionRequirement:
    def test_an_empty_candidate_set_asks_the_broker_for_nothing(
            self, monkeypatch):
        calls = []
        monkeypatch.setattr(precision_watch, "evaluate",
                            lambda symbol, **k: calls.append(symbol))
        source = precision_watch.WatchedCandidateSource(
            _Inner([]), session="PREMARKET")

        assert source.symbols() == []
        assert calls == []
