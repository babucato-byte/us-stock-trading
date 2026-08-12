"""S5 -- Gap Pullback.

The theory
----------
Section S5 is explicit that the target is not the gap itself. Buying a
gap up is chasing; what this looks for is a gap that has since pulled
back in an orderly way -- shallow, on less volume than the move that
created it, still holding VWAP. That combination is the difference
between a pause and a failure, and it is the only thing separating this
scanner from "gap scanner with extra steps".

The impulse / pullback split
----------------------------
The session is cut at the bar that made the session high:

    open ----------- HIGH ----------- now
    |<-- impulse -->|<-- pullback -->|

`impulse_volume` and `pullback_volume` are the MEAN volume per bar over
each leg, not the sum. Sums would make the ratio a function of how long
each leg happens to be -- a two-hour drift on light volume would "beat"
a ten-minute thrust on heavy volume purely by having more bars. Per-bar
means compare the intensity of the two legs, which is what "pullback
volume lower than impulse volume" actually claims. Both sums are
recorded as well, so the choice can be revisited from month-1 data
rather than re-run.

Ordering of the checks
----------------------
Gap first, and cheaply, because on a typical day fewer than one name in
fifty gaps 2-8% and every check after that is wasted work on the other
forty-nine. The reason string is also more useful this way: "gap 0.4%
below the 2% floor" says more about why a name was skipped than a VWAP
condition would.

v1.0 scope
----------
Section S5 says this one produces candidates rather than orders in its
first version. It does: like the other five it writes to the analytics
store only, and nothing in this package can reach an order path.
"""

from typing import Any, Dict, List, Optional

import pandas as pd

from scanners.base import indicators as ind
from scanners.base import session as sess
from scanners.base.features import SymbolFeatures
from scanners.base.market_data_provider import SymbolData
from scanners.base.models import ScannerDataError
from scanners.base.scanner_base import BaseScanner, fmt, require


