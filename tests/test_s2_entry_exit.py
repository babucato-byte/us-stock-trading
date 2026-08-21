"""S2 entry confirmation and S2_EXIT_V0.

The shape of the exit is the thing under test. S2 enters because volume
arrived and the price confirmed it, so the exit is that statement
running backwards -- the volume drained AND the price stopped being
carried. Neither half alone is the signal, and the tests that matter
most are the ones proving decay by itself does not liquidate: volume
fading while price keeps working above VWAP is a normal winning shape,
and cutting it would systematically remove the trades that worked.

Two other properties:

Nothing reads PnL. A rule that behaved differently above and below water
would be two strategies sharing a name, and the one running in a
drawdown would be the untested one.

Missing data never becomes an action. An unreadable HMA is not a broken
thesis, and a clock problem is not a reason to close a healthy position.
"""

import ast
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import s2_exit_v0 as policy  # noqa: E402
from market_hours import EASTERN  # noqa: E402
from s2_live import entry_policy as entry  # noqa: E402
from s2_live import exit_policy as ex  # noqa: E402

BASE = 1_000_000
T0 = datetime(2026, 8, 19, 10, 0, tzinfo=EASTERN)


class Features:
    def __init__(self, price=None, hma200=None, hma200_slope=None,
                 vwap=None, volume=None):
        self.price, self.hma200, self.hma200_slope = price, hma200, hma200_slope
        self.vwap, self.volume = vwap, volume


def healthy(price=105.0, volume=6 * BASE):
    """Structure intact, above VWAP, volume where it started."""
    return Features(price=price, hma200=95.0, hma200_slope=0.4, vwap=100.0,
                    volume=volume)


def held(**kw):
    kw.setdefault("symbol", "ABC")
    kw.setdefault("entry_price", 100.0)
    kw.setdefault("entry_volume_multiple", 6.0)
    kw.setdefault("baseline_volume", BASE)
    return ex.S2PositionState(**kw)


# --------------------------------------------------------------------------
# The core: volume momentum extinction
# --------------------------------------------------------------------------

class TestDecayAloneDoesNotLiquidate:
    def test_volume_fading_while_price_climbs_is_held(self):
        """The winning shape. A move continuing on lighter participation
        is exactly what selling here would systematically destroy."""
        state = held(peak_volume_multiple=6.0, price_at_volume_peak=104.0)
        decision = ex.decide(state, features=healthy(price=108.0,
                                                     volume=2 * BASE), now=T0)
        assert decision.action == ex.HOLD
        assert ex.volume_has_decayed(state, healthy(volume=2 * BASE)) is True

    def test_it_holds_only_until_the_window_elapses(self):
        state = held(peak_volume_multiple=6.0, price_at_volume_peak=104.0,
                     decay_since=T0)
        early = ex.decide(state, features=healthy(price=108.0, volume=2 * BASE),
                          now=T0 + timedelta(minutes=10))
        assert early.action == ex.HOLD

        late = ex.decide(state, features=healthy(price=108.0, volume=2 * BASE),
                         now=T0 + timedelta(
                             minutes=policy.VOLUME_DECAY_CONFIRMATION_MINUTES))
        assert late.sells
        assert late.reason == ex.REASON_VOLUME_DECAY
        assert late.detail["weakness"] == ex.WEAKNESS_STALLED

    def test_recovered_volume_restarts_the_window(self):
        """The window times a condition; when the condition stops being
        true it has nothing left to time."""
        state = held(peak_volume_multiple=6.0, decay_since=T0)
        recovered = ex.observe(state, healthy(volume=6 * BASE),
                               now=T0 + timedelta(minutes=5))
        assert recovered.decay_since is None


