"""EXIT V2 PHASE 2: the shadow decision, never a live input.

Every test in `TestLiveBehaviorUnchanged` and `TestShadowCannotMutate`
is the load-bearing proof this phase requires: the shadow policy reads
`exit_policy.decide()`'s output and never feeds back into it.
"""

import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from s6_live import exit_diagnostics, exit_policy, exit_shadow, exit_snapshot
from s6_live import realtime_features as rf
from s6_live.entry_quality import EntryQuality

NOW = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("STATE_STORE_DB_FILE", tempfile.mktemp(suffix=".db"))
    from state_store.db import open_db

    with open_db() as connection:
        yield connection


def _row(**overrides):
    kwargs = dict(
        position_id="s6pos_test1", symbol="RIG", entry_price=5.79,
        quantity=28, entry_time=(NOW - timedelta(minutes=20)).isoformat(),
        range_high=5.76, range_low=5.71, exit_submitted=0,
    )
    kwargs.update(overrides)
    return kwargs


def _quality(**overrides):
    base = dict(
        symbol="RIG", session="PREMARKET", scanner_variant="S6_ORB5",
        orb_minutes=5, bar_count=15, recent_volume_5m=500.0,
        recent_volume_10m=800.0, recent_volume_15m=1200.0,
        dollar_volume_5m=3000.0, spread_bps=None,
    )
    base.update(overrides)
    return EntryQuality(**base)


def _feats(entry_quality=None, **overrides):
    kwargs = dict(
        symbol="RIG", session="PREMARKET", market_data_asof=NOW, price=5.85,
        vwap=5.80, ema9=5.90, ema21=5.85, volume=100, volume_status=rf.VOLUME_OK,
        volume_expansion=1.5, range_high=5.76, range_low=5.71,
        entry_quality=entry_quality,
    )
    kwargs.update(overrides)
    return rf.SessionFeatures(**kwargs)


def _state(**overrides):
    kwargs = dict(
        symbol="RIG", entry_price=5.79, range_high=5.76, range_low=5.71,
        peak_price=5.90, peak_price_at=NOW.isoformat(), exit_submitted=False,
    )
    kwargs.update(overrides)
    return exit_policy.S6PositionState(**kwargs)


def _tick(conn, *, state=None, feats=None, row=None, now=NOW, current_price=5.85,
          session="PREMARKET"):
    """One full live-decide + Phase1-snapshot + Phase2-shadow tick,
    exactly the sequence s6_live.exit_runtime now runs."""
    state = state or _state()
    feats = feats if feats is not None else _feats(entry_quality=_quality())
    row = row or _row()
    decision = exit_policy.decide(state, current_price=current_price,
                                  features=feats, session=session, now=now)
    diagnostics = exit_diagnostics.evaluate(
        state, features=feats, price=exit_policy._price_of(feats, current_price),
        session=session, now=now, decision=decision)
    prior = exit_snapshot.last_snapshot(conn, row["position_id"])
    snapshot = exit_snapshot.build(conn=conn, position_id=row["position_id"], row=row,
                                   features=feats, diagnostics=diagnostics,
                                   decision=decision, now=now, prior_row=prior)
    record = exit_shadow.build(prior_row=prior, snapshot=snapshot,
                               live_action=decision.action)
    exit_snapshot.persist(conn, record, now=now)
    return record, decision


class TestVwapShadowStateMachine:
    def test_healthy_when_price_at_or_above_vwap(self):
        state, streak = exit_shadow.vwap_shadow_state(5.85, 5.80)
        assert state == exit_shadow.VWAP_HEALTHY
        assert streak == 0

    def test_first_tick_below_vwap_is_a_breach_not_confirmed(self):
        state, streak = exit_shadow.vwap_shadow_state(5.75, 5.80)
        assert state == exit_shadow.VWAP_BREACH
        assert streak == 1

    def test_breach_recovers_when_price_returns_above_vwap(self):
        state, streak = exit_shadow.vwap_shadow_state(
            5.85, 5.80, prior_state=exit_shadow.VWAP_BREACH, prior_streak=1)
        assert state == exit_shadow.VWAP_RECOVERED
        assert streak == 0

    def test_persistent_breach_confirms_at_the_provisional_threshold(self):
        state1, streak1 = exit_shadow.vwap_shadow_state(5.75, 5.80)
        assert state1 == exit_shadow.VWAP_BREACH and streak1 == 1
        state2, streak2 = exit_shadow.vwap_shadow_state(
            5.74, 5.80, prior_state=state1, prior_streak=streak1)
        assert streak2 == 2
        assert state2 == (exit_shadow.VWAP_FAILURE_CONFIRMED
                          if exit_shadow.VWAP_CONFIRMATION_TICKS <= 2
                          else exit_shadow.VWAP_BREACH)

    def test_missing_data_is_unknown_not_a_fabricated_state(self):
        state, streak = exit_shadow.vwap_shadow_state(None, 5.80)
        assert state == exit_shadow.VWAP_UNKNOWN
        assert streak == 0

    def test_healthy_after_a_recovery_resets_to_ordinary_healthy(self):
        state, streak = exit_shadow.vwap_shadow_state(
            5.90, 5.80, prior_state=exit_shadow.VWAP_RECOVERED, prior_streak=0)
        assert state == exit_shadow.VWAP_HEALTHY