class GapPullbackScanner(BaseScanner):
    scanner_dir = "gap_pullback"
    scanner_name = "gap_pullback"
    requires_intraday = True
    #: Its verdict is an intraday one: price, VWAP and EMAs all come
    #: from minute bars, so `data_timestamp` must report the newest
    #: MINUTE bar, not the newest daily bar the feature pass also read.
    source_timeframe = "1m"

    def check(self, features: SymbolFeatures, data: SymbolData,
              context: Dict[str, Any]) -> List[str]:
        config = self.config
        reasons: List[str] = []

        session = sess.slice_session(
            data.intraday,
            regular_only=config.require_bool("regular_session_only"),
        )
        minimum_bars = config.require_int("min_session_bars")
        if session is None or len(session) < minimum_bars:
            raise ScannerDataError(
                f"{data.symbol}: {0 if session is None else len(session)} regular-session "
                f"bars, need {minimum_bars}")

        session_date = sess.latest_session_date(data.intraday)
        previous_close = sess.previous_daily_close(data.daily, before=session_date)
        if previous_close is None:
            previous_close = features.previous_close
        require(previous_close not in (None, 0),
                "no prior daily close to measure the gap from")

        opens = ind.open_series(session).dropna()
        session_open = ind.to_float(opens.iloc[0]) if len(opens) else None
        if session_open is None:
            closes = ind.close_series(session).dropna()
            session_open = ind.to_float(closes.iloc[0]) if len(closes) else None
        require(session_open is not None, "no usable session open")

        gap_pct = (session_open / previous_close - 1.0) * 100.0
        gap_min = config.require_float("gap_min_pct")
        gap_max = config.require_float("gap_max_pct")
        require(gap_pct >= gap_min,
                f"gap {fmt(gap_pct)}% below the {fmt(gap_min)}% floor")
        require(gap_pct <= gap_max,
                f"gap {fmt(gap_pct)}% above the {fmt(gap_max)}% ceiling")
        reasons.append(f"gap {fmt(gap_pct)}% (open {fmt(session_open)} "
                       f"vs prior close {fmt(previous_close)})")

        highs = ind.high_series(session).dropna()
        closes = ind.close_series(session).dropna()
        require(len(highs) > 0 and len(closes) > 0, "session bars have no usable prices")
        price = ind.to_float(closes.iloc[-1])
        require(price is not None and price > 0, "no usable current price")

        if config.require_bool("require_price_above_hma200"):
            require(features.hma200 is not None, "HMA200 not computable")
            require(price > features.hma200,
                    f"price {fmt(price)} at/below HMA200 {fmt(features.hma200)}")
            reasons.append(f"price {fmt(price)} above HMA200 {fmt(features.hma200)}")

        session_high = ind.to_float(highs.max())
        high_position = int(highs.values.argmax())
        pullback_from_high_pct = (session_high - price) / session_high * 100.0

        minimum_pullback = config.require_float("min_pullback_from_high_pct")
        maximum_pullback = config.require_float("max_pullback_from_high_pct")
        require(pullback_from_high_pct >= minimum_pullback,
                f"only {fmt(pullback_from_high_pct)}% off the session high "
                f"{fmt(session_high)}; no pullback yet "
                f"(need {fmt(minimum_pullback)}%)")
        require(pullback_from_high_pct <= maximum_pullback,
                f"{fmt(pullback_from_high_pct)}% off the session high {fmt(session_high)} "
                f"is a failed gap, not a pullback (limit {fmt(maximum_pullback)}%)")
        reasons.append(f"pulled back {fmt(pullback_from_high_pct)}% from session high "
                       f"{fmt(session_high)}")

        impulse_volume, pullback_volume, impulse_sum, pullback_sum = _leg_volumes(
            session, high_position)
        require(impulse_volume is not None and impulse_volume > 0,
                "impulse leg has no volume to compare against")
        require(pullback_volume is not None,
                "pullback leg has no bars yet (session high is the latest bar)")
        volume_ratio = ind.safe_ratio(pullback_volume, impulse_volume)
        ratio_limit = config.require_float("max_pullback_to_impulse_volume_ratio")
        require(volume_ratio is not None, "pullback/impulse volume ratio not computable")
        require(volume_ratio < ratio_limit,
                f"pullback volume {fmt(volume_ratio)}x the impulse leg; "
                f"needs to be under {fmt(ratio_limit)}x")
        reasons.append(f"pullback volume {fmt(volume_ratio)}x the impulse leg "
                       f"(per-bar mean {fmt(pullback_volume, 0)} vs {fmt(impulse_volume, 0)})")

        vwap = ind.last_valid(ind.session_vwap(session))
        require(vwap not in (None, 0), "session VWAP not computable")
        vwap_distance = (price / vwap - 1.0) * 100.0
        tolerance = config.require_float("vwap_tolerance_pct")
        # Section S5: "above VWAP, or holding near it". A name that has
        # given up VWAP by more than the tolerance is no longer pulling
        # back into support -- it is unwinding the gap.
        require(vwap_distance >= -tolerance,
                f"price {fmt(vwap_distance)}% vs VWAP {fmt(vwap)}, "
                f"below the -{fmt(tolerance)}% tolerance")
        if vwap_distance >= 0:
            reasons.append(f"holding above VWAP ({fmt(vwap_distance)}%)")
        else:
            reasons.append(f"within {fmt(tolerance)}% of VWAP ({fmt(vwap_distance)}%)")

        context.update({
            "gap_pct": gap_pct,
            "session_open": session_open,
            "previous_close": previous_close,
            "price": price,
            "session_high": session_high,
            "pullback_from_high_pct": pullback_from_high_pct,
            "impulse_volume": impulse_volume,
            "pullback_volume": pullback_volume,
            "impulse_volume_sum": impulse_sum,
            "pullback_volume_sum": pullback_sum,
            "pullback_volume_ratio": volume_ratio,
            "vwap": vwap,
            "vwap_distance": vwap_distance,
            "session_bars": len(session),
            "impulse_bars": high_position + 1,
            "pullback_bars": len(session) - high_position - 1,
        })
        return reasons

    def score(self, features: SymbolFeatures, data: SymbolData,
              context: Dict[str, Any]) -> float:
        config = self.config

        # A shallow, controlled pullback scores best; a deep one is
        # closer to a failed gap even when it is inside the limit.
        depth_part = _decay(
            context.get("pullback_from_high_pct"),
            config.require_float("score_ideal_pullback_pct"),
            config.require_float("score_pullback_zero_pct"),
        ) * config.require_float("score_weight_pullback_depth")

        # Volume drying up on the pullback is the core tell. Full credit
        # at or below the ideal ratio, decaying to nothing at the limit
        # where the check itself would have rejected.
        dryup_part = _decay(
            context.get("pullback_volume_ratio"),
            config.require_float("score_pullback_to_impulse_volume_ideal"),
            config.require_float("max_pullback_to_impulse_volume_ratio"),
        ) * config.require_float("score_weight_volume_dryup")

        # Comfortably above VWAP beats scraping the tolerance band.
        tolerance = config.require_float("vwap_tolerance_pct")
        vwap_part = _ramp(
            context.get("vwap_distance"), -tolerance, tolerance,
        ) * config.require_float("score_weight_vwap_hold")

        # Mid-band gaps are the cleanest: near the floor is barely a gap,
        # near the ceiling is the runaway move S5 is trying to avoid.
        gap_part = _band(
            context.get("gap_pct"),
            config.require_float("gap_min_pct"),
            config.require_float("gap_max_pct"),
        ) * config.require_float("score_weight_gap_quality")

        return depth_part + dryup_part + vwap_part + gap_part

    def extra_metrics(self, features: SymbolFeatures, data: SymbolData,
                      context: Dict[str, Any]) -> Dict[str, Any]:
        return {key: context.get(key) for key in (
            "gap_pct", "session_open", "session_high", "pullback_from_high_pct",
            "impulse_volume", "pullback_volume", "impulse_volume_sum",
            "pullback_volume_sum", "pullback_volume_ratio", "vwap_distance",
            "session_bars", "impulse_bars", "pullback_bars",
        )}

    def override_schema_fields(self, features: SymbolFeatures, data: SymbolData,
                               context: Dict[str, Any]) -> Dict[str, Any]:
        """Use the session's price and VWAP, not the daily close.

        This scanner judged an intraday moment. Recording the daily
        close as `signal_price` would anchor its forward returns to a
        price it never saw -- and for a gap-pullback signal taken at
        11:00, the daily "close" during a live scan is simply the last
        print, which may be several percent away by 16:00.
        """
        price = context.get("price")
        if price is None:
            return {}
        vwap = context.get("vwap")
        return {
            "signal_price": price,
            "vwap": vwap,
            "distance_20d_high": ind.distance_pct(price, features.high_20d),
            "distance_50d_high": ind.distance_pct(price, features.high_50d),
            "distance_52w_high": ind.distance_pct(price, features.high_52w),
            "extension_hma89_pct": ind.extension_pct(price, features.hma89),
            "extension_hma200_pct": ind.extension_pct(price, features.hma200),
            "extension_vwap_pct": ind.extension_pct(price, vwap),
        }