class TestDecayPlusWeaknessIsTheExit:
    def test_decay_with_a_vwap_break_sells_as_the_compound_reason(self):
        state = held(peak_volume_multiple=6.0, price_at_volume_peak=104.0)
        decision = ex.decide(
            state, now=T0,
            features=Features(price=99.0, hma200=95.0, hma200_slope=0.4,
                              vwap=100.0, volume=2 * BASE))
        assert decision.reason == ex.REASON_VOLUME_DECAY_PRICE_WEAKNESS
        assert decision.detail["weakness"] == ex.WEAKNESS_VWAP

    def test_decay_with_the_move_given_back_sells(self):
        """Price below where it stood when volume peaked: the move the
        volume produced has been handed back."""
        state = held(peak_volume_multiple=6.0, price_at_volume_peak=108.0)
        decision = ex.decide(state, now=T0,
                             features=healthy(price=104.0, volume=2 * BASE))
        assert decision.reason == ex.REASON_VOLUME_DECAY_PRICE_WEAKNESS
        assert decision.detail["weakness"] == ex.WEAKNESS_MOMENTUM_REVERSAL

    def test_it_does_not_wait_for_the_window_when_price_is_weak(self):
        """Decay plus weakness is the signal; there is nothing to
        debounce once both halves are true."""
        state = held(peak_volume_multiple=6.0, price_at_volume_peak=108.0,
                     decay_since=None)
        decision = ex.decide(state, now=T0,
                             features=healthy(price=104.0, volume=2 * BASE))
        assert decision.sells

    def test_strong_volume_with_weak_price_is_not_the_compound_exit(self):
        """Weakness without decay is handled on its own terms."""
        state = held(peak_volume_multiple=6.0, price_at_volume_peak=104.0)
        decision = ex.decide(
            state, now=T0,
            features=Features(price=99.0, hma200=95.0, hma200_slope=0.4,
                              vwap=100.0, volume=6 * BASE))
        assert decision.reason == ex.REASON_VWAP_FAILURE


class TestDecayIsMeasuredFromThePeak:
    def test_momentum_that_built_and_fell_has_decayed(self):
        """8x down to 4x is decay even though 4x dwarfs the 1.5x that
        triggered the scan. Measuring from entry would call it
        untouched."""
        state = held(entry_volume_multiple=2.0, peak_volume_multiple=8.0)
        assert ex.volume_has_decayed(state, healthy(volume=4 * BASE)) is True

    def test_the_peak_ratchets_up_only(self):
        """A lower reading is decay, not a new peak. Letting the peak
        follow volume down would pin the ratio at 1.0 forever."""
        state = ex.observe(held(), healthy(volume=8 * BASE), now=T0)
        assert state.peak_volume_multiple == 8.0
        later = ex.observe(state, healthy(volume=3 * BASE), now=T0)
        assert later.peak_volume_multiple == 8.0

    def test_the_price_at_the_peak_is_captured_with_it(self):
        state = ex.observe(held(), healthy(price=112.0, volume=8 * BASE),
                           now=T0)
        assert state.price_at_volume_peak == 112.0

    def test_the_decay_ratio_is_reported_raw(self):
        """So a later study recomputes with a different fraction rather
        than inheriting this one."""
        state = held(peak_volume_multiple=6.0)
        ratio = ex.volume_decay_ratio(state, healthy(volume=int(3.5 * BASE)))
        assert ratio == pytest.approx(0.5, abs=0.01)

    def test_a_position_that_never_rose_cannot_decay(self):
        """Otherwise the quietest positions exit first, inverting the
        finding."""
        state = held(peak_volume_multiple=1.0)
        assert ex.volume_decay_ratio(state, healthy(volume=100)) is None
        assert ex.decide(state, features=healthy(volume=100),
                         now=T0).action == ex.HOLD


class TestTheExitNeverReadsPnL:
    def test_the_same_conditions_exit_at_a_profit_and_at_a_loss(self):
        """The volume case dying is the exit. Which side of entry that
        lands on is an outcome, not an input."""
        winner = held(entry_price=100.0, peak_volume_multiple=6.0,
                      price_at_volume_peak=120.0)
        loser = held(entry_price=100.0, peak_volume_multiple=6.0,
                     price_at_volume_peak=99.5)
        up = ex.decide(winner, now=T0,
                       features=healthy(price=115.0, volume=2 * BASE))
        down = ex.decide(loser, now=T0,
                         features=healthy(price=98.5, volume=2 * BASE))
        assert up.reason == down.reason == ex.REASON_VOLUME_DECAY_PRICE_WEAKNESS

    def test_no_condition_is_written_in_r_or_percent_from_entry(self):
        """Except the catastrophic cap, which is not the strategy.

        Checked against NAMES the code actually uses, not against the
        text: the module docstring exists to say that nothing here reads
        profit, and a substring sweep reports that sentence as a
        violation of itself.
        """
        source = (REPO_ROOT / "s2_live" / "exit_policy.py").read_text()
        forbidden = {"unrealised_r", "unrealized_r", "pnl", "profit",
                     "take_profit", "target_pct", "realized_pnl"}
        used = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Name):
                used.add(node.id.lower())
            elif isinstance(node, ast.Attribute):
                used.add(node.attr.lower())
            elif isinstance(node, ast.arg):
                used.add(node.arg.lower())
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                used.add(node.name.lower())
        assert used & forbidden == set(), used & forbidden


