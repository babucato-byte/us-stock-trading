"""S6_EXIT_V0: the stop is the setup's own geometry.

The design question this settles is §4's. S1 uses -6% and S2 -3%, both
measured for those strategies; copying either would apply a level chosen
for a different thesis, and picking a third from one observation (IEFA,
MAE -0.155%) would encode a single candidate into the risk policy.

Neither is needed. S6's thesis IS the range, so price below the range LOW
has falsified the entire setup -- a level computed from the candidate's
own bars, with no free parameter, differing per position exactly as the
setups differ. There is no percentage to tune and none is invented.
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import s6_exit_v0 as policy  # noqa: E402
from market_hours import EASTERN  # noqa: E402
from s6_live import exit_policy as ex  # noqa: E402

T0 = datetime(2026, 8, 21, 12, 0, tzinfo=EASTERN)


class Features:
    def __init__(self, price=101.0, vwap=100.0, ema9=100.5, ema21=100.0,
                 volume_expansion=2.0):
        self.price, self.vwap = price, vwap
        self.ema9, self.ema21 = ema9, ema21
        self.volume_expansion = volume_expansion


def held(**kw):
    kw.setdefault("symbol", "ABC")
    kw.setdefault("entry_price", 100.0)
    kw.setdefault("range_high", 99.5)
    kw.setdefault("range_low", 99.0)
    kw.setdefault("variant", "S6-R")
    kw.setdefault("entry_volume_expansion", 2.0)
    return ex.S6PositionState(**kw)


class TestNoPercentageIsInvented:
    def test_there_is_no_catastrophic_cap(self):
        assert policy.CATASTROPHIC_CAP_PCT is None
        assert policy.HARD_RISK_LEVEL == "RANGE_LOW"

    def test_no_other_strategys_level_is_present(self):
        source = (REPO_ROOT / "config" / "s6_exit_v0.py").read_text()
        code = "\n".join(l for l in source.splitlines()
                         if not l.strip().startswith("#")
                         and '"""' not in l)
        assert "6.0" not in code and "3.0" not in code

    def test_it_imports_no_other_strategys_policy(self):
        import ast

        for module in ("config/s6_exit_v0.py", "s6_live/exit_policy.py"):
            source = (REPO_ROOT / module).read_text()
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [str(getattr(node, "module", "") or "")]
                    names += [a.name for a in node.names]
                    for name in names:
                        assert "s1_exit" not in name and "s2_exit" not in name

    def test_the_stop_differs_per_position(self):
        """A percentage would be the same for every trade; the range is
        the setup's own geometry."""
        assert policy.structural_stop(99.0) == 99.0
        assert policy.structural_stop(42.5) == 42.5

    def test_the_basis_records_that_it_is_not_measured(self):
        assert policy.MAX_LOSS_IS_MEASURED is False
        assert "STRUCTURAL_STOP" in policy.MAX_LOSS_BASIS
        assert policy.REEVALUATION_QUESTIONS


class TestPriority:
    def test_emergency_outranks_everything(self):
        decision = ex.decide(held(), features=None, emergency=True)
        assert decision.sells and decision.reason == ex.REASON_EMERGENCY

    def test_below_the_range_low_is_the_hard_risk_exit(self):
        decision = ex.decide(held(), current_price=98.9, features=Features(98.9))
        assert decision.reason == ex.REASON_HARD_RISK_CAP
        assert decision.detail["structural_stop"] == 99.0

    def test_the_hard_risk_exit_outranks_range_reentry(self):
        """A position that gaps straight through the range must not be
        reported as an ordinary re-entry."""
        decision = ex.decide(held(), current_price=98.0, features=Features(98.0))
        assert decision.reason == ex.REASON_HARD_RISK_CAP

    def test_back_inside_the_range_is_a_reentry(self):
        decision = ex.decide(held(), current_price=99.4,
                             features=Features(99.4))
        assert decision.reason == ex.REASON_RANGE_REENTRY

    def test_vwap_failure_when_the_range_still_holds(self):
        decision = ex.decide(held(), current_price=101.0,
                             features=Features(price=101.0, vwap=102.0))
        assert decision.reason == ex.REASON_VWAP_FAILURE

    def test_ema_turnover_is_its_own_exit(self):
        decision = ex.decide(held(), current_price=101.0,
                             features=Features(price=101.0, vwap=100.0,
                                               ema9=100.0, ema21=100.5))
        assert decision.reason == ex.REASON_EMA_STRUCTURE_FAILURE

    def test_a_healthy_breakout_is_held(self):
        decision = ex.decide(held(peak_price=101.0), current_price=101.5,
                             features=Features(price=101.5))
        assert decision.action == ex.HOLD


