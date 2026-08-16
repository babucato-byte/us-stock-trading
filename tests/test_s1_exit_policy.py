"""S1_EXIT_V0: four axes, one decision, and exits that cannot be blocked."""

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import s1_exit_v0 as policy  # noqa: E402
from s1_live import exit_policy as ep  # noqa: E402

ENTRY = 100.0
R = policy.R_PCT  # 0.06


def state(**kw):
    base = dict(symbol="NVDA", entry_price=ENTRY)
    base.update(kw)
    return ep.S1PositionState(**base)


class Features:
    def __init__(self, price=110.0, hma200=100.0, hma89=105.0, hma200_slope=1.0):
        self.price, self.hma200 = price, hma200
        self.hma89, self.hma200_slope = hma89, hma200_slope


HEALTHY = Features()


class TestHardStop:
    def test_the_level_is_minus_six_percent(self):
        assert policy.HARD_STOP_PCT == -0.06

    def test_it_is_not_the_scalping_stop(self):
        """§4: -8% must not be copied without examination."""
        import risk_config

        assert risk_config.STOP_LOSS_RATE == -0.08
        assert policy.HARD_STOP_PCT != risk_config.STOP_LOSS_RATE

    def test_one_stop_out_costs_about_one_daily_loss_budget(self):
        """The derivation the level actually comes from."""
        import risk_config
        from config import s1_allocation

        cost = s1_allocation.MAX_SINGLE_POSITION_PCT * abs(policy.HARD_STOP_PCT)
        assert cost == pytest.approx(0.021, abs=0.001)
        assert cost <= abs(risk_config.MAX_DAILY_LOSS_RATE) * 1.10

    def test_price_at_the_stop_sells(self):
        out = ep.decide(state(), current_price=94.0, features=HEALTHY)
        assert out.sells and out.reason == ep.REASON_HARD_STOP

    def test_price_just_above_the_stop_holds(self):
        out = ep.decide(state(), current_price=94.5, features=HEALTHY)
        assert out.action == ep.HOLD

    def test_it_is_not_data_derived_and_says_so(self):
        assert policy.DATA_DERIVED is False


class TestProfitProtection:
    def test_one_R_earns_a_breakeven_floor(self):
        out = ep.decide(state(), current_price=ENTRY * (1 + 1.0 * R),
                        features=HEALTHY)
        assert out.action == ep.RATCHET
        assert out.new_protective_floor_r == 0.0

    def test_two_R_earns_a_one_R_floor(self):
        out = ep.decide(state(), current_price=ENTRY * (1 + 2.0 * R),
                        features=HEALTHY)
        assert out.action == ep.RATCHET
        assert out.new_protective_floor_r == 1.0

    def test_the_floor_never_falls(self):
        assert ep.protective_floor_for(2.5) == 1.0
        assert ep.protective_floor_for(1.2) == 0.0
        assert ep.protective_floor_for(0.9) is None

    def test_a_protected_trade_cannot_become_a_full_loss(self):
        """The specific failure this axis prevents."""
        held = state(protective_floor_r=0.0, peak_r=1.2)
        out = ep.decide(held, current_price=ENTRY, features=HEALTHY)
        assert out.sells and out.reason == ep.REASON_PROTECTIVE_STOP
        assert out.effective_stop_price == pytest.approx(ENTRY)

    def test_the_protective_stop_outranks_the_hard_stop(self):
        held = state(protective_floor_r=1.0, peak_r=2.1)
        assert ep.effective_stop_price(ENTRY, 1.0) == pytest.approx(ENTRY * (1 + R))
        out = ep.decide(held, current_price=ENTRY * (1 + R) - 0.01, features=HEALTHY)
        assert out.sells and out.reason == ep.REASON_PROTECTIVE_STOP

    def test_a_ratchet_is_not_an_exit(self):
        out = ep.decide(state(), current_price=ENTRY * (1 + R), features=HEALTHY)
        assert out.sells is False

    def test_the_steps_are_marked_as_convention_not_measurement(self):
        assert policy.PROFIT_PROTECTION_STEPS == ((1.0, 0.0), (2.0, 1.0))
        assert policy.DATA_DERIVED is False


