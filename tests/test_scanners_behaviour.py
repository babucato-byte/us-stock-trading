"""Each scanner's own conditions: what passes, what fails, and why.

One class per scanner. The FAIL tests are as important as the PASS ones
and are written to fail for a NAMED reason rather than just "returned
None" -- a scanner that rejects everything for the wrong stated reason
looks identical to one working correctly until month 2 tries to
calibrate it.

The tests also pin the things that distinguish each scanner from its
neighbour, since that is what the whole comparison depends on:

  S1  rejects an ADX that is high but flat (already-trending, not turning)
  S2  rejects a name that already ran, no matter how big the volume
  S3  keeps an already-broken-out name but SCORES it down
  S4  is the existing scanner's verdict, unchanged
  S5  rejects a pullback on heavier volume than the impulse
  S6  distinguishes a closing breakout from a wick
"""

import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scanners.base.market_data_provider import SymbolData  # noqa: E402
from scanners.base.scanner_base import Rejected  # noqa: E402
from scanners.registry import build_scanner  # noqa: E402
from tests import scanner_fixtures as fx  # noqa: E402

DAY = "2026-08-12"


def check(scanner, bundle):
    """Run `check` and return (reasons, context) or raise Rejected."""
    features = scanner.build_features(bundle)
    context = {}
    reasons = scanner.check(features, bundle, context)
    return reasons, context


def evaluate(scanner, bundle):
    return scanner.evaluate(bundle, trading_day=DAY)


class TestHmaEarlyTrend:
    def setup_method(self):
        self.scanner = build_scanner("hma_early_trend")

    def test_passes_a_turning_long_trend(self):
        signal = evaluate(self.scanner, fx.uptrend_bundle())
        assert signal is not None
        assert signal.scanner_name == "hma_early_trend"
        assert signal.hma89 > signal.hma200
        assert signal.hma200_slope > 0
        assert signal.adx > 20

    def test_rejects_price_below_hma200(self):
        closes = fx.accelerating_uptrend()
        closes = closes.copy()
        closes[-1] = closes[-1] * 0.5
        bundle = SymbolData(symbol="T", daily=fx.daily_frame(closes))
        with pytest.raises(Rejected, match="below HMA200"):
            check(self.scanner, bundle)

    def test_rejects_a_flat_adx_even_when_it_is_high(self):
        """A perfectly linear ramp has ADX pinned at 100 and NOT rising.
        That is an established trend, not one turning up -- exactly the
        thing S1 exists to exclude from the already-moved population."""
        closes = np.linspace(20.0, 80.0, 320)
        bundle = SymbolData(symbol="T", daily=fx.daily_frame(closes))
        with pytest.raises(Rejected, match="ADX not rising"):
            check(self.scanner, bundle)

    def test_records_extension_but_never_rejects_on_it(self):
        """Section S1/8: a hard extension cap now would decide one of
        month 1's questions by making sure no evidence is collected."""
        signal = evaluate(self.scanner, fx.uptrend_bundle())
        assert signal.extension_hma200_pct is not None
        assert signal.extension_hma89_pct is not None

    def test_a_stretched_name_still_passes_but_scores_lower(self):
        base = fx.uptrend_bundle()
        stretched_closes = fx.accelerating_uptrend().copy()
        stretched_closes[-1] *= 1.35
        stretched = SymbolData(symbol="T", daily=fx.daily_frame(stretched_closes))

        base_signal = evaluate(self.scanner, base)
        stretched_signal = evaluate(self.scanner, stretched)
        assert stretched_signal is not None
        assert stretched_signal.extension_hma200_pct > base_signal.extension_hma200_pct
        assert stretched_signal.scanner_score < base_signal.scanner_score

    def test_records_the_hma_cross_recency_field(self):
        signal = evaluate(self.scanner, fx.uptrend_bundle())
        assert "hma89_cross_hma200_recent" in signal.metrics
        assert "bars_since_hma_cross" in signal.metrics

    def test_reasons_are_specific_not_a_pass_flag(self):
        """Section 27."""
        signal = evaluate(self.scanner, fx.uptrend_bundle())
        assert len(signal.reasons) >= 4
        assert any("HMA200" in reason for reason in signal.reasons)
        assert any("ADX" in reason for reason in signal.reasons)


