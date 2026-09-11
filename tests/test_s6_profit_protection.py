"""EXIT V2 integrated live profit protection.

These tests exercise the existing S6 decision and submission path.  They do
not introduce a second exit engine or a broker double: the new rule is only a
new P4 reason selected by ``s6_live.exit_policy.decide``.
"""
from datetime import datetime, timezone

import pytest

from config import s6_exit_v0 as policy
from s6_live import exit_policy
from s6_live import realtime_features as rf


NOW = datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc)


def _state(**overrides):
    values = dict(symbol="NTSK", entry_price=100.0, range_high=95.0,
                  range_low=90.0, peak_price=103.0, exit_submitted=False)
    values.update(overrides)
    return exit_policy.S6PositionState(**values)


def _features(**overrides):
    values = dict(symbol="NTSK", session="REGULAR", market_data_asof=NOW,
                  built_at=NOW, price=102.0, vwap=100.0, ema9=102.0,
                  ema21=101.0, volume=1000.0,
                  volume_status=rf.VOLUME_OK, volume_expansion=1.5,
                  range_high=95.0, range_low=90.0, bar_count=20)
    values.update(overrides)
    return rf.SessionFeatures(**values)


def _assessment(state=None, feats=None, *, price=None, vwap_state="VWAP_HEALTHY",
                history=()):
    state = state or _state()
    feats = feats or _features()
    price = feats.price if price is None else price
    return exit_policy.profit_protection_assessment(
        state, features=feats, current_price=price, vwap_state=vwap_state,
        price_history=history)


def _decide(state=None, feats=None, *, protection=None):
    state = state or _state()
    feats = feats or _features()
    return exit_policy.decide(state, current_price=feats.price, features=feats,
                              session="REGULAR", now=NOW,
                              profit_protection=protection)


class TestLiveProfitProtection:
    def test_plus_three_percent_healthy_trend_holds(self):
        state = _state(peak_price=103.0)
        feats = _features(price=103.0, vwap=101.0, ema9=103.0, ema21=102.0)
        assessment = _assessment(state, feats)
        assert assessment["armed"] and not assessment["giveback_warning"]
        assert _decide(state, feats, protection=assessment).action == exit_policy.HOLD

    def test_plus_five_percent_healthy_trend_holds(self):
        state = _state(peak_price=105.0)
        feats = _features(price=104.8, vwap=103.0, ema9=104.0, ema21=103.0)
        assessment = _assessment(state, feats)
        assert assessment["armed"] and not assessment["trigger"]
        assert _decide(state, feats, protection=assessment).action == exit_policy.HOLD

    def test_armed_alone_holds(self):
        feats = _features(price=103.0)
        assessment = _assessment(_state(), feats)
        assert assessment["armed"] and not assessment["giveback_warning"]
        assert _decide(_state(), feats, protection=assessment).action == exit_policy.HOLD

    def test_giveback_alone_holds(self):
        state = _state(peak_price=101.0)  # never armed at +2%
        feats = _features(price=100.0)
        assessment = _assessment(state, feats)
        assert not assessment["armed"] and not assessment["trigger"]
        assert _decide(state, feats, protection=assessment).action == exit_policy.HOLD

    def test_giveback_plus_ema_failure_selects_live_sell(self):
        feats = _features(price=102.0, vwap=100.0, ema9=100.0, ema21=101.0)
        assessment = _assessment(_state(), feats)
        assert assessment["giveback_warning"] and assessment["ema_structure_failure"]
        decision = _decide(_state(), feats, protection=assessment)
        assert decision.sells and decision.reason == exit_policy.REASON_PROFIT_PROTECTION_EXIT

    def test_giveback_plus_confirmed_vwap_failure_selects_live_sell(self):
        feats = _features(price=102.0, vwap=103.0, ema9=102.0, ema21=101.0)
        assessment = _assessment(_state(), feats,
                                 vwap_state="VWAP_FAILURE_CONFIRMED")
        assert assessment["vwap_failure_confirmed"]
        decision = _decide(_state(), feats, protection=assessment)
        assert decision.sells and decision.reason == exit_policy.REASON_PROFIT_PROTECTION_EXIT

    def test_giveback_plus_confirmed_lower_high_lower_low_selects_live_sell(self):
        feats = _features(price=100.0, vwap=99.0, ema9=101.0, ema21=100.0)
        history = (100.0, 105.0, 102.0, 104.0, 101.0, 103.0)
        assessment = _assessment(_state(), feats, history=history)
        assert assessment["lower_high"] and assessment["lower_low"]
        decision = _decide(_state(), feats, protection=assessment)
        assert decision.sells and decision.reason == exit_policy.REASON_PROFIT_PROTECTION_EXIT

    def test_single_vwap_flicker_then_recovery_never_selects_profit_protection(self):
        feats = _features(price=102.0, vwap=100.0, ema9=102.0, ema21=101.0)
        assessment = _assessment(_state(), feats, vwap_state="VWAP_RECOVERED")
        assert not assessment["vwap_failure_confirmed"] and not assessment["trigger"]
        assert _decide(_state(), feats, protection=assessment).action == exit_policy.HOLD

    def test_price_structure_uses_only_confirmed_pivots(self):
        # The terminal 103 is not a high until a later price arrives.
        structure = exit_policy.causal_price_structure(
            (100.0, 105.0, 102.0, 104.0, 101.0, 103.0))
        assert structure["lower_high"] is True
        assert structure["lower_low"] is True
        assert structure["confirmed_swing_highs"] == [105.0, 104.0]