class TestTrendExit:
    def test_a_healthy_trend_holds(self):
        """Below +1R, so no ratchet is due either."""
        assert ep.decide(state(), current_price=103.0,
                         features=Features(price=103.0)).action == ep.HOLD

    def test_price_closing_below_hma200_sells(self):
        broken = Features(price=99.0, hma200=100.0, hma89=105.0, hma200_slope=1.0)
        out = ep.decide(state(), current_price=99.0, features=broken)
        assert out.sells and out.reason == ep.REASON_TREND_BREAKDOWN

    def test_hma89_crossing_back_below_hma200_sells(self):
        broken = Features(price=110.0, hma200=100.0, hma89=99.0, hma200_slope=1.0)
        out = ep.decide(state(), current_price=110.0, features=broken)
        assert out.sells and "HMA89" in out.detail

    def test_the_long_trend_rolling_over_sells(self):
        broken = Features(price=110.0, hma200=100.0, hma89=105.0, hma200_slope=-0.5)
        out = ep.decide(state(), current_price=110.0, features=broken)
        assert out.sells and "slope" in out.detail

    def test_adx_is_deliberately_not_an_exit_condition(self):
        """§6: a normal consolidation must not be sold."""
        quiet = Features(price=110.0, hma200=100.0, hma89=105.0, hma200_slope=0.2)
        quiet.adx, quiet.adx_rising = 12.0, False   # would FAIL entry
        quiet.price = 103.0
        assert ep.decide(state(), current_price=103.0, features=quiet).action == ep.HOLD
        assert "adx" not in str(policy.as_dict()["trend_structural_conditions"]).lower()

    def test_entry_and_exit_are_not_symmetric(self):
        excluded = policy.as_dict()["trend_momentum_conditions_deliberately_excluded"]
        assert set(excluded) == {"adx_min", "adx_rising"}

    def test_unjudgeable_features_do_not_sell(self):
        """An unknown is not a broken trend."""
        assert ep.trend_broken(Features(hma200=None)) is None
        assert ep.decide(state(), current_price=103.0,
                         features=Features(hma200=None)).action == ep.HOLD

    def test_no_features_means_no_trend_judgement(self):
        assert ep.decide(state(), current_price=103.0, features=None).action == ep.HOLD

    def test_a_ratchet_is_due_above_one_R_even_in_a_healthy_trend(self):
        """+10% is +1.67R, which has earned a breakeven floor."""
        out = ep.decide(state(), current_price=110.0, features=HEALTHY)
        assert out.action == ep.RATCHET and out.new_protective_floor_r == 0.0


class TestTimeExit:
    def test_ten_sessions_without_progress_releases_capital(self):
        out = ep.decide(state(sessions_held=10, peak_r=0.3),
                        current_price=101.0, features=HEALTHY)
        assert out.sells and out.reason == ep.REASON_TIME_EXIT

    def test_a_working_trade_is_exempt(self):
        """Reaching +1R hands the position to the other axes."""
        out = ep.decide(state(sessions_held=30, peak_r=1.5, protective_floor_r=0.0),
                        current_price=ENTRY * (1 + 1.4 * R), features=HEALTHY)
        assert out.action != ep.SELL

    def test_nine_sessions_is_not_yet(self):
        assert ep.decide(state(sessions_held=9, peak_r=0.1),
                         current_price=101.0, features=HEALTHY).action == ep.HOLD

    def test_the_scalping_sixty_minute_stop_is_not_used(self):
        from config import scalping_strategy_v1_config as scalping

        assert scalping.MAX_POSITION_HOLD_MINUTES == 60
        assert policy.TIME_EXIT_SESSIONS == 10
        source = (REPO_ROOT / "config" / "s1_exit_v0.py").read_text()
        assert "MAX_POSITION_HOLD_MINUTES" not in source.split('"""')[2]