class TestVolumeDecayNeverExitsAlone:
    def test_decay_with_price_still_working_is_held(self):
        """A breakout continuing on lighter participation is a normal
        winning shape; cutting it removes the trades that worked."""
        state = held(peak_volume_expansion=4.0, peak_price=101.0)
        decision = ex.decide(state, current_price=102.0,
                             features=Features(price=102.0, vwap=100.0,
                                               volume_expansion=2.0))
        assert decision.action == ex.HOLD

    def test_decay_with_price_weakness_exits_when_enforced(self, monkeypatch):
        """Peak 103 over a range high of 99.5; 101 has given back 2.0 of
        the 3.5 gained. The give-back half is provisional and ships
        unenforced, so the SELL is only reachable with the switch on."""
        monkeypatch.setattr(policy, "ENFORCE_PEAK_GIVEBACK_EXIT", True)
        state = held(peak_volume_expansion=4.0, peak_price=103.0)
        decision = ex.decide(state, current_price=101.0,
                             features=Features(price=101.0, vwap=100.0,
                                               volume_expansion=2.0))
        assert decision.reason == ex.REASON_VOLUME_DECAY_PRICE_WEAKNESS
        assert decision.detail["weakness"] == "GAVE_BACK_PEAK"

    def test_the_same_case_is_held_and_recorded_while_unenforced(self):
        assert policy.ENFORCE_PEAK_GIVEBACK_EXIT is False
        state = held(peak_volume_expansion=4.0, peak_price=103.0)
        decision = ex.decide(state, current_price=101.0,
                             features=Features(price=101.0, vwap=100.0,
                                               volume_expansion=2.0))
        assert decision.action == ex.HOLD
        assert decision.detail["would_sell_reason"] == \
            ex.REASON_VOLUME_DECAY_PRICE_WEAKNESS
        assert decision.detail["peak_exit_enforced"] is False

    def test_a_position_never_elevated_cannot_decay(self):
        state = held(peak_volume_expansion=1.0, peak_price=103.0)
        decision = ex.decide(state, current_price=101.0,
                             features=Features(price=101.0, vwap=100.0,
                                               volume_expansion=0.1))
        assert decision.action == ex.HOLD


class TestSessionExit:
    def test_no_overnight_carry_during_validation(self):
        assert policy.ALLOW_OVERNIGHT_CARRY is False
        assert policy.EXIT_ON_SESSION_END is True

    def test_a_healthy_position_exits_at_the_boundary(self):
        near_close = datetime(2026, 8, 21, 15, 50, tzinfo=EASTERN)
        decision = ex.decide(held(peak_price=101.0), current_price=101.5,
                             features=Features(price=101.5), session="REGULAR",
                             now=near_close)
        assert decision.reason == ex.REASON_SESSION_EXIT

    def test_session_exit_is_last(self):
        """Anything saying the position is actually wrong is the reason
        instead."""
        near_close = datetime(2026, 8, 21, 15, 50, tzinfo=EASTERN)
        decision = ex.decide(held(), current_price=98.9,
                             features=Features(98.9), session="REGULAR",
                             now=near_close)
        assert decision.reason == ex.REASON_HARD_RISK_CAP

    def test_an_unreadable_clock_does_not_liquidate(self):
        assert ex.session_ending("REGULAR", None) is None


class TestSafetyProperties:
    def test_a_position_without_a_range_is_exited_not_held(self):
        """It cannot normally exist -- qualification refuses a candidate
        without a range -- so this is a corrupted row, and holding it
        would mean holding something with no expressible stop."""
        decision = ex.decide(held(range_low=None), current_price=100.0,
                             features=Features(100.0))
        assert decision.sells
        assert decision.reason == ex.REASON_NO_STRUCTURE

    def test_missing_data_holds_rather_than_selling(self):
        decision = ex.decide(held(), current_price=None, features=None)
        assert decision.action == ex.HOLD
        assert decision.reason == ex.REASON_INSUFFICIENT_DATA

    def test_a_submitted_exit_is_not_decided_again(self):
        decision = ex.decide(held(exit_submitted=True), current_price=50.0,
                             features=Features(50.0))
        assert decision.action == ex.HOLD
        assert decision.reason == ex.REASON_ALREADY_SUBMITTED

    def test_every_condition_true_yields_one_action(self):
        near_close = datetime(2026, 8, 21, 15, 50, tzinfo=EASTERN)
        decision = ex.decide(
            held(peak_volume_expansion=4.0, peak_price=103.0),
            current_price=90.0,
            features=Features(price=90.0, vwap=100.0, ema9=99.0, ema21=100.0,
                              volume_expansion=1.0),
            session="REGULAR", now=near_close)
        assert decision.action == ex.SELL
        assert decision.reason in ex.EXIT_REASONS

    def test_nothing_reads_pnl(self):
        import ast

        source = (REPO_ROOT / "s6_live" / "exit_policy.py").read_text()
        used = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Name):
                used.add(node.id.lower())
            elif isinstance(node, ast.Attribute):
                used.add(node.attr.lower())
        assert not (used & {"pnl", "profit", "realized_pnl", "unrealised_r"})

    def test_exits_are_never_gated_by_entry_risk(self):
        import ast

        banned = {"position_limits", "kill_switch_state", "order_gate",
                  "allocator", "brokers", "kis_broker"}
        source = (REPO_ROOT / "s6_live" / "exit_policy.py").read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [str(getattr(node, "module", "") or "")]
                names += [a.name for a in node.names]
                for name in names:
                    for segment in name.split("."):
                        assert segment not in banned, name

    def test_the_levels_travel_on_every_decision(self):
        decision = ex.decide(held(peak_price=101.0), current_price=101.5,
                             features=Features(price=101.5))
        assert decision.action == ex.HOLD
        for field in ("range_high", "range_low", "structural_stop",
                      "hard_risk_level", "variant"):
            assert field in decision.detail