def _leg_volumes(session, high_position: int):
    """Mean and total volume for the impulse and pullback legs.

    The high bar itself belongs to the IMPULSE -- it is the last bar of
    the move up, not the first of the retreat. Putting it in the
    pullback leg would import the heaviest bar of the session into the
    denominator's counterpart and flatter every ratio.

    Returns (impulse_mean, pullback_mean, impulse_sum, pullback_sum),
    with the pullback pair None when the high is the most recent bar --
    there is no pullback yet, which the caller rejects with that reason.
    """
    volumes = ind.volume_series(session)
    if volumes is None or len(volumes) == 0:
        return None, None, None, None
    impulse = volumes.iloc[: high_position + 1].dropna()
    pullback = volumes.iloc[high_position + 1:].dropna()
    impulse_mean = ind.to_float(impulse.mean()) if len(impulse) else None
    impulse_sum = ind.to_float(impulse.sum()) if len(impulse) else None
    if len(pullback) == 0:
        return impulse_mean, None, impulse_sum, None
    return (impulse_mean, ind.to_float(pullback.mean()),
            impulse_sum, ind.to_float(pullback.sum()))


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


def _band(value, low, high) -> float:
    """1.0 at the midpoint of [low, high], falling to 0 at either edge."""
    if value is None or high <= low:
        return 0.0
    number = float(value)
    if number <= low or number >= high:
        return 0.0
    middle = (low + high) / 2.0
    half_width = (high - low) / 2.0
    return max(0.0, 1.0 - abs(number - middle) / half_width)
