"""S1 -- HMA Early Trend.

The theory
----------
The existing system is good at finding names that have already moved.
This one looks for the opposite shape: a long-term trend that has just
turned up and has not yet been chased. Price above a RISING HMA200, the
faster HMA89 above it, and a directional-strength reading (ADX) that is
both meaningful and increasing.

What is deliberately NOT filtered
---------------------------------
Section S1 warns that a name far above its HMA200 is a different animal
from one that just crossed, and then says not to cut hard on it in v1.0.
So extension is measured, recorded on every signal (section 8), and fed
into the SCORE -- a stretched name scores lower -- but it never rejects.
The reason is that a hard extension cap chosen today would decide the
answer to one of month one's actual questions ("do high-extension names
underperform?") by making sure no high-extension names are ever
recorded. Scoring biases the ranking without truncating the dataset.

`hma89_cross_hma200_recent` is recorded for the same reason: section S1
asks for it as a field, not as a condition.
"""

from typing import Any, Dict, List

from scanners.base.market_data_provider import SymbolData
from scanners.base.scanner_base import BaseScanner, fmt, require
from scanners.base.features import SymbolFeatures


class HmaEarlyTrendScanner(BaseScanner):
    scanner_dir = "hma_early_trend"
    scanner_name = "hma_early_trend"
    requires_intraday = False

    def check(self, features: SymbolFeatures, data: SymbolData,
              context: Dict[str, Any]) -> List[str]:
        config = self.config
        reasons: List[str] = []

        price, hma200, hma89 = features.price, features.hma200, features.hma89
        require(price is not None and hma200 is not None,
                "price or HMA200 not computable")
        require(price > hma200,
                f"price {fmt(price)} at/below HMA200 {fmt(hma200)}")
        reasons.append(f"price {fmt(price)} above HMA200 {fmt(hma200)}")

        if config.require_bool("require_hma200_rising"):
            slope = features.hma200_slope
            floor = config.require_float("hma200_slope_min_pct")
            require(slope is not None, "HMA200 slope not computable")
            require(slope > floor,
                    f"HMA200 slope {fmt(slope, 3)}% not above {fmt(floor, 3)}%")
            reasons.append(f"HMA200 rising ({fmt(slope, 3)}% over slope window)")

        if config.require_bool("require_hma89_above_hma200"):
            require(hma89 is not None, "HMA89 not computable")
            require(hma89 > hma200,
                    f"HMA89 {fmt(hma89)} at/below HMA200 {fmt(hma200)}")
            reasons.append(f"HMA89 {fmt(hma89)} above HMA200 {fmt(hma200)}")

        adx = features.adx
        adx_min = config.require_float("adx_min")
        require(adx is not None, "ADX not computable")
        require(adx > adx_min, f"ADX {fmt(adx)} not above {fmt(adx_min)}")
        reasons.append(f"ADX {fmt(adx)} above {fmt(adx_min)}")

        if config.require_bool("require_adx_rising"):
            # `adx_rising` is None when only one ADX value exists. None
            # must reject: an unknown is not a satisfied condition.
            require(features.adx_rising is True,
                    f"ADX not rising (now {fmt(adx)}, prev {fmt(features.adx_previous)})")
            reasons.append(f"ADX rising ({fmt(features.adx_previous)} -> {fmt(adx)})")

        if features.hma89_cross_hma200_recent:
            reasons.append(
                f"HMA89 crossed HMA200 {features.bars_since_hma_cross} bars ago")
        return reasons

    def score(self, features: SymbolFeatures, data: SymbolData,
              context: Dict[str, Any]) -> float:
        config = self.config
        adx_min = config.require_float("adx_min")
        adx_strong = config.require_float("score_adx_strong")
        slope_strong = config.require_float("score_slope_strong_pct")
        ideal_max = config.require_float("score_extension_ideal_max_pct")
        zero_at = config.require_float("score_extension_zero_pct")

        adx_part = _ramp(features.adx, adx_min, adx_strong) * config.require_float("score_weight_adx")
        slope_part = _ramp(features.hma200_slope, 0.0, slope_strong) * config.require_float("score_weight_slope")

        # Earliness: full credit up to `ideal_max` above HMA200, then
        # decaying to zero at `zero_at`. This is the "early, not chased"
        # preference expressed as a ranking, not a filter.
        earliness = _decay(features.extension_hma200_pct, ideal_max, zero_at)
        earliness_part = earliness * config.require_float("score_weight_earliness")

        cross_part = (config.require_float("score_weight_cross_recent")
                      if features.hma89_cross_hma200_recent else 0.0)
        return adx_part + slope_part + earliness_part + cross_part

    def extra_metrics(self, features: SymbolFeatures, data: SymbolData,
                      context: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "hma200_rising": (None if features.hma200_slope is None
                              else bool(features.hma200_slope > 0)),
            "adx_min_applied": self.config.require_float("adx_min"),
        }


def _ramp(value, low, high) -> float:
    """0 at or below `low`, 1 at or above `high`, linear between.

    None scores 0 rather than raising: a signal only reaches scoring
    after `check()` passed, so a None here is a value the scanner did
    not require, and awarding it nothing is the honest treatment.
    """
    if value is None or high <= low:
        return 0.0
    return max(0.0, min(1.0, (float(value) - low) / (high - low)))


def _decay(value, full_until, zero_at) -> float:
    """1 up to `full_until`, falling linearly to 0 at `zero_at`."""
    if value is None or zero_at <= full_until:
        return 0.0
    number = float(value)
    if number <= full_until:
        return 1.0
    return max(0.0, min(1.0, (zero_at - number) / (zero_at - full_until)))
