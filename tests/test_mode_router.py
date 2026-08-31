"""One door, and which side an intent goes through is a config value.

"Is this strategy live?" answered in six places is six chances to answer
it differently, and the dangerous answer is the one that says yes by
accident.

The property that matters most here is that promotion is a MODE change.
A strategy running in PAPER emits exactly the intents it would emit
LIVE; flipping its table row changes which engine receives them and
nothing else. If promotion needed a strategy rewrite, everything
measured in PAPER would describe code that no longer exists.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import scanner_live_mode as slm  # noqa: E402
from execution import mode_router as mr  # noqa: E402

INTENT = {"symbol": "NVDA", "side": "buy", "quantity": 2, "price": 100.0}


def _engines():
    seen = {"live": [], "paper": []}
    return seen, (lambda i: seen["live"].append(i) or "LIVE_RESULT"), \
        (lambda i: seen["paper"].append(i) or "PAPER_RESULT")


class TestTheProductionRouting:
    def test_s6_is_live(self):
        assert mr.mode_for("orb")["mode"] == mr.MODE_LIVE

    @pytest.mark.parametrize("scanner", [
        "hma_early_trend", "accumulation", "breakout_ready",
        "premarket_momentum", "gap_pullback"])
    def test_every_other_scanner_is_paper(self, scanner):
        decision = mr.mode_for(scanner)
        assert decision["mode"] == mr.MODE_PAPER
        assert decision["reason"] == mr.REASON_NOT_LIVE

    def test_exactly_one_scanner_is_live(self):
        live = [n for n in slm.SCANNER_LIVE_MODE if mr.is_live(n)]
        assert live == ["orb"]


class TestIntentsReachTheRightEngine:
    def test_a_live_strategy_reaches_the_common_execution_engine(self):
        seen, live, paper = _engines()
        out = mr.route(INTENT, scanner_name="orb", live_execute=live,
                       paper_execute=paper)
        assert seen["live"] == [INTENT]
        assert seen["paper"] == []
        assert out["result"] == "LIVE_RESULT"

    @pytest.mark.parametrize("scanner", [
        "hma_early_trend", "accumulation", "breakout_ready",
        "premarket_momentum", "gap_pullback"])
    def test_a_paper_strategy_never_reaches_the_live_engine(self, scanner):
        seen, live, paper = _engines()
        mr.route(INTENT, scanner_name=scanner, live_execute=live,
                 paper_execute=paper)
        assert seen["live"] == [], f"{scanner} reached the LIVE engine"
        assert seen["paper"] == [INTENT]

    def test_an_unknown_scanner_goes_to_paper(self):
        seen, live, paper = _engines()
        mr.route(INTENT, scanner_name="not_a_scanner", live_execute=live,
                 paper_execute=paper)
        assert seen["live"] == []


class TestPromotionIsAModeChangeOnly:
    """The whole reason PAPER results are worth collecting."""

    def test_the_same_intent_routes_live_when_the_row_flips(self):
        seen, live, paper = _engines()
        promoted = {**slm.SCANNER_LIVE_MODE,
                    "hma_early_trend": slm.MODE_LIMITED_LIVE}
        mr.route(INTENT, scanner_name="hma_early_trend", live_execute=live,
                 paper_execute=paper, modes=promoted)
        assert seen["live"] == [INTENT], (
            "promotion must change only which engine receives the intent")

    def test_the_intent_is_passed_through_unchanged(self):
        """Not transformed, wrapped or re-derived on the way."""
        seen, live, paper = _engines()
        mr.route(INTENT, scanner_name="orb", live_execute=live,
                 paper_execute=paper)
        assert seen["live"][0] is INTENT

    def test_the_router_holds_no_strategy_logic(self):
        source = (REPO_ROOT / "execution" / "mode_router.py").read_text()
        for forbidden in ("volume_expansion", "orb_minutes", "threshold",
                          "stop_price", "take_profit", "score"):
            assert forbidden not in source, forbidden

    def test_the_router_cannot_itself_reach_a_broker(self):
        """It decides WHICH engine, never HOW to execute."""
        source = (REPO_ROOT / "execution" / "mode_router.py").read_text()
        for forbidden in ("from brokers", "KISBroker", "submit_order",
                          "execution_engine", "virtual_execution"):
            assert forbidden not in source, forbidden


class TestItFailsClosed:
    def test_an_unreadable_table_routes_to_paper(self, monkeypatch):
        """Guessing LIVE would place a real order for a strategy nobody
        promoted; guessing PAPER shows up in the funnel."""
        monkeypatch.setattr(
            slm, "is_limited_live",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unreadable")))
        decision = mr.mode_for("orb")
        assert decision["mode"] == mr.MODE_PAPER
        assert decision["reason"] == mr.REASON_MODE_UNREADABLE

    def test_an_unreadable_table_still_routes_the_intent(self, monkeypatch):
        monkeypatch.setattr(
            slm, "is_limited_live",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unreadable")))
        seen, live, paper = _engines()
        mr.route(INTENT, scanner_name="orb", live_execute=live,
                 paper_execute=paper)
        assert seen["live"] == []
        assert seen["paper"] == [INTENT]
