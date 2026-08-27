"""The three exit rules that could not fire, and why they now can.

The defect
----------
S6's exit engine was handed `s1_executor.make_features_fn()`, built for
S1's DAILY trend axis: `intraday_lookback_days=0, require_intraday=False`.
Intraday fields are left None when there are no minute bars, and
`volume_expansion` was never a `SymbolFeatures` field at all -- the ORB
scanner computes it into the candidate row and nothing recomputed it
after entry. Measured against the deployed code on 2026-08-27:

    vwap=None  ema9=None  ema21=None  volume_expansion=(absent)

so `vwap_failed`, `ema_structure_failed` and `volume_decayed` each
returned None on every tick. Three of seven rules, silently off. The DT
position was left with only two live price rules, sitting 3.80% and
7.11% below its entry.

No threshold is changed here. The rules were always correct; they were
being asked questions about values that never arrived.

Nothing in this file places an order.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import s6_exit_v0 as policy  # noqa: E402
from s6_live import exit_diagnostics, exit_policy  # noqa: E402
from s6_live import realtime_features as rf  # noqa: E402

NOW = datetime(2026, 8, 26, 17, 0, tzinfo=timezone.utc)


def _state(**overrides):
    kwargs = dict(
        symbol="DT", entry_price=52.75,
        range_high=50.747398376464844, range_low=49.0,
        peak_price=53.0,
        entry_volume_expansion=1.657214579511058,
        peak_volume_expansion=1.657214579511058,
        exit_submitted=False,
    )
    kwargs.update(overrides)
    return exit_policy.S6PositionState(**kwargs)


def _features(**overrides):
    kwargs = dict(symbol="DT", session="REGULAR", market_data_asof=NOW,
                  built_at=NOW, price=52.0, vwap=52.5, ema9=52.1, ema21=52.0,
                  volume=1_000_000.0, volume_status=rf.VOLUME_OK,
                  volume_expansion=1.6, range_high=50.747, range_low=49.0,
                  bar_count=40)
    kwargs.update(overrides)
    return rf.SessionFeatures(**kwargs)


class TestTheRulesCanNowFire:
    def test_VWAP_FAILURE_fires_when_price_is_below_vwap(self):
        d = exit_policy.decide(_state(), current_price=52.0,
                               features=_features(price=52.0, vwap=52.5),
                               session="REGULAR", now=NOW)
        assert d.sells and d.reason == exit_policy.REASON_VWAP_FAILURE

    def test_EMA_STRUCTURE_FAILURE_fires_when_ema9_crosses_under(self):
        d = exit_policy.decide(
            _state(), current_price=52.0,
            features=_features(price=52.0, vwap=51.0, ema9=51.9, ema21=52.0),
            session="REGULAR", now=NOW)
        assert d.sells and d.reason == exit_policy.REASON_EMA_STRUCTURE_FAILURE

    def test_VOLUME_DECAY_PRICE_WEAKNESS_fires_on_decay_plus_weakness(self):
        """peak expansion 1.657 -> target 1 + 0.657*0.5 = 1.3286."""
        target = 1.0 + (1.657214579511058 - 1.0) * (1.0 - policy.VOLUME_DECAY_FRACTION)
        assert target == pytest.approx(1.3286, abs=1e-3)
        d = exit_policy.decide(
            _state(), current_price=52.0,
            # Above VWAP and EMA-healthy so the earlier rules abstain and
            # this one is genuinely what fires; price below peak is the
            # weakness half.
            features=_features(price=52.0, vwap=51.0, ema9=52.5, ema21=52.0,
                               volume_expansion=1.2),
            session="REGULAR", now=NOW)
        assert d.sells
        assert d.reason == exit_policy.REASON_VOLUME_DECAY_PRICE_WEAKNESS

    def test_volume_decay_alone_is_still_not_an_exit(self):
        """Unchanged behaviour: a breakout continuing on falling volume
        is not by itself broken."""
        d = exit_policy.decide(
            _state(peak_price=51.0), current_price=52.0,
            features=_features(price=52.0, vwap=51.0, ema9=52.5, ema21=52.0,
                               volume_expansion=1.2),
            session="REGULAR", now=NOW)
        assert not d.sells

    def test_a_healthy_position_still_holds(self):
        d = exit_policy.decide(
            _state(peak_price=51.9), current_price=52.0,
            features=_features(price=52.0, vwap=51.0, ema9=52.5, ema21=52.0,
                               volume_expansion=1.6),
            session="REGULAR", now=NOW)
        assert not d.sells


class TestProfitNeverBlocksAnExit:
    """§26/§44 -- structure decides, not P&L."""

    @pytest.mark.parametrize("features,expected", [
        (dict(price=60.0, vwap=61.0, ema9=60.5, ema21=60.0),
         exit_policy.REASON_VWAP_FAILURE),
        (dict(price=60.0, vwap=59.0, ema9=59.5, ema21=60.0),
         exit_policy.REASON_EMA_STRUCTURE_FAILURE),
        (dict(price=60.0, vwap=59.0, ema9=60.5, ema21=60.0,
              volume_expansion=1.1),
         exit_policy.REASON_VOLUME_DECAY_PRICE_WEAKNESS),
    ])
    def test_a_position_up_14_percent_still_exits(self, features, expected):
        """Entry 52.75, price 60.00 -> +13.7%. The rule still fires."""
        state = _state(peak_price=61.0)
        d = exit_policy.decide(state, current_price=60.0,
                               features=_features(**features),
                               session="REGULAR", now=NOW)
        assert d.sells and d.reason == expected

    def test_hard_risk_ignores_profit_too(self):
        d = exit_policy.decide(_state(), current_price=48.0,
                               features=_features(price=48.0),
                               session="REGULAR", now=NOW)
        assert d.sells and d.reason == exit_policy.REASON_HARD_RISK_CAP


class TestUnavailableIsNotFalse:
    """§21/§33 -- a rule that cannot run must not look like a quiet one."""

    def test_a_missing_vwap_is_reported_unavailable(self):
        feats = _features(vwap=None, unavailable={"vwap": "not computable"})
        record = exit_diagnostics.evaluate(
            _state(), features=feats, price=52.0, session="REGULAR", now=NOW)
        assert record["conditions"][exit_policy.REASON_VWAP_FAILURE] == \
            exit_diagnostics.UNAVAILABLE

    def test_missing_emas_are_reported_unavailable(self):
        feats = _features(ema9=None, ema21=None,
                          unavailable={"ema9": "x", "ema21": "x"})
        record = exit_diagnostics.evaluate(
            _state(), features=feats, price=52.0, session="REGULAR", now=NOW)
        assert record["conditions"][
            exit_policy.REASON_EMA_STRUCTURE_FAILURE] == exit_diagnostics.UNAVAILABLE

    def test_missing_volume_expansion_is_reported_unavailable(self):
        feats = _features(volume_expansion=None,
                          volume_status=rf.VOLUME_DATA_UNAVAILABLE,
                          unavailable={"volume_expansion": "no volume"})
        record = exit_diagnostics.evaluate(
            _state(), features=feats, price=52.0, session="REGULAR", now=NOW)
        assert record["conditions"][
            exit_policy.REASON_VOLUME_DECAY_PRICE_WEAKNESS] == \
            exit_diagnostics.UNAVAILABLE

    def test_the_exact_DT_tick_names_all_three_dead_rules(self):
        """What the deployed engine actually received."""
        feats = rf.SessionFeatures(
            symbol="DT", session="AFTER_HOURS",
            unavailable={"vwap": "x", "ema9": "x", "ema21": "x",
                         "volume_expansion": "x"})
        record = exit_diagnostics.evaluate(
            _state(), features=feats, price=51.6, session="AFTER_HOURS",
            now=NOW)
        assert set(record["unavailable_rules"]) == {
            exit_policy.REASON_VWAP_FAILURE,
            exit_policy.REASON_EMA_STRUCTURE_FAILURE,
            exit_policy.REASON_VOLUME_DECAY_PRICE_WEAKNESS,
        }
        # ...while the two live price rules still answered.
        assert record["conditions"][exit_policy.REASON_HARD_RISK_CAP] == \
            exit_diagnostics.FALSE
        assert record["conditions"][exit_policy.REASON_RANGE_REENTRY] == \
            exit_diagnostics.FALSE

    def test_a_working_tick_reports_no_unavailable_rules(self):
        record = exit_diagnostics.evaluate(
            _state(), features=_features(), price=52.0, session="REGULAR",
            now=NOW)
        assert record["unavailable_rules"] == []

    def test_the_record_carries_what_an_operator_needs(self):
        record = exit_diagnostics.evaluate(
            _state(), features=_features(), price=52.0, session="REGULAR",
            now=NOW)
        for key in ("symbol", "session", "price", "entry_price", "pnl_pct",
                    "range_high", "range_low", "conditions", "features"):
            assert key in record, key
        assert record["pnl_pct"] == pytest.approx((52.0 / 52.75 - 1) * 100)


class TestTheRuntimeUsesS6sOwnFeatures:
    def test_the_runtime_no_longer_hands_S6_the_S1_daily_axis(self):
        source = (REPO_ROOT / "scripts" / "run_s6_runtime.py").read_text(
            encoding="utf-8")
        assert "realtime_features.make_features_fn(" in source
        # Checked on code, not prose: the comment above the fix names the
        # old call deliberately, so that the reason survives the change.
        code = "\n".join(line for line in source.splitlines()
                         if not line.strip().startswith("#"))
        assert "s1_executor.make_features_fn()" not in code

    def test_the_monitor_records_diagnostics_on_every_hold(self):
        import inspect

        from s6_live import exit_runtime

        source = inspect.getsource(exit_runtime.evaluate_position)
        assert "exit_diagnostics.evaluate" in source
        assert 'ACTION_HELD,\n                           decision.reason, diagnostics' \
            in source