class TestAccumulation:
    def setup_method(self):
        self.scanner = build_scanner("accumulation")

    def test_passes_volume_without_a_price_move(self):
        signal = evaluate(self.scanner, fx.uptrend_bundle(volumes=fx.volume_surge()))
        assert signal is not None
        assert signal.volume_multiple >= 1.5
        assert signal.price_change_pct <= 8.0

    def test_rejects_ordinary_volume(self):
        with pytest.raises(Rejected, match="below 1.50x"):
            check(self.scanner, fx.uptrend_bundle())

    def test_rejects_a_name_that_already_ran(self):
        """The price ceiling is what makes this scanner find something
        different from the momentum scanner rather than a slower copy."""
        closes = fx.accelerating_uptrend().copy()
        closes[-1] = closes[-2] * 1.15
        bundle = SymbolData(symbol="T",
                            daily=fx.daily_frame(closes, volumes=fx.volume_surge()))
        with pytest.raises(Rejected, match="already moved"):
            check(self.scanner, bundle)

    def test_rejects_when_volume_multiple_is_not_computable(self):
        """Zero average volume must reject, not produce `inf` (section S2)."""
        closes = fx.accelerating_uptrend()
        volumes = np.zeros(len(closes))
        volumes[-1] = 5_000_000.0
        bundle = SymbolData(symbol="T", daily=fx.daily_frame(closes, volumes=volumes))
        with pytest.raises(Rejected, match="not computable"):
            check(self.scanner, bundle)

    def test_records_volume_price_efficiency(self):
        signal = evaluate(self.scanner, fx.uptrend_bundle(volumes=fx.volume_surge()))
        assert signal.metrics["volume_price_efficiency"] is not None

    def test_quieter_price_action_scores_higher_at_equal_volume(self):
        """The claim being scored is "volume without price"."""
        quiet_closes = fx.accelerating_uptrend().copy()
        quiet_closes[-1] = quiet_closes[-2] * 1.001
        loud_closes = fx.accelerating_uptrend().copy()
        loud_closes[-1] = loud_closes[-2] * 1.07

        volumes = fx.volume_surge()
        quiet = evaluate(self.scanner, SymbolData(
            symbol="Q", daily=fx.daily_frame(quiet_closes, volumes=volumes)))
        loud = evaluate(self.scanner, SymbolData(
            symbol="L", daily=fx.daily_frame(loud_closes, volumes=volumes)))
        assert quiet is not None and loud is not None
        assert quiet.scanner_score > loud.scanner_score


class TestBreakoutReady:
    def setup_method(self):
        self.scanner = build_scanner("breakout_ready")

    def test_passes_a_name_coiled_just_under_its_20d_high(self):
        bundle = SymbolData(symbol="T", daily=fx.daily_frame(fx.coiled_under_high()))
        signal = evaluate(self.scanner, bundle)
        assert signal is not None
        assert 0 <= signal.distance_20d_high <= 5.0

    def test_rejects_a_name_far_from_its_high(self):
        closes = fx.coiled_under_high(gap_pct=20.0)
        bundle = SymbolData(symbol="T", daily=fx.daily_frame(closes))
        with pytest.raises(Rejected, match="further than"):
            check(self.scanner, bundle)

    def test_keeps_an_already_broken_out_name(self):
        """Section S3 says lower its PRIORITY, not remove it -- month 1
        needs to know how those actually perform."""
        closes = fx.accelerating_uptrend().copy()
        closes[-1] = closes[-1] * 1.02
        bundle = SymbolData(symbol="T", daily=fx.daily_frame(closes))
        signal = evaluate(self.scanner, bundle)
        assert signal is not None
        assert signal.distance_20d_high < 0
        assert signal.metrics["already_above_20d_high"] is True

    def test_an_already_broken_out_name_scores_below_a_coiled_one(self):
        coiled = evaluate(self.scanner, SymbolData(
            symbol="C", daily=fx.daily_frame(fx.coiled_under_high(gap_pct=1.0))))

        blown = fx.accelerating_uptrend().copy()
        blown[-1] = blown[-1] * 1.06
        extended = evaluate(self.scanner, SymbolData(
            symbol="X", daily=fx.daily_frame(blown)))

        assert coiled is not None and extended is not None
        assert extended.scanner_score < coiled.scanner_score

    def test_records_all_three_distances(self):
        bundle = SymbolData(symbol="T", daily=fx.daily_frame(fx.coiled_under_high()))
        signal = evaluate(self.scanner, bundle)
        assert signal.distance_20d_high is not None
        assert signal.distance_50d_high is not None
        assert signal.distance_52w_high is not None


