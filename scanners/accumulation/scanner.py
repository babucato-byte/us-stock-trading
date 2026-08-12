"""S2 -- Volume Accumulation.

The theory
----------
Someone building a position leaves a trace in volume before it shows in
price. So: unusual volume, on a day the price did NOT already run, in a
name whose long trend is up. The `price_change_max_pct` ceiling is the
load-bearing condition -- it is what makes this scanner find something
different from the momentum scanner rather than a slower copy of it.

volume_price_efficiency
-----------------------
Section S2 asks for a measure of volume expansion relative to price
movement, and says to treat it as an analysis field rather than a filter
in v1.0. It is computed in `indicators.volume_price_efficiency` (with
the divide-by-zero guard section S2 calls out), recorded on every
signal, and used in the SCORE -- so it ranks candidates without deciding
which ones exist.

What is NOT applied
-------------------
No lower bound on `price_change_pct`. A name down 6% on 3x volume is
either distribution or a shakeout, and which one it is on average is
exactly the sort of thing month 1 exists to measure. A floor picked
today would guarantee no such rows are ever recorded, and the question
would be unanswerable in month 2.
"""

from typing import Any, Dict, List

from scanners.base.features import SymbolFeatures
from scanners.base.market_data_provider import SymbolData
from scanners.base.scanner_base import BaseScanner, fmt, require


class AccumulationScanner(BaseScanner):
    scanner_dir = "accumulation"
    scanner_name = "accumulation"
    requires_intraday = False

    def check(self, features: SymbolFeatures, data: SymbolData,
              context: Dict[str, Any]) -> List[str]:
        config = self.config
        reasons: List[str] = []

        multiple = features.volume_multiple
        minimum = config.require_float("volume_multiple_min")
        # None here means avg_volume was zero or the 20-bar window was
        # not full -- a halted or freshly-listed name. Rejecting on the
        # None keeps `inf` out of the dataset (section S2's explicit
        # divide-by-zero requirement).
        require(multiple is not None,
                f"volume multiple not computable (volume={fmt(features.volume, 0)}, "
                f"avg={fmt(features.avg_volume, 0)})")
        require(multiple >= minimum,
                f"volume {fmt(multiple)}x below {fmt(minimum)}x")
        reasons.append(f"volume {fmt(multiple)}x average")

        change = features.price_change_pct
        ceiling = config.require_float("price_change_max_pct")
        require(change is not None, "price change not computable")
        require(change <= ceiling,
                f"price already moved {fmt(change)}%, above the {fmt(ceiling)}% ceiling")
        reasons.append(f"price change {fmt(change)}% within {fmt(ceiling)}% ceiling")

        if config.require_bool("require_price_above_hma200"):
            price, hma200 = features.price, features.hma200
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
            reasons.append(f"HMA200 rising ({fmt(slope, 3)}%)")

        if features.volume_price_efficiency is not None:
            reasons.append(
                f"volume/price efficiency {fmt(features.volume_price_efficiency)}")
        return reasons

    def score(self, features: SymbolFeatures, data: SymbolData,
              context: Dict[str, Any]) -> float:
        config = self.config
        volume_part = _ramp(
            features.volume_multiple,
            config.require_float("volume_multiple_min"),
            config.require_float("score_volume_multiple_strong"),
        ) * config.require_float("score_weight_volume")

        # Quietness: the closer the price move is to flat, the more the
        # volume looks like accumulation rather than a chase. Measured
        # on the absolute move, so a quiet down day and a quiet up day
        # rank alike -- the claim being scored is "volume without price",
        # which has no direction.
        quiet_part = _decay(
            abs(features.price_change_pct) if features.price_change_pct is not None else None,
            config.require_float("score_quiet_change_pct"),
            config.require_float("price_change_max_pct"),
        ) * config.require_float("score_weight_quietness")

        efficiency_part = _ramp(
            features.volume_price_efficiency, 0.0,
            config.require_float("score_efficiency_strong"),
        ) * config.require_float("score_weight_efficiency")

        trend_part = _ramp(
            features.hma200_slope, 0.0, config.require_float("score_slope_strong_pct"),
        ) * config.require_float("score_weight_trend")

        return volume_part + quiet_part + efficiency_part + trend_part

    def extra_metrics(self, features: SymbolFeatures, data: SymbolData,
                      context: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "volume_multiple_min_applied": self.config.require_float("volume_multiple_min"),
            "price_change_max_pct_applied": self.config.require_float("price_change_max_pct"),
            "abs_price_change_pct": (None if features.price_change_pct is None
                                     else abs(features.price_change_pct)),
        }


def _ramp(value, low, high) -> float:
    if value is None or high <= low:
        return 0.0
    return max(0.0, min(1.0, (float(value) - low) / (high - low)))


def _decay(value, full_until, zero_at) -> float:
    if value is None or zero_at <= full_until:
        return 0.0
    number = float(value)
    if number <= full_until:
        return 1.0
    return max(0.0, min(1.0, (zero_at - number) / (zero_at - full_until)))
