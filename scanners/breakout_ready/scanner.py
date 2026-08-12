"""S3 -- Breakout Ready.

The theory
----------
Coiled under resistance, in an uptrend, with directional strength -- but
NOT already through. The distinction from a breakout scanner is the
whole point, and it lives in `distance_20d_high`.

Sign convention, and why it decides this scanner's behaviour
------------------------------------------------------------
`indicators.distance_pct` is positive when price is BELOW the level. So:

    +3.1%  ->  3.1% under the 20-day high; this is the target shape
     0.0%  ->  exactly at it
    -4.0%  ->  already 4% ABOVE it; it has broken out

The `distance_20d_high_max_pct` ceiling of 5% therefore does what
section S3 asks on one side -- a name 12% below its high is too far away
and is excluded -- but it does NOT exclude the negative side, because a
broken-out name has a distance of -4%, which is comfortably under 5%.

That is intentional and matches the spec exactly: section S3 says a name
that has already broken out sharply serves a different purpose and
should be given LOWER PRIORITY, not removed. So it stays in the dataset
(month 1 needs to know how those actually perform) and the score
penalises it via `already_broken_out_penalty_pct`, past which proximity
credit decays to nothing.

The 20-day high excludes today's bar
------------------------------------
`rolling_high(..., exclude_current=True)`. Without that, every name
printing a new high today would show `distance_20d_high == 0` -- a
perfect proximity score for precisely the already-extended names this
scanner exists to rank below the coiled ones.
"""

from typing import Any, Dict, List

from scanners.base.features import SymbolFeatures
from scanners.base.market_data_provider import SymbolData
from scanners.base.scanner_base import BaseScanner, fmt, require


class BreakoutReadyScanner(BaseScanner):
    scanner_dir = "breakout_ready"
    scanner_name = "breakout_ready"
    requires_intraday = False

    def check(self, features: SymbolFeatures, data: SymbolData,
              context: Dict[str, Any]) -> List[str]:
        config = self.config
        reasons: List[str] = []

        price, hma200, hma89 = features.price, features.hma200, features.hma89

        if config.require_bool("require_price_above_hma200"):
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

        if config.require_bool("require_hma89_above_hma200"):
            require(hma89 is not None and hma200 is not None, "HMA89/HMA200 not computable")
            require(hma89 > hma200,
                    f"HMA89 {fmt(hma89)} at/below HMA200 {fmt(hma200)}")
            reasons.append(f"HMA89 above HMA200")

        distance = features.distance_20d_high
        ceiling = config.require_float("distance_20d_high_max_pct")
        require(distance is not None,
                "20-day high not computable (insufficient daily bars)")
        require(distance <= ceiling,
                f"{fmt(distance)}% below the 20d high {fmt(features.high_20d)}, "
                f"further than the {fmt(ceiling)}% limit")
        if distance >= 0:
            reasons.append(f"{fmt(distance)}% below 20d high {fmt(features.high_20d)}")
        else:
            reasons.append(
                f"already {fmt(-distance)}% above the 20d high {fmt(features.high_20d)} "
                "(kept, scored down per S3)")

        adx = features.adx
        adx_min = config.require_float("adx_min")
        require(adx is not None, "ADX not computable")
        require(adx > adx_min, f"ADX {fmt(adx)} not above {fmt(adx_min)}")
        reasons.append(f"ADX {fmt(adx)} above {fmt(adx_min)}")

        if features.distance_50d_high is not None:
            reasons.append(f"{fmt(features.distance_50d_high)}% below 50d high")
        if features.distance_52w_high is not None:
            reasons.append(f"{fmt(features.distance_52w_high)}% below 52w high")
        return reasons

    def score(self, features: SymbolFeatures, data: SymbolData,
              context: Dict[str, Any]) -> float:
        config = self.config
        proximity = _proximity(
            features.distance_20d_high,
            ceiling=config.require_float("distance_20d_high_max_pct"),
            overshoot_zero_at=config.require_float("already_broken_out_penalty_pct"),
        ) * config.require_float("score_weight_proximity")

        adx_part = _ramp(
            features.adx,
            config.require_float("adx_min"),
            config.require_float("score_adx_strong"),
        ) * config.require_float("score_weight_adx")

        trend_part = _ramp(
            features.hma200_slope, 0.0, config.require_float("score_slope_strong_pct"),
        ) * config.require_float("score_weight_trend")

        volume_part = _ramp(
            features.volume_multiple, 1.0,
            config.require_float("score_volume_multiple_strong"),
        ) * config.require_float("score_weight_volume")

        # Near a 52-week high is a stronger setup than near a 20-day
        # high inside a long downtrend, so distance-to-52w-high adds
        # context. Decays from full credit at the high itself to zero at
        # `score_distance_52w_full_pct` below it.
        context_52w_part = _decay(
            features.distance_52w_high, 0.0,
            config.require_float("score_distance_52w_full_pct"),
        ) * config.require_float("score_weight_52w_context")

        return proximity + adx_part + trend_part + volume_part + context_52w_part

    def extra_metrics(self, features: SymbolFeatures, data: SymbolData,
                      context: Dict[str, Any]) -> Dict[str, Any]:
        distance = features.distance_20d_high
        return {
            "already_above_20d_high": (None if distance is None else bool(distance < 0)),
            "overshoot_20d_high_pct": (None if distance is None or distance >= 0
                                       else abs(distance)),
            "distance_20d_high_max_pct_applied":
                self.config.require_float("distance_20d_high_max_pct"),
        }


def _proximity(distance, *, ceiling: float, overshoot_zero_at: float) -> float:
    """Credit for being close to -- but not far past -- the 20-day high.

    Below the high: full credit at 0% away, decaying to zero at the
    `ceiling`. Above it: credit falls from full at the high itself to
    zero once the name is `overshoot_zero_at` percent past it, which is
    how section S3's "already broken out, lower priority" is expressed
    numerically. A name 8% above its 20-day high scores zero here and
    can only reach a mediocre total from the other components -- present
    in the dataset, ranked below the coiled names.
    """
    if distance is None:
        return 0.0
    number = float(distance)
    if number >= 0:
        if ceiling <= 0:
            return 0.0
        return max(0.0, min(1.0, (ceiling - number) / ceiling))
    overshoot = -number
    if overshoot_zero_at <= 0:
        return 0.0
    return max(0.0, min(1.0, (overshoot_zero_at - overshoot) / overshoot_zero_at))


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