# --------------------------------------------------------------------------
# Priority
# --------------------------------------------------------------------------

class TestPriority:
    def test_emergency_outranks_everything(self):
        decision = ex.decide(held(), features=None, emergency=True)
        assert decision.sells and decision.reason == ex.REASON_EMERGENCY

    def test_the_hard_stop_outranks_the_structural_exits(self):
        """A ceiling that yields to anything is not a ceiling."""
        decision = ex.decide(held(entry_price=100.0), current_price=96.0,
                             features=healthy(price=96.0), now=T0)
        assert decision.reason == ex.REASON_HARD_STOP
        assert decision.detail["hard_stop"] == pytest.approx(97.0)

    def test_structure_failure_outranks_the_volume_exits(self):
        state = held(peak_volume_multiple=6.0, price_at_volume_peak=104.0)
        decision = ex.decide(
            state, now=T0,
            features=Features(price=99.0, hma200=99.5, hma200_slope=0.4,
                              vwap=100.0, volume=2 * BASE))
        assert decision.reason == ex.REASON_STRUCTURE_FAILURE

    def test_session_exit_is_last(self):
        """It is a validation constraint, not a judgement about the
        trade. Anything saying the position is actually wrong should be
        the reason instead."""
        state = held(peak_volume_multiple=6.0, price_at_volume_peak=104.0)
        near_close = datetime(2026, 8, 19, 15, 50, tzinfo=EASTERN)
        weak = ex.decide(state, session="REGULAR", now=near_close,
                         features=healthy(price=99.0, volume=2 * BASE))
        assert weak.reason == ex.REASON_VOLUME_DECAY_PRICE_WEAKNESS

    def test_a_healthy_position_still_exits_at_the_session_boundary(self):
        near_close = datetime(2026, 8, 19, 15, 50, tzinfo=EASTERN)
        decision = ex.decide(held(peak_volume_multiple=6.0),
                             session="REGULAR", now=near_close,
                             features=healthy())
        assert decision.reason == ex.REASON_SESSION_EXIT
        assert decision.detail["next_session"] == "AFTER_HOURS"

    def test_no_overnight_carry_during_validation(self):
        assert policy.ALLOW_OVERNIGHT_CARRY is False
        assert policy.EXIT_ON_SESSION_END is True

    def test_mid_session_is_not_a_session_exit(self):
        midday = datetime(2026, 8, 19, 12, 0, tzinfo=EASTERN)
        assert ex.session_ending("REGULAR", midday) is None


class TestTheHardStopIsProtectionNotStrategy:
    def test_the_cap_is_named_for_its_purpose(self):
        assert policy.S2_LIMITED_LIVE_MAX_LOSS_PCT == 3.0
        assert policy.MAX_LOSS_IS_MEASURED is False
        assert "CATASTROPHIC" in policy.MAX_LOSS_BASIS

    def test_s1s_level_is_not_reused(self):
        source = (REPO_ROOT / "s2_live" / "exit_policy.py").read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [str(getattr(node, "module", "") or "")]
                names += [a.name for a in node.names]
                assert not any("s1_" in n for n in names), names
        assert "6.0" not in (REPO_ROOT / "config" / "s2_exit_v0.py").read_text()

    def test_the_reevaluation_agenda_is_recorded_in_code(self):
        assert "-2.0%" in policy.REEVALUATION_CANDIDATES
        assert "ATR_VOLATILITY_BASED" in policy.REEVALUATION_CANDIDATES

    def test_the_more_conservative_stop_governs(self):
        """A structural level above the cap gets out sooner; one below it
        is overridden, because the cap is the agreed maximum."""
        assert ex.effective_stop_price(100.0, 98.0) == pytest.approx(98.0)
        assert ex.effective_stop_price(100.0, 90.0) == pytest.approx(97.0)
        assert ex.effective_stop_price(100.0, None) == pytest.approx(97.0)

    def test_an_unreadable_entry_price_expresses_no_stop(self):
        assert ex.effective_stop_price(None) is None
        assert ex.effective_stop_price(float("nan")) is None


# --------------------------------------------------------------------------
# Safety
# --------------------------------------------------------------------------

