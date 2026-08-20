"""S2 entry confirmation and S2_EXIT_V0.

The property that shapes both: S2 has about four trading days behind it,
so neither module is allowed to invent a level. Every condition is either
S2's own measured entry condition re-checked, or a comparison with no
free parameter. The tests below check that as a fact about the code, not
just about today's behaviour -- a tuned constant added later should fail
something here.

The second property is that missing data never becomes an action. An
unreadable HMA is not a broken thesis, and a provider hiccup must not be
able to liquidate a position or open one.
"""

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import s2_exit_v0 as policy  # noqa: E402
from s2_live import entry_policy as entry  # noqa: E402
from s2_live import exit_policy as ex  # noqa: E402


class Features:
    def __init__(self, price=None, hma200=None, hma200_slope=None,
                 vwap=None, volume=None):
        self.price, self.hma200, self.hma200_slope = price, hma200, hma200_slope
        self.vwap, self.volume = vwap, volume


def held(**kw):
    kw.setdefault("symbol", "ABC")
    kw.setdefault("entry_price", 100.0)
    return ex.S2PositionState(**kw)


# --------------------------------------------------------------------------
# Entry
# --------------------------------------------------------------------------

class TestPositivePriceConfirmationLivesHere:
    def test_a_price_above_the_signal_confirms(self):
        verdict = entry.confirm(
            current_price=101.0, signal_price=100.0, session="REGULAR",
            features=Features(hma200=95.0, hma200_slope=0.4))
        assert verdict.allowed is True
        assert verdict.reason == entry.REASON_OK
        assert verdict.detail["gain_since_signal"] == pytest.approx(1.0)

    def test_an_unchanged_price_confirms_nothing(self):
        """It is the same observation the scanner already made. Treating
        it as confirmation would pass every candidate that did not move,
        which is most of them -- S2 selects for quiet."""
        verdict = entry.confirm(
            current_price=100.0, signal_price=100.0, session="REGULAR",
            features=Features(hma200=95.0, hma200_slope=0.4))
        assert verdict.allowed is False
        assert verdict.reason == entry.REASON_PRICE_NOT_CONFIRMED

    def test_a_price_below_the_signal_is_refused(self):
        verdict = entry.confirm(
            current_price=98.0, signal_price=100.0, session="REGULAR",
            features=Features(hma200=95.0, hma200_slope=0.4))
        assert verdict.allowed is False
        assert verdict.reason == entry.REASON_PRICE_NOT_CONFIRMED

    def test_there_is_no_percentage_to_choose(self):
        """The confirmation is a sign comparison. One cent above the
        signal price confirms, because the alternative is picking a
        threshold S2 has no data for."""
        verdict = entry.confirm(
            current_price=100.01, signal_price=100.0, session="REGULAR",
            features=Features(hma200=95.0, hma200_slope=0.4))
        assert verdict.allowed is True


class TestEntryRecheckesS2sOwnConditions:
    def test_a_price_that_fell_below_hma200_is_refused(self):
        verdict = entry.confirm(
            current_price=94.0, signal_price=90.0, session="REGULAR",
            features=Features(hma200=95.0, hma200_slope=0.4))
        assert verdict.reason == entry.REASON_BELOW_HMA200

    def test_a_flattened_hma200_is_refused(self):
        verdict = entry.confirm(
            current_price=101.0, signal_price=100.0, session="REGULAR",
            features=Features(hma200=95.0, hma200_slope=0.0))
        assert verdict.reason == entry.REASON_HMA200_NOT_RISING

    def test_the_conditions_are_not_restated_as_new_constants(self):
        """They live in scanners/accumulation/config.json. A second copy
        here could drift from the one the scanner actually applied."""
        source = (REPO_ROOT / "s2_live" / "entry_policy.py").read_text()
        assert "1.5" not in source
        assert "8.0" not in source


class TestEntryFailsClosed:
    @pytest.mark.parametrize("kwargs,reason", [
        ({"current_price": None, "signal_price": 100.0}, entry.REASON_NO_PRICE),
        ({"current_price": 101.0, "signal_price": None},
         entry.REASON_NO_SIGNAL_PRICE),
        ({"current_price": float("nan"), "signal_price": 100.0},
         entry.REASON_NO_PRICE),
    ])
    def test_an_unreadable_price_blocks(self, kwargs, reason):
        verdict = entry.confirm(session="REGULAR", features=Features(
            hma200=95.0, hma200_slope=0.4), **kwargs)
        assert verdict.allowed is False
        assert verdict.reason == reason

    def test_absent_features_block(self):
        verdict = entry.confirm(current_price=101.0, signal_price=100.0,
                                session="REGULAR", features=None)
        assert verdict.reason == entry.REASON_NO_FEATURES

    def test_an_unreadable_hma_blocks_rather_than_passing(self):
        """Not known to hold is not the same as holding."""
        verdict = entry.confirm(
            current_price=101.0, signal_price=100.0, session="REGULAR",
            features=Features(hma200=None, hma200_slope=0.4))
        assert verdict.reason == entry.REASON_HMA200_UNAVAILABLE

    @pytest.mark.parametrize("session", ["PREMARKET", "AFTER_HOURS", None,
                                         "DAILY", "OVERNIGHT"])
    def test_an_unverified_session_blocks(self, session):
        """A reservation is an instruction to trade later, not a fill."""
        verdict = entry.confirm(
            current_price=101.0, signal_price=100.0, session=session,
            features=Features(hma200=95.0, hma200_slope=0.4))
        assert verdict.allowed is False
        assert verdict.reason == entry.REASON_STALE_SESSION

    @pytest.mark.parametrize("session", ["REGULAR", "OVERNIGHT_DAYTIME"])
    def test_a_verified_session_is_allowed_through(self, session):
        verdict = entry.confirm(
            current_price=101.0, signal_price=100.0, session=session,
            features=Features(hma200=95.0, hma200_slope=0.4))
        assert verdict.allowed is True

    def test_the_refusal_names_the_first_problem(self):
        """A verdict reporting the last failure would send an operator to
        fix something that was merely also true."""
        verdict = entry.confirm(
            current_price=None, signal_price=None, session="PREMARKET",
            features=None)
        assert verdict.reason == entry.REASON_NO_PRICE