class TestPriorityAndIdempotency:
    def test_only_one_action_is_ever_returned(self):
        """Several conditions true at once still yields one decision."""
        everything = Features(price=90.0, hma200=100.0, hma89=95.0, hma200_slope=-1.0)
        out = ep.decide(state(sessions_held=30, peak_r=0.0),
                        current_price=90.0, features=everything)
        assert isinstance(out, ep.ExitDecision)
        assert out.sells and out.reason == ep.REASON_HARD_STOP, "stop outranks the rest"

    def test_emergency_outranks_everything(self):
        out = ep.decide(state(), current_price=200.0, features=HEALTHY, emergency=True)
        assert out.sells and out.reason == ep.REASON_EMERGENCY

    def test_trend_breakdown_outranks_the_time_exit(self):
        broken = Features(price=99.0, hma200=100.0, hma89=105.0, hma200_slope=1.0)
        out = ep.decide(state(sessions_held=30, peak_r=0.0),
                        current_price=99.0, features=broken)
        assert out.reason == ep.REASON_TREND_BREAKDOWN

    def test_an_exit_already_in_flight_is_never_re_sold(self):
        out = ep.decide(state(exit_submitted=True), current_price=50.0,
                        features=Features(price=50.0, hma200=100.0))
        assert out.action == ep.HOLD
        assert "already in flight" in out.detail

    def test_the_same_input_gives_the_same_decision(self):
        args = dict(current_price=94.0, features=HEALTHY)
        assert ep.decide(state(), **args).as_dict() == ep.decide(state(), **args).as_dict()


class TestRestartRecovery:
    def test_the_floor_survives_because_it_is_persisted(self):
        """Recomputing it from the current price would hand it back."""
        after_restart = state(protective_floor_r=1.0, peak_r=2.2)
        out = ep.decide(after_restart, current_price=ENTRY * (1 + 0.5 * R),
                        features=HEALTHY)
        assert out.sells and out.reason == ep.REASON_PROTECTIVE_STOP

    def test_a_lost_peak_does_not_lower_a_stored_floor(self):
        out = ep.decide(state(protective_floor_r=1.0, peak_r=0.0),
                        current_price=ENTRY * (1 + 1.5 * R), features=HEALTHY)
        assert out.effective_stop_price == pytest.approx(ENTRY * (1 + R))

    def test_the_persisted_shape_round_trips(self):
        original = state(sessions_held=4, protective_floor_r=0.0, peak_r=1.3)
        restored = ep.S1PositionState(**original.as_dict())
        assert restored.as_dict() == original.as_dict()


class TestExitIsNeverBlocked:
    """§9: entry can be blocked; exit must remain available."""

    def test_the_policy_does_not_import_any_entry_gate(self):
        forbidden = {"risk_guards", "allocator", "kill_switch", "cash_pool",
                     "risk_state", "readiness", "candidate_source"}
        source = (REPO_ROOT / "s1_live" / "exit_policy.py").read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    assert alias.name not in forbidden, alias.name
                assert node.module.split(".")[-1] not in forbidden, node.module

    def test_a_stop_still_sells_whatever_the_account_state(self):
        """No account fact is an input, so none can suppress the exit."""
        import inspect

        signature = inspect.signature(ep.decide)
        assert set(signature.parameters) == {
            "state", "current_price", "features", "emergency"}
        assert ep.decide(state(), current_price=90.0, features=HEALTHY).sells


class TestUnusableInputs:
    @pytest.mark.parametrize("price", [None, 0.0, -1.0, float("nan"), True, "94"])
    def test_an_unusable_price_holds_rather_than_selling(self, price):
        """A zero or negative tick is a bad feed, not a collapse to zero.
        Firing the stop on it would sell at whatever the market really
        was."""
        out = ep.decide(state(), current_price=price, features=HEALTHY)
        assert out.action == ep.HOLD
        assert out.reason == ep.REASON_INSUFFICIENT_DATA

    def test_no_state_holds(self):
        assert ep.decide(None, current_price=94.0).action == ep.HOLD