class TestMissingDataNeverLiquidates:
    def test_absent_features_hold(self):
        decision = ex.decide(held(), features=None, now=T0)
        assert decision.action == ex.HOLD
        assert decision.reason == ex.REASON_INSUFFICIENT_DATA

    def test_an_unreadable_hma_is_not_a_broken_thesis(self):
        decision = ex.decide(held(), now=T0, features=Features(
            price=105.0, hma200=None, hma200_slope=None, vwap=100.0,
            volume=6 * BASE))
        assert decision.action == ex.HOLD

    def test_an_unreadable_vwap_is_not_a_vwap_failure(self):
        decision = ex.decide(held(), now=T0, features=Features(
            price=105.0, hma200=95.0, hma200_slope=0.4, vwap=None,
            volume=6 * BASE))
        assert decision.action == ex.HOLD

    def test_an_unreadable_clock_does_not_close_the_position(self):
        assert ex.session_ending("REGULAR", None) is None
        assert ex.decide(held(peak_volume_multiple=6.0), session="REGULAR",
                         now=None, features=healthy()).action == ex.HOLD

    def test_an_unrecognised_session_does_not_close_the_position(self):
        assert ex.session_ending("DAILY", T0) is None

    def test_nan_is_not_a_measurement(self):
        decision = ex.decide(held(), now=T0, features=Features(
            price=float("nan"), hma200=95.0, hma200_slope=0.4, vwap=100.0,
            volume=6 * BASE))
        assert decision.action == ex.HOLD


class TestOnePositionCannotProduceTwoSells:
    def test_a_submitted_exit_is_not_decided_again(self):
        decision = ex.decide(held(exit_submitted=True), current_price=50.0,
                             features=healthy(price=50.0), now=T0)
        assert decision.action == ex.HOLD
        assert decision.reason == ex.REASON_ALREADY_SUBMITTED

    def test_every_condition_true_at_once_yields_one_action(self):
        state = held(peak_volume_multiple=6.0, price_at_volume_peak=110.0,
                     decay_since=T0)
        near_close = datetime(2026, 8, 19, 15, 50, tzinfo=EASTERN)
        decision = ex.decide(state, current_price=80.0, session="REGULAR",
                             now=near_close,
                             features=Features(price=80.0, hma200=95.0,
                                               hma200_slope=-1.0, vwap=100.0,
                                               volume=BASE))
        assert decision.action == ex.SELL
        assert decision.reason in ex.EXIT_REASONS

    def test_the_reason_vocabulary_is_the_agreed_one(self):
        for required in ("VOLUME_DECAY", "VOLUME_DECAY_PRICE_WEAKNESS",
                         "VWAP_FAILURE", "STRUCTURE_FAILURE", "HARD_STOP",
                         "SESSION_EXIT"):
            assert required in ex.EXIT_REASONS, required


class TestEveryDecisionCarriesTheVolumeStory:
    @pytest.mark.parametrize("field", [
        "entry_volume_multiple", "peak_volume_multiple",
        "current_volume_multiple", "volume_decay_ratio",
        "price_at_volume_peak", "current_price", "vwap"])
    def test_the_required_fields_are_on_a_hold_too(self, field):
        """A field written only at exit cannot say what the position
        looked like on the way there."""
        decision = ex.decide(held(peak_volume_multiple=6.0,
                                  price_at_volume_peak=104.0),
                             features=healthy(), now=T0)
        assert decision.action == ex.HOLD
        assert field in decision.detail

    def test_a_sell_carries_them_as_well(self):
        decision = ex.decide(held(peak_volume_multiple=6.0,
                                  price_at_volume_peak=110.0),
                             features=healthy(price=104.0, volume=2 * BASE),
                             now=T0)
        assert decision.sells
        assert decision.detail["volume_decay_ratio"] is not None
        assert decision.detail["peak_volume_multiple"] == 6.0


class TestExitsAreNeverGatedByEntryRisk:
    def test_it_imports_no_risk_guard(self):
        banned = {"allocator", "kill_switch_state", "daily_loss", "drawdown",
                  "position_limits", "order_gate"}
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


# --------------------------------------------------------------------------
# Entry
# --------------------------------------------------------------------------