# --------------------------------------------------------------------------
# Exit
# --------------------------------------------------------------------------

class TestExitIsStructuralNotTuned:
    def test_there_is_no_price_stop_and_that_is_recorded(self):
        """Absence with a reason, not an oversight -- and a live blocker
        rather than something to be noticed later."""
        assert policy.HARD_STOP_PCT is None
        assert policy.REQUIRES_STOP_BEFORE_LIVE is True
        assert "S2_STOP_NOT_ESTABLISHED" in policy.NO_STOP_REASON

    def test_it_does_not_borrow_s1s_levels(self):
        """S1's −6% was measured on a trend strategy. Reusing it here
        would look justified because it appears elsewhere in the repo."""
        source = (REPO_ROOT / "config" / "s2_exit_v0.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert "s1_exit" not in str(node.module or "")
        exit_source = (REPO_ROOT / "s2_live" / "exit_policy.py").read_text()
        for node in ast.walk(ast.parse(exit_source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [str(getattr(node, "module", "") or "")]
                names += [a.name for a in node.names]
                assert not any("s1_" in n for n in names), names

    def test_the_only_numeric_constant_is_a_definition_not_a_level(self):
        """0.5 says "the excess has halved". It is not a tuned threshold,
        and it travels on every measured row so a study can recompute."""
        assert policy.VOLUME_DECAY_FRACTION == 0.5
        decision = ex.decide(
            held(signal_volume_multiple=6.0, baseline_volume=1_000_000),
            features=Features(price=101.0, hma200=95.0, hma200_slope=0.4,
                              vwap=100.0, volume=3_400_000))
        assert decision.sells
        assert decision.detail["decay_fraction"] == 0.5

    def test_a_hold_carries_the_missing_stop_forward(self):
        """So a reader of the decision log does not have to find the
        config to learn the position has no price stop."""
        decision = ex.decide(
            held(), features=Features(price=101.0, hma200=95.0,
                                      hma200_slope=0.4, vwap=100.0))
        assert decision.action == ex.HOLD
        assert "S2_STOP_NOT_ESTABLISHED" in decision.detail["stop_status"]


class TestExitPriority:
    def test_emergency_outranks_everything(self):
        decision = ex.decide(held(), features=None, emergency=True)
        assert decision.sells and decision.reason == ex.REASON_EMERGENCY

    def test_thesis_invalidation_outranks_vwap(self):
        """A symbol can lose VWAP intraday and recover; one that dropped
        through its 200-period HMA is no longer the setup that was
        found. The other order would exit on noise and hold through the
        real breakdown."""
        decision = ex.decide(held(), features=Features(
            price=90.0, hma200=95.0, hma200_slope=0.4, vwap=100.0))
        assert decision.reason == ex.REASON_BELOW_HMA200

    def test_vwap_loss_sells_when_the_thesis_still_holds(self):
        decision = ex.decide(held(), features=Features(
            price=99.0, hma200=95.0, hma200_slope=0.4, vwap=100.0))
        assert decision.reason == ex.REASON_VWAP_LOSS

    def test_a_price_exactly_at_vwap_has_not_lost_it(self):
        decision = ex.decide(held(), features=Features(
            price=100.0, hma200=95.0, hma200_slope=0.4, vwap=100.0))
        assert decision.action == ex.HOLD

    def test_a_flat_hma_invalidates_the_thesis(self):
        decision = ex.decide(held(), features=Features(
            price=101.0, hma200=95.0, hma200_slope=0.0, vwap=100.0))
        assert decision.reason == ex.REASON_HMA200_NOT_RISING


class TestVolumeDecayIsRelative:
    def test_a_loud_and_a_quiet_candidate_decay_on_the_same_scale(self):
        loud = ex.decide(held(signal_volume_multiple=6.0,
                              baseline_volume=1_000_000),
                         features=Features(price=101.0, hma200=95.0,
                                           hma200_slope=0.4, vwap=100.0,
                                           volume=3_400_000))
        quiet = ex.decide(held(signal_volume_multiple=1.6,
                               baseline_volume=1_000_000),
                          features=Features(price=101.0, hma200=95.0,
                                            hma200_slope=0.4, vwap=100.0,
                                            volume=1_250_000))
        assert loud.reason == ex.REASON_VOLUME_DECAY
        assert quiet.reason == ex.REASON_VOLUME_DECAY

    def test_volume_still_elevated_holds(self):
        decision = ex.decide(held(signal_volume_multiple=6.0,
                                  baseline_volume=1_000_000),
                             features=Features(price=101.0, hma200=95.0,
                                               hma200_slope=0.4, vwap=100.0,
                                               volume=6_000_000))
        assert decision.action == ex.HOLD

    def test_a_candidate_that_was_never_elevated_cannot_decay(self):
        """Otherwise the quietest positions exit first, which inverts the
        finding."""
        decision = ex.decide(held(signal_volume_multiple=1.0,
                                  baseline_volume=1_000_000),
                             features=Features(price=101.0, hma200=95.0,
                                               hma200_slope=0.4, vwap=100.0,
                                               volume=100))
        assert decision.action == ex.HOLD

    def test_a_missing_baseline_is_not_a_decay(self):
        decision = ex.decide(held(signal_volume_multiple=6.0,
                                  baseline_volume=None),
                             features=Features(price=101.0, hma200=95.0,
                                               hma200_slope=0.4, vwap=100.0,
                                               volume=1))
        assert decision.action == ex.HOLD


class TestMissingDataNeverLiquidates:
    def test_absent_features_hold(self):
        decision = ex.decide(held(), features=None)
        assert decision.action == ex.HOLD
        assert decision.reason == ex.REASON_INSUFFICIENT_DATA

    def test_an_unreadable_hma_is_not_a_broken_thesis(self):
        """A provider hiccup must not become a liquidation."""
        decision = ex.decide(held(), features=Features(
            price=101.0, hma200=None, hma200_slope=None, vwap=100.0))
        assert decision.action == ex.HOLD

    def test_an_unreadable_vwap_is_not_a_vwap_loss(self):
        decision = ex.decide(held(), features=Features(
            price=101.0, hma200=95.0, hma200_slope=0.4, vwap=None))
        assert decision.action == ex.HOLD

    def test_nan_is_not_a_measurement(self):
        decision = ex.decide(held(), features=Features(
            price=float("nan"), hma200=95.0, hma200_slope=0.4, vwap=100.0))
        assert decision.action == ex.HOLD


class TestOnePositionCannotProduceTwoSells:
    def test_a_submitted_exit_is_not_decided_again(self):
        decision = ex.decide(held(exit_submitted=True), features=Features(
            price=90.0, hma200=95.0, hma200_slope=-1.0, vwap=100.0))
        assert decision.action == ex.HOLD
        assert decision.reason == "S2_EXIT_ALREADY_SUBMITTED"

    def test_several_conditions_true_at_once_yield_one_action(self):
        """Below HMA200, below VWAP, and volume drained -- one SELL."""
        decision = ex.decide(
            held(signal_volume_multiple=6.0, baseline_volume=1_000_000),
            features=Features(price=80.0, hma200=95.0, hma200_slope=-1.0,
                              vwap=100.0, volume=1_000_000))
        assert decision.action == ex.SELL
        assert isinstance(decision.reason, str)


class TestExitsAreNeverGatedByEntryRisk:
    def test_it_imports_no_risk_guard(self):
        """A drawdown limit that also blocked liquidation would trap the
        account in the position the limit exists to escape."""
        banned = {"allocator", "kill_switch_state", "daily_loss",
                  "drawdown", "position_limits", "order_gate"}
        source = (REPO_ROOT / "s2_live" / "exit_policy.py").read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [str(getattr(node, "module", "") or "")]
                names += [a.name for a in node.names]
                for name in names:
                    for segment in name.split("."):
                        assert segment not in banned, f"imports {name}"

    def test_neither_module_places_an_order(self):
        banned = {"brokers", "kis_broker", "execution_engine",
                  "kis_live_trading", "exit_runtime"}
        for filename in ("exit_policy.py", "entry_policy.py"):
            source = (REPO_ROOT / "s2_live" / filename).read_text()
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [str(getattr(node, "module", "") or "")]
                    names += [a.name for a in node.names]
                    for name in names:
                        for segment in name.split("."):
                            assert segment not in banned, f"{filename}: {name}"


class TestS2IsStillNotLive:
    def test_s2_remains_discovery_only(self):
        from config import scanner_live_mode

        assert scanner_live_mode.SCANNER_LIVE_MODE["accumulation"] == \
            "DISCOVERY_ONLY"

    def test_only_s1_is_limited_live(self):
        from config import scanner_live_mode

        live = [name for name, mode in scanner_live_mode.SCANNER_LIVE_MODE.items()
                if mode == "LIMITED_LIVE"]
        assert live == ["hma_early_trend"]

    def test_the_risk_matrix_still_refuses_an_s2_position(self):
        from config import position_limits as pl

        assert pl.check_entry("S2_VOLUME_ACCUMULATION_V1", {}).allowed is False