class TestPriorityAndExistingBehavior:
    @pytest.mark.parametrize("price,expect", [
        (89.0, exit_policy.REASON_HARD_RISK_CAP),
        (95.0, exit_policy.REASON_RANGE_REENTRY),
    ])
    def test_existing_price_safety_exits_outrank_profit_protection(self, price, expect):
        feats = _features(price=price, vwap=200.0, ema9=90.0, ema21=100.0)
        decision = _decide(_state(), feats, protection={"trigger": True})
        assert decision.reason == expect

    def test_existing_standalone_vwap_behavior_is_unchanged_without_pp_context(self):
        feats = _features(price=102.0, vwap=103.0)
        decision = _decide(_state(), feats, protection=None)
        assert decision.reason == exit_policy.REASON_VWAP_FAILURE

    def test_existing_standalone_ema_behavior_is_unchanged_without_pp_context(self):
        feats = _features(price=102.0, vwap=100.0, ema9=100.0, ema21=101.0)
        decision = _decide(_state(), feats, protection=None)
        assert decision.reason == exit_policy.REASON_EMA_STRUCTURE_FAILURE

    def test_existing_exit_submitted_latch_prevents_duplicate_sell(self):
        feats = _features(price=102.0, vwap=100.0, ema9=100.0, ema21=101.0)
        assessment = _assessment(_state(), feats)
        decision = _decide(_state(exit_submitted=True), feats, protection=assessment)
        assert decision.action == exit_policy.HOLD
        assert decision.reason == exit_policy.REASON_ALREADY_SUBMITTED


def test_s1_to_s5_and_buy_path_do_not_import_profit_protection():
    from pathlib import Path
    for path in ("s1_live/exit_runtime.py", "s2_live/exit_runtime.py",
                 "s6_live/precision_watch.py", "s6_live/qualification.py"):
        assert "profit_protection_assessment" not in Path(path).read_text()


def test_replay_reports_first_causal_profit_protection_tick():
    from scripts.replay_s6_profit_protection import replay_rows

    prices = (100.0, 105.0, 102.0, 104.0, 101.0, 103.0, 100.0)
    rows = []
    for number, price in enumerate(prices):
        rows.append({
            "symbol": "NTSK", "entry_price": 100.0, "range_high": 95.0,
            "range_low": 90.0, "peak_price": max(prices[:number + 1]),
            "exit_submitted": 0, "current_price": price,
            "vwap": 99.0, "ema9": 101.0, "ema21": 100.0,
            "shadow_vwap_state": "VWAP_HEALTHY",
            "evaluated_at": f"2026-09-11T15:0{number}:00+00:00",
        })
    milestones, assessments = replay_rows(rows)
    assert milestones["armed_at"] == rows[1]["evaluated_at"]
    assert milestones["giveback_warning_at"] == rows[2]["evaluated_at"]
    assert milestones["lower_high_lower_low_at"] == rows[5]["evaluated_at"]
    assert milestones["profit_protection_exit_at"] == rows[5]["evaluated_at"]
    assert assessments[5]["trigger"] is True