class TestPositivePriceConfirmationLivesHere:
    def test_a_price_above_the_signal_confirms(self):
        verdict = entry.confirm(
            current_price=101.0, signal_price=100.0, session="REGULAR",
            features=Features(hma200=95.0, hma200_slope=0.4))
        assert verdict.allowed is True
        assert verdict.detail["gain_since_signal"] == pytest.approx(1.0)

    def test_an_unchanged_price_confirms_nothing(self):
        """It is the same observation the scanner already made, and
        accepting it would pass every candidate that did not move --
        which is most of what S2 selects for."""
        verdict = entry.confirm(
            current_price=100.0, signal_price=100.0, session="REGULAR",
            features=Features(hma200=95.0, hma200_slope=0.4))
        assert verdict.reason == entry.REASON_PRICE_NOT_CONFIRMED

    def test_there_is_no_percentage_to_choose(self):
        verdict = entry.confirm(
            current_price=100.01, signal_price=100.0, session="REGULAR",
            features=Features(hma200=95.0, hma200_slope=0.4))
        assert verdict.allowed is True

    def test_the_scanner_conditions_are_not_restated_as_constants(self):
        source = (REPO_ROOT / "s2_live" / "entry_policy.py").read_text()
        assert "1.5" not in source and "8.0" not in source


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
        assert verdict.allowed is False and verdict.reason == reason

    def test_an_unreadable_hma_blocks_rather_than_passing(self):
        verdict = entry.confirm(
            current_price=101.0, signal_price=100.0, session="REGULAR",
            features=Features(hma200=None, hma200_slope=0.4))
        assert verdict.reason == entry.REASON_HMA200_UNAVAILABLE

    @pytest.mark.parametrize("session", ["PREMARKET", "AFTER_HOURS", None,
                                         "DAILY", "OVERNIGHT"])
    def test_an_unverified_session_blocks(self, session):
        verdict = entry.confirm(
            current_price=101.0, signal_price=100.0, session=session,
            features=Features(hma200=95.0, hma200_slope=0.4))
        assert verdict.reason == entry.REASON_STALE_SESSION

    def test_only_regular_is_enabled_for_live_orders(self):
        """The rollout has reached REGULAR and nothing else."""
        assert entry.S2_LIVE_SESSIONS == {"REGULAR"}
        verdict = entry.confirm(
            current_price=101.0, signal_price=100.0, session="REGULAR",
            features=Features(hma200=95.0, hma200_slope=0.4))
        assert verdict.allowed is True

    def test_a_verified_route_is_not_by_itself_permission_to_trade(self):
        """OVERNIGHT_DAYTIME's route IS verified, and S2 still may not
        use it. Two separate facts, refused with two separate reasons, so
        an operator can tell "never verified" from "verified and not yet
        switched on"."""
        from scanners.base import scan_session

        assert scan_session.order_route_verified("OVERNIGHT_DAYTIME") is True
        verdict = entry.confirm(
            current_price=101.0, signal_price=100.0,
            session="OVERNIGHT_DAYTIME",
            features=Features(hma200=95.0, hma200_slope=0.4))
        assert verdict.allowed is False
        assert verdict.reason == entry.REASON_SESSION_NOT_ENABLED

    def test_an_unverified_route_is_refused_for_a_different_reason(self):
        verdict = entry.confirm(
            current_price=101.0, signal_price=100.0, session="PREMARKET",
            features=Features(hma200=95.0, hma200_slope=0.4))
        assert verdict.reason == entry.REASON_STALE_SESSION


# --------------------------------------------------------------------------
# Activation posture
# --------------------------------------------------------------------------

class TestTheApprovedRiskMatrix:
    S1 = "S1_HMA_EARLY_TREND_V1"
    S2 = "S2_VOLUME_ACCUMULATION_V1"

    def test_it_is_active(self):
        from config import position_limits as pl

        assert pl.ACTIVE is True
        assert pl.effective_limits() == (2, {self.S1: 1, self.S2: 1})

    def test_s1_1_s2_0_allows_s2(self):
        from config import position_limits as pl

        assert pl.check_entry(self.S2, {self.S1: 1}).allowed is True

    def test_s1_1_s2_1_is_the_full_book(self):
        from config import position_limits as pl

        assert pl.check_entry(self.S1, {self.S2: 1}).allowed is True
        assert pl.check_entry(self.S2, {self.S1: 1}).allowed is True

    def test_two_s1_is_blocked(self):
        from config import position_limits as pl

        assert pl.check_entry(self.S1, {self.S1: 1}).reason == pl.BLOCK_STRATEGY

    def test_two_s2_is_blocked(self):
        from config import position_limits as pl

        assert pl.check_entry(self.S2, {self.S2: 1}).reason == pl.BLOCK_STRATEGY

    def test_a_third_position_is_blocked_globally(self):
        from config import position_limits as pl

        decision = pl.check_entry("S3_FUTURE", {self.S1: 1, self.S2: 1})
        assert decision.allowed is False

    def test_the_existing_s1_position_is_unaffected(self):
        """TX is open and S1 keeps trading exactly as before."""
        from config import position_limits as pl

        assert pl.check_entry(self.S1, {}).allowed is True