class TestStructureShadowState:
    def test_healthy_well_above_range_high_with_no_giveback(self):
        state = exit_shadow.structure_shadow_state(
            price=5.90, range_high=5.76, giveback_fraction=0.0)
        assert state == exit_shadow.STRUCTURE_HEALTHY

    def test_weakening_uses_the_existing_peak_giveback_threshold(self):
        from config import s6_exit_v0 as policy

        state = exit_shadow.structure_shadow_state(
            price=5.80, range_high=5.76, giveback_fraction=policy.PEAK_GIVEBACK_FRACTION)
        assert state == exit_shadow.STRUCTURE_WEAKENING

    def test_failed_breakout_at_or_below_range_high(self):
        state = exit_shadow.structure_shadow_state(
            price=5.76, range_high=5.76, giveback_fraction=1.0)
        assert state == exit_shadow.STRUCTURE_FAILED_BREAKOUT

    def test_unknown_without_price_or_range(self):
        state = exit_shadow.structure_shadow_state(
            price=None, range_high=None, giveback_fraction=None)
        assert state == exit_shadow.STRUCTURE_UNKNOWN


class TestTimeStopIsShadowOnly:
    def test_never_appears_as_a_live_action(self):
        import inspect

        for name in ("submit_order", "submit_buy_order", "_submit_sell",
                     "latch_pending_exit", "mark_exit_submitted"):
            assert name not in inspect.getsource(exit_shadow)

    def test_no_progress_within_the_warning_window_is_none(self):
        state = exit_shadow.time_stop_shadow_state(
            time_in_trade_seconds=5 * 60, peak_gain_pct=0.1, current_gain_pct=0.1)
        assert state is None

    def test_long_stagnant_hold_with_no_favorable_move_warns(self):
        state = exit_shadow.time_stop_shadow_state(
            time_in_trade_seconds=exit_shadow.TIME_STOP_NO_PROGRESS_MINUTES * 60 + 60,
            peak_gain_pct=-0.5, current_gain_pct=-0.5)
        assert state == exit_shadow.TIME_STOP_WARNING

    def test_very_long_stagnant_hold_escalates(self):
        state = exit_shadow.time_stop_shadow_state(
            time_in_trade_seconds=exit_shadow.TIME_STOP_WARNING_MINUTES * 60 + 60,
            peak_gain_pct=-0.5, current_gain_pct=-0.5)
        assert state == exit_shadow.TIME_STOP_EXIT_SHADOW

    def test_a_position_that_was_favorable_at_some_point_never_time_stops(self):
        state = exit_shadow.time_stop_shadow_state(
            time_in_trade_seconds=exit_shadow.TIME_STOP_WARNING_MINUTES * 60 + 600,
            peak_gain_pct=2.0, current_gain_pct=-0.1)
        assert state != exit_shadow.TIME_STOP_EXIT_SHADOW
        assert state != exit_shadow.TIME_STOP_WARNING


class TestDecideShadowHardStopsNeverDelayed:
    @pytest.mark.parametrize("reason", [
        exit_policy.REASON_EMERGENCY, exit_policy.REASON_HARD_RISK_CAP,
        exit_policy.REASON_NO_STRUCTURE, exit_policy.REASON_SESSION_EXIT,
        exit_policy.REASON_RANGE_REENTRY,
    ])
    def test_mirrors_immediately_regardless_of_shadow_state(self, reason):
        result = exit_shadow.decide_shadow(
            live_reason=reason, live_action=exit_policy.SELL,
            vwap_state=exit_shadow.VWAP_HEALTHY, liquidity_state="NORMAL",
            structure_state=exit_shadow.STRUCTURE_HEALTHY, momentum_state="HEALTHY")
        assert result["shadow_v2_decision"] == exit_shadow.WOULD_EXIT
        assert result["would_exit_now"] is True
        assert result["shadow_confidence"] == 1.0

    def test_already_submitted_is_never_reopened_by_shadow(self):
        result = exit_shadow.decide_shadow(
            live_reason=exit_policy.REASON_ALREADY_SUBMITTED, live_action=exit_policy.HOLD,
            vwap_state=exit_shadow.VWAP_HEALTHY, liquidity_state="NORMAL",
            structure_state=exit_shadow.STRUCTURE_HEALTHY, momentum_state="HEALTHY")
        assert result["shadow_v2_decision"] == exit_shadow.ALREADY_EXITING
        assert result["would_exit_now"] is True


