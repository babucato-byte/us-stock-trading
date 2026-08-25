"""Session capability and strategy mode are two conditions, not one.

Reading either one alone gives the wrong answer -- the session says
orders are possible here, the mode says whether this strategy may place
them, and a real order needs BOTH.

Getting it wrong in the permissive direction means a strategy that was
never promoted trades because its session happened to be open.

These tests INJECT the mode they are reasoning about rather than reading
the production table, because the two are genuinely independent and a
test that asserted today's table would have to be rewritten every time a
strategy is promoted -- which is exactly what happened when `orb` moved
to LIMITED_LIVE. The independence is the invariant; the current value is
not.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import s6_sessions as s6  # noqa: E402
from config import scanner_live_mode as slm  # noqa: E402
from s6_live import candidate_source as cs  # noqa: E402


def live_modes():
    modes = dict(slm.SCANNER_LIVE_MODE)
    modes[s6.SCANNER_NAME] = slm.MODE_LIMITED_LIVE
    return modes


def discovery_modes():
    """S6 stood down, whatever the production table currently says."""
    modes = dict(slm.SCANNER_LIVE_MODE)
    modes[s6.SCANNER_NAME] = slm.MODE_DISCOVERY_ONLY
    return modes


class TestTheTwoConditionsAreIndependent:
    def test_a_capable_session_and_a_stood_down_strategy_coexist(self):
        """The exact state that makes reading one alone dangerous: the
        session route is open while the strategy is not promoted."""
        assert s6.orders_allowed("REGULAR") is True
        assert slm.is_limited_live(s6.SCANNER_NAME, discovery_modes()) is False

    def test_a_capable_session_alone_offers_nothing(self):
        """Capability without promotion must yield no symbols at all --
        not a refusal downstream, but an empty source."""
        source = cs.S6CandidateSource(trading_day="2026-08-21",
                                      session="REGULAR",
                                      modes=discovery_modes())
        assert source.symbols() == []
        assert "not LIMITED_LIVE" in source.describe()["refusal"]

    def test_promotion_alone_does_not_open_a_shadow_session(self):
        """The mirror error: LIMITED_LIVE must not make PREMARKET
        tradeable."""
        source = cs.S6CandidateSource(trading_day="2026-08-21",
                                      session="PREMARKET",
                                      modes=live_modes())
        assert source.symbols() == []
        refusal = source.describe()["refusal"]
        assert "REALTIME_SHADOW" in refusal or "orders are enabled" in refusal

    @pytest.mark.parametrize("session", [
        "OVERNIGHT_DAYTIME", "PREMARKET", "AFTER_HOURS"])
    def test_every_shadow_session_refuses_even_when_promoted(self, session):
        source = cs.S6CandidateSource(trading_day="2026-08-21",
                                      session=session, modes=live_modes())
        assert source.symbols() == []

    def test_the_mode_check_runs_before_the_session_check(self):
        """So a stood-down strategy in a shadow session reports the
        reason an operator can act on first."""
        source = cs.S6CandidateSource(trading_day="2026-08-21",
                                      session="PREMARKET",
                                      modes=discovery_modes())
        assert "not LIMITED_LIVE" in source.describe()["refusal"]

    def test_both_together_are_required_and_sufficient(self):
        """With both satisfied the source stops refusing on either
        ground -- what remains is whether candidates exist."""
        source = cs.S6CandidateSource(trading_day="2026-08-21",
                                      session="REGULAR", modes=live_modes())
        refusal = source.describe()["refusal"] or ""
        assert "not LIMITED_LIVE" not in refusal
        assert "orders are enabled" not in refusal


class TestCapabilityIsNotPromotion:
    def test_the_session_matrix_says_capable_for_the_routed_sessions(self):
        """Capable, not promoted. Both sessions whose KIS order route the
        specification defines report LIMITED_LIVE as a statement about
        the SESSION; the two with no route report shadow and cannot be
        widened, because there is no endpoint to widen to."""
        assert s6.LIVE_SESSIONS == {"REGULAR", "OVERNIGHT_DAYTIME"}
        assert s6.mode_for("REGULAR") == s6.MODE_LIMITED_LIVE
        assert s6.mode_for("OVERNIGHT_DAYTIME") == s6.MODE_LIMITED_LIVE
        for unrouted in ("PREMARKET", "AFTER_HOURS"):
            assert s6.mode_for(unrouted) == s6.MODE_REALTIME_SHADOW
            assert s6.orders_allowed(unrouted) is False

    def test_mode_for_describes_the_SESSION_not_the_strategy(self):
        """`mode_for("REGULAR")` returning LIMITED_LIVE is a statement
        about the session's order route, not about whether S6 has been
        promoted. Conflating them is the whole hazard."""
        assert s6.mode_for("REGULAR") == "LIMITED_LIVE"
        # The session says LIMITED_LIVE regardless of the strategy mode:
        # hold the strategy down and the session route does not move.
        assert slm.is_limited_live(s6.SCANNER_NAME, discovery_modes()) is False
        assert s6.mode_for("REGULAR") == "LIMITED_LIVE"

    def test_the_scan_set_is_wider_than_the_order_set(self):
        assert s6.LIVE_SESSIONS < s6.SCAN_SESSIONS


class TestAStoodDownStrategyCannotSubmit:
    def test_the_risk_matrix_is_not_what_stops_it(self):
        """The limit is not what stops an unpromoted strategy -- the
        mode is. Worth asserting so a later reader does not mistake one
        for the other."""
        from config import position_limits as pl

        assert pl.check_entry("S6_ORB_BREAKOUT_V1", {}).allowed is True

    def test_the_source_yields_nothing_to_submit(self):
        source = cs.S6CandidateSource(trading_day="2026-08-21",
                                      session="REGULAR",
                                      modes=discovery_modes())
        assert source.allowed_symbols() == frozenset()
        assert source.qualify("ANY").qualified is False