class TestPremarketMomentumAdapter:
    def setup_method(self):
        self.scanner = build_scanner("premarket_momentum")

    def test_passes_what_the_existing_scanner_passes(self):
        signal = evaluate(self.scanner, fx.premarket_momentum_bundle())
        assert signal is not None
        assert signal.metrics["wrapped_scanner"] == "score_scanner.premarket_momentum_score"

    def test_score_is_the_existing_scanners_score_unchanged(self):
        """Not renormalised: the stored score must be the same number
        the existing scanner's own CSV shows for that symbol."""
        from score_scanner.premarket_momentum_score import evaluate_symbol

        bundle = fx.premarket_momentum_bundle()
        direct = evaluate_symbol(bundle.symbol, bundle.intraday, bundle.daily,
                                 config=self.scanner.score_scanner_config())
        signal = evaluate(self.scanner, bundle)
        assert signal.scanner_score == pytest.approx(float(direct["score"]))

    def test_signal_price_is_the_wrapped_scanners_price(self):
        """Every forward return is measured from `signal_price`, so it
        has to be the price the scanner actually judged -- not the daily
        close the framework computed."""
        from score_scanner.premarket_momentum_score import evaluate_symbol

        bundle = fx.premarket_momentum_bundle()
        direct = evaluate_symbol(bundle.symbol, bundle.intraday, bundle.daily,
                                 config=self.scanner.score_scanner_config())
        signal = evaluate(self.scanner, bundle)
        assert signal.signal_price == pytest.approx(float(direct["price"]))
        assert signal.vwap == pytest.approx(float(direct["vwap"]))
        assert signal.signal_price != pytest.approx(float(bundle.daily["Close"].iloc[-1]))

    def test_extension_is_recomputed_from_the_wrapped_price(self):
        """A signal reporting one price and an extension measured from a
        different one is internally inconsistent."""
        signal = evaluate(self.scanner, fx.premarket_momentum_bundle())
        expected = (signal.signal_price / signal.hma200 - 1.0) * 100.0
        assert signal.extension_hma200_pct == pytest.approx(expected)

    def test_rejects_a_weak_session_and_says_which_thresholds_applied(self):
        bundle = fx.premarket_momentum_bundle(gain_pct=0.5, volume_multiple=1.0)
        with pytest.raises(Rejected, match="min_score"):
            check(self.scanner, bundle)

    def test_params_reach_the_existing_scanners_own_config_object(self):
        """Section 19 without editing the existing module."""
        config = self.scanner.score_scanner_config()
        assert config.min_score == self.scanner.config.require_int("min_score")
        assert config.adx_threshold == self.scanner.config.require_float("adx_threshold")

    def test_still_records_the_common_daily_schema(self):
        """The wrapped scanner does not compute HMA or the 52-week high;
        sections 7 and 8 want them on every signal anyway."""
        signal = evaluate(self.scanner, fx.premarket_momentum_bundle())
        assert signal.hma200 is not None
        assert signal.high_52w is not None
        assert signal.extension_hma200_pct is not None


class TestGapPullback:
    def setup_method(self):
        self.scanner = build_scanner("gap_pullback")

    def test_passes_an_orderly_pullback_after_a_gap(self):
        signal = evaluate(self.scanner, fx.gap_pullback_bundle())
        assert signal is not None
        assert 2.0 <= signal.metrics["gap_pct"] <= 8.0
        assert signal.metrics["pullback_volume_ratio"] < 1.0

    def test_rejects_a_gap_that_is_too_small(self):
        with pytest.raises(Rejected, match="below the 2.00% floor"):
            check(self.scanner, fx.gap_pullback_bundle(gap_pct=0.5))

    def test_rejects_a_runaway_gap(self):
        with pytest.raises(Rejected, match="above the 8.00% ceiling"):
            check(self.scanner, fx.gap_pullback_bundle(gap_pct=15.0))

    def test_rejects_a_pullback_on_heavier_volume(self):
        """Volume drying up on the retreat is the core tell; without it
        this is a failing gap, not a pause."""
        with pytest.raises(Rejected, match="pullback volume"):
            check(self.scanner, fx.gap_pullback_bundle(
                impulse_volume=10_000.0, pullback_volume=30_000.0))

    def test_rejects_when_there_is_no_pullback_yet(self):
        with pytest.raises(Rejected, match="no pullback yet"):
            check(self.scanner, fx.gap_pullback_bundle(pullback_pct=0.05))

    def test_rejects_a_name_that_has_lost_vwap(self):
        with pytest.raises(Rejected, match="VWAP"):
            check(self.scanner, fx.gap_pullback_bundle(pullback_pct=5.0))

    def test_records_every_field_section_s5_asks_for(self):
        signal = evaluate(self.scanner, fx.gap_pullback_bundle())
        for field in ("gap_pct", "impulse_volume", "pullback_volume",
                      "pullback_volume_ratio", "vwap_distance"):
            assert signal.metrics[field] is not None, field

    def test_leg_volumes_are_per_bar_not_totals(self):
        """Sums would make the ratio a function of leg LENGTH -- a long
        light drift would beat a short heavy thrust on arithmetic alone."""
        _, context = check(self.scanner, fx.gap_pullback_bundle(
            impulse_volume=40_000.0, pullback_volume=12_000.0,
            impulse_bars=25, pullback_bars=25))
        assert context["impulse_volume"] == pytest.approx(40_000.0)
        assert context["pullback_volume"] == pytest.approx(12_000.0)
        assert context["pullback_volume_ratio"] == pytest.approx(0.3)

    def test_signal_price_is_the_intraday_price(self):
        bundle = fx.gap_pullback_bundle()
        signal = evaluate(self.scanner, bundle)
        assert signal.signal_price == pytest.approx(float(bundle.intraday["Close"].iloc[-1]))