class TestVwapConfirmationGate:
    def test_first_breach_tick_holds_for_confirmation_not_an_immediate_exit(self):
        result = exit_shadow.decide_shadow(
            live_reason=exit_policy.REASON_VWAP_FAILURE, live_action=exit_policy.SELL,
            vwap_state=exit_shadow.VWAP_BREACH, liquidity_state="NORMAL",
            structure_state=exit_shadow.STRUCTURE_HEALTHY, momentum_state="WEAKENING")
        assert result["shadow_v2_decision"] == exit_shadow.HOLD_FOR_CONFIRMATION
        assert result["would_exit_now"] is False
        assert result["would_hold_now"] is True

    def test_confirmed_breach_agrees_with_live_but_later(self):
        result = exit_shadow.decide_shadow(
            live_reason=exit_policy.REASON_VWAP_FAILURE, live_action=exit_policy.SELL,
            vwap_state=exit_shadow.VWAP_FAILURE_CONFIRMED, liquidity_state="NORMAL",
            structure_state=exit_shadow.STRUCTURE_HEALTHY, momentum_state="FAILED")
        assert result["shadow_v2_decision"] == exit_shadow.WOULD_EXIT
        assert result["would_exit_now"] is True


class TestLiquidityAloneNeverExits:
    def test_critical_liquidity_with_healthy_everything_else_is_hold(self):
        result = exit_shadow.decide_shadow(
            live_reason=None, live_action=exit_policy.HOLD,
            vwap_state=exit_shadow.VWAP_HEALTHY, liquidity_state="CRITICAL",
            structure_state=exit_shadow.STRUCTURE_HEALTHY, momentum_state="HEALTHY")
        assert result["shadow_v2_decision"] == exit_shadow.HOLD
        assert result["would_exit_now"] is False

    def test_combined_confirmed_vwap_failure_and_critical_liquidity_would_exit(self):
        result = exit_shadow.decide_shadow(
            live_reason=None, live_action=exit_policy.HOLD,
            vwap_state=exit_shadow.VWAP_FAILURE_CONFIRMED, liquidity_state="CRITICAL",
            structure_state=exit_shadow.STRUCTURE_HEALTHY, momentum_state="FAILED")
        assert result["shadow_v2_decision"] == exit_shadow.WOULD_EXIT
        assert "liquidity:CRITICAL" in result["shadow_evidence"]

    def test_combined_failed_breakout_and_critical_liquidity_would_exit(self):
        result = exit_shadow.decide_shadow(
            live_reason=None, live_action=exit_policy.HOLD,
            vwap_state=exit_shadow.VWAP_HEALTHY, liquidity_state="CRITICAL",
            structure_state=exit_shadow.STRUCTURE_FAILED_BREAKOUT, momentum_state="WEAKENING")
        assert result["shadow_v2_decision"] == exit_shadow.WOULD_EXIT

    def test_failed_breakout_alone_without_liquidity_is_still_hold(self):
        result = exit_shadow.decide_shadow(
            live_reason=None, live_action=exit_policy.HOLD,
            vwap_state=exit_shadow.VWAP_HEALTHY, liquidity_state="NORMAL",
            structure_state=exit_shadow.STRUCTURE_FAILED_BREAKOUT, momentum_state="WEAKENING")
        assert result["shadow_v2_decision"] == exit_shadow.HOLD