class TestOpeningRangeBreakout:
    def setup_method(self):
        self.scanner = build_scanner("orb")

    def test_passes_a_confirmed_breakout(self):
        signal = evaluate(self.scanner, fx.orb_bundle())
        assert signal is not None
        assert signal.metrics["breakout_confirmed"] is True
        assert signal.metrics["opening_range_high"] is not None
        assert signal.metrics["opening_range_low"] is not None
        assert signal.metrics["opening_range_mid"] is not None

    def test_rejects_a_wick_only_breakout_by_default(self):
        """Section S6 singles this out: a print through the level is not
        acceptance of it."""
        with pytest.raises(Rejected, match="wick only|has not been touched|CLOSED"):
            check(self.scanner, fx.orb_bundle(confirm_close=False))

    def test_wick_only_is_reachable_when_the_flag_is_off(self):
        """The flag has to be able to change the outcome, or month 1 can
        never measure how the wick-only population resolves."""
        scanner = build_scanner("orb")
        scanner.config.params["require_close_breakout"] = False
        reasons, context = check(scanner, fx.orb_bundle(confirm_close=False))
        assert context["breakout_touched"] is True
        assert context["breakout_confirmed"] is False

    def test_records_a_retest(self):
        signal = evaluate(self.scanner, fx.orb_bundle(retest=True))
        assert signal is not None
        assert signal.metrics["retest_confirmed"] is True

    def test_a_retest_scores_above_an_identical_setup_without_one(self):
        with_retest = evaluate(self.scanner, fx.orb_bundle(retest=True))
        without = evaluate(self.scanner, fx.orb_bundle(retest=False))
        assert with_retest.scanner_score > without.scanner_score

    def test_rejects_without_volume_expansion(self):
        with pytest.raises(Rejected, match="volume expansion"):
            check(self.scanner, fx.orb_bundle(range_volume=30_000.0, post_volume=8_000.0))

    def test_rejects_a_name_already_far_past_the_range(self):
        with pytest.raises(Rejected, match="past the"):
            check(self.scanner, fx.orb_bundle(breakout_pct=12.0))

    def test_orb_minutes_is_configurable_and_validated(self):
        for minutes in (5, 15, 30):
            scanner = build_scanner("orb")
            scanner.config.params["orb_minutes"] = minutes
            assert scanner.orb_minutes() == minutes

    def test_an_unsupported_orb_window_is_refused_not_silently_corrected(self):
        """A silently corrected typo would mean a month of data labelled
        ORB15 collected under something else (section 11)."""
        from scanners.base.config import ScannerConfigError

        scanner = build_scanner("orb")
        scanner.config.params["orb_minutes"] = 7
        with pytest.raises(ScannerConfigError, match="not one of the supported"):
            scanner.orb_minutes()

    def test_a_different_window_changes_the_measured_range(self):
        five = build_scanner("orb")
        five.config.params["orb_minutes"] = 5
        thirty = build_scanner("orb")
        thirty.config.params["orb_minutes"] = 30

        bundle = fx.orb_bundle()
        _, five_context = check(five, bundle)
        assert five_context["opening_range_bars"] == 5
        # The 30-minute window swallows part of the breakout leg, so its
        # range high is higher than the 5-minute one's.
        _, thirty_context = check(thirty, bundle)
        assert thirty_context["opening_range_high"] > five_context["opening_range_high"]

    def test_needs_bars_after_the_range_before_it_will_judge(self):
        """An early scan has no verdict yet -- that must read as "no data
        yet", not as "no setups today"."""
        from scanners.base.models import ScannerDataError

        bundle = fx.orb_bundle(range_bars=15, post_bars=1)
        with pytest.raises(ScannerDataError, match="bars since"):
            check(self.scanner, bundle)