class TestLiveBehaviorUnchanged:
    """Re-derives exit_policy.decide() from scratch after a full
    Phase1+Phase2 tick and asserts byte-identical output -- the shadow
    computation reads the decision, it cannot have influenced it."""

    @pytest.mark.parametrize("current_price,range_high,range_low,extra_feats,expect_reason", [
        (4.50, 5.76, 4.60, {}, exit_policy.REASON_HARD_RISK_CAP),
        (5.75, 5.76, 5.71, {}, exit_policy.REASON_RANGE_REENTRY),
        (5.90, 5.60, 5.50, {"vwap": 6.0}, exit_policy.REASON_VWAP_FAILURE),
    ])
    def test_decision_is_identical_with_and_without_the_shadow_call(
            self, conn, current_price, range_high, range_low, extra_feats, expect_reason):
        state = _state(range_high=range_high, range_low=range_low, peak_price=current_price)
        base = {"vwap": 5.5, "ema9": 5.9, "ema21": 5.8}
        base.update(extra_feats)
        feats = _feats(entry_quality=_quality(), price=current_price, **base)

        decision_before = exit_policy.decide(state, current_price=current_price,
                                             features=feats, session="PREMARKET", now=NOW)
        record, decision_after_call = _tick(conn, state=state, feats=feats,
                                            current_price=current_price)
        decision_after = exit_policy.decide(state, current_price=current_price,
                                            features=feats, session="PREMARKET", now=NOW)
        assert decision_after.as_dict() == decision_before.as_dict()
        assert decision_before.reason == expect_reason
        assert record["current_exit_reason"] == expect_reason  # unaffected by shadow build


class TestShadowCannotMutate:
    def test_module_imports_nothing_that_can_submit_or_latch(self):
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(exit_shadow))
        forbidden = {"position_store", "exit_intent_ledger", "execution_engine",
                     "kis_broker", "order_repository"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [str(getattr(node, "module", "") or "")]
                names += [a.name for a in node.names]
                for name in names:
                    for seg in name.split("."):
                        assert seg not in forbidden, seg

    def test_a_wound_exit_now_result_does_not_change_the_position_row(self, conn):
        from s6_live import position_store as ps

        pid = ps.record_submission(conn, symbol="RIG", variant="S6-R",
                                   entry_session="PREMARKET", range_high=5.76,
                                   range_low=5.71, now=NOW)
        ps.open_from_fill(conn, pid, quantity=28, average_fill_price=5.79, now=NOW)
        before = dict(ps.load(conn, pid))

        feats = _feats(entry_quality=_quality(dollar_volume_5m=17.0), price=5.70, vwap=5.60)
        state = _state(range_high=100.0, range_low=99.0)  # forces a confirmed-style tick
        _tick(conn, state=state, feats=feats, row=dict(before), current_price=5.70)

        after = dict(ps.load(conn, pid))
        assert after["status"] == before["status"]
        assert after["exit_submitted"] == before["exit_submitted"]
        assert after["quantity"] == before["quantity"]

    def test_no_extra_broker_calls(self, conn):
        """`exit_shadow.build`/`decide_shadow` take no broker argument
        at all -- there is nothing to call."""
        import inspect

        sig_build = inspect.signature(exit_shadow.build)
        sig_decide = inspect.signature(exit_shadow.decide_shadow)
        for sig in (sig_build, sig_decide):
            assert "broker" not in sig.parameters


class TestRestartPersistence:
    def test_shadow_columns_survive_a_fresh_connection(self, monkeypatch):
        db_file = tempfile.mktemp(suffix=".db")
        monkeypatch.setenv("STATE_STORE_DB_FILE", db_file)
        from state_store.db import open_db

        with open_db() as c1:
            _tick(c1)

        with open_db() as c2:
            row = dict(c2.execute("SELECT * FROM s6_exit_snapshots").fetchone())
            assert row["shadow_v2_decision"] is not None
            assert row["shadow_vwap_state"] is not None

    def test_consecutive_breach_ticks_build_a_real_streak_across_restarts(self, monkeypatch):
        db_file = tempfile.mktemp(suffix=".db")
        monkeypatch.setenv("STATE_STORE_DB_FILE", db_file)
        from state_store.db import open_db

        feats_below = _feats(entry_quality=_quality(), price=5.70, vwap=5.80)
        with open_db() as c1:
            r1, _d = _tick(c1, feats=feats_below, current_price=5.70, now=NOW)
        with open_db() as c2:
            r2, _d = _tick(c2, feats=feats_below, current_price=5.69,
                           now=NOW + timedelta(minutes=1))
        assert r1["shadow_vwap_breach_streak"] == 1
        assert r2["shadow_vwap_breach_streak"] == 2


class TestS1S2Unaffected:
    def test_s1_exit_runtime_does_not_import_exit_shadow(self):
        from pathlib import Path

        text = Path("s1_live/exit_runtime.py").read_text(encoding="utf-8")
        assert "exit_shadow" not in text

    def test_s2_exit_runtime_does_not_import_exit_shadow(self):
        from pathlib import Path

        text = Path("s2_live/exit_runtime.py").read_text(encoding="utf-8")
        assert "exit_shadow" not in text
