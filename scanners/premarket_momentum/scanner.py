"""S4 -- Premarket Momentum, as an adapter over the existing scanner.

Section S4 and section 24 both say the same thing: the premarket/score
scanner already exists, do not remove it, do not reimplement it, connect
it to the new framework as a wrapper. That is exactly what this is.

Where the decision is actually made
-----------------------------------
`score_scanner.premarket_momentum_score.evaluate_symbol` -- untouched.
Every threshold, every required check, the 50/+20/+10/+10 scoring, the
`reason` string: all of it still lives in that module and still behaves
identically when `python score_scanner/premarket_momentum_score.py` is
run directly. This file contributes no strategy of its own. It:

  1. builds that module's own `ScoreScannerConfig` from `config.json`
     (so section 19's "parameters in a file" holds for this scanner too,
     without editing the existing module),
  2. hands it the bars the framework already fetched,
  3. translates its result dict into the common `ScannerSignal` schema.

Why an adapter rather than a rewrite
------------------------------------
This scanner's job in the experiment is to be the CONTROL. Sections S4
and 14 keep it alongside the three early-trend scanners precisely so
month one can answer "is finding it early actually better than buying
today's strength?" -- and that comparison is only worth anything if the
momentum arm is the system's real, already-running momentum logic rather
than a fresh reimplementation of it that might be subtly different.

The result is that this adapter can never diverge from the production
premarket scanner: there is no second copy of the logic to drift.

Data note
---------
The existing `evaluate_symbol` reads the LAST row of the intraday frame,
so what it judges depends on when it is called -- premarket bars before
the open, regular-session bars after it. That is its existing behaviour
and is preserved. The runner schedules this scanner premarket (see
`scanners/runner.py` and section F of the delivery notes) for the same
reason cron already runs the score scanner then.
"""

from typing import Any, Dict, List

from score_scanner.premarket_momentum_score import (  # reused, never modified
    ScoreScannerConfig,
    evaluate_symbol,
)
from scanners.base import indicators as ind
from scanners.base.features import SymbolFeatures
from scanners.base.market_data_provider import SymbolData
from scanners.base.models import ScannerDataError
from scanners.base.scanner_base import BaseScanner, Rejected, fmt

#: Keys of the existing scanner's result dict that the common schema
#: already has a column for. Everything else it returns is carried into
#: `metrics` so nothing that module measured is lost.
_SCHEMA_MAPPED = {
    "timestamp", "symbol", "price", "vwap", "ema9", "ema21", "volume",
    "avg_volume", "volume_multiple", "premarket_gain_pct", "adx", "score", "reason",
}


class PremarketMomentumScanner(BaseScanner):
    scanner_dir = "premarket_momentum"
    scanner_name = "premarket_momentum"
    #: The wrapped scanner reads intraday bars for VWAP/EMA/volume and
    #: cannot produce anything without them.
    requires_intraday = True
    #: Its verdict is an intraday one: price, VWAP and EMAs all come
    #: from minute bars, so `data_timestamp` must report the newest
    #: MINUTE bar, not the newest daily bar the feature pass also read.
    source_timeframe = "1m"

    def score_scanner_config(self) -> ScoreScannerConfig:
        """Build the wrapped module's own config object from ours.

        Note `week52_high_proximity_ratio` -> `near_52w_ratio`. The
        wrapped module's field name stays exactly as it is (section 1
        forbids editing it), but this config file uses the clearer name,
        because the value it carries is otherwise unreadable: `0.98`
        says nothing on its own, while "the 52-week-high proximity
        ratio" says price must reach 98% of the 52-week high.
        """
        config = self.config
        return ScoreScannerConfig(
            min_score=config.require_int("min_score"),
            min_premarket_gain_pct=config.require_float("min_premarket_gain_pct"),
            min_volume_multiple=config.require_float("min_volume_multiple"),
            adx_threshold=config.require_float("adx_threshold"),
            near_52w_ratio=config.require_float("week52_high_proximity_ratio"),
            avg_volume_window=config.require_int("avg_volume_window"),
        )

    def check(self, features: SymbolFeatures, data: SymbolData,
              context: Dict[str, Any]) -> List[str]:
        intraday = data.intraday
        daily = data.daily
        if intraday is None or len(intraday) == 0:
            raise ScannerDataError(f"{data.symbol}: no intraday bars for premarket momentum")
        if daily is None or len(daily) == 0:
            raise ScannerDataError(f"{data.symbol}: no daily bars for premarket momentum")

        result = evaluate_symbol(
            data.symbol, intraday, daily,
            config=self.score_scanner_config(),
        )
        if result is None:
            # The existing scanner returns None for both "a required
            # check failed" and "score below min_score", and does not
            # distinguish them in its return value. Reporting the
            # thresholds in force is the most specific reason available
            # without changing that module -- which section 1 forbids.
            raise Rejected(
                "existing score scanner returned no candidate "
                f"(min_score={self.config.require_int('min_score')}, "
                f"min_gain={fmt(self.config.require_float('min_premarket_gain_pct'))}%, "
                f"min_volume={fmt(self.config.require_float('min_volume_multiple'))}x)")

        context["result"] = result
        reasons = [f"score_scanner: {result.get('reason', '')}".strip()]
        for label, key in (("premarket gain", "premarket_gain_pct"),
                           ("volume", "volume_multiple"),
                           ("ADX", "adx")):
            value = result.get(key)
            if value is not None:
                suffix = "%" if key.endswith("_pct") else ("x" if key == "volume_multiple" else "")
                reasons.append(f"{label} {fmt(value)}{suffix}")
        if result.get("break_prev_high"):
            reasons.append(f"broke previous high {fmt(result.get('prev_high'))}")
        if result.get("near_or_break_52w_high"):
            # Section 9: spell the threshold out. "0.98" in a reason
            # string is a number a reader has to go and look up; "52W
            # high proximity: 98% (threshold 98%)" is a sentence.
            ratio = self.config.require_float("week52_high_proximity_ratio")
            high52 = result.get("week52_high")
            price = result.get("price")
            achieved = (price / high52 * 100.0) if high52 else None
            reasons.append(
                f"52W high proximity: {fmt(achieved, 1)}% of {fmt(high52)} "
                f"(threshold {fmt(ratio * 100, 0)}%)")
        return [reason for reason in reasons if reason]

    def score(self, features: SymbolFeatures, data: SymbolData,
              context: Dict[str, Any]) -> float:
        """The existing scanner's own score, passed through unchanged.

        Not renormalised. Section 9 says scanner scores are not
        comparable across scanners anyway, so rescaling this one to
        "match" the others would buy nothing and would break the one
        property worth having: that a `premarket_momentum` score in the
        analytics store is the same number the existing scanner's own
        CSV shows for that symbol.
        """
        result = context.get("result") or {}
        value = result.get("score")
        return float(value) if value is not None else 0.0

    def extra_metrics(self, features: SymbolFeatures, data: SymbolData,
                      context: Dict[str, Any]) -> Dict[str, Any]:
        result = context.get("result") or {}
        metrics: Dict[str, Any] = {
            key: value for key, value in result.items() if key not in _SCHEMA_MAPPED
        }
        metrics["wrapped_scanner"] = "score_scanner.premarket_momentum_score"
        metrics["score_scanner_score"] = result.get("score")
        return metrics

    def override_schema_fields(self, features: SymbolFeatures, data: SymbolData,
                               context: Dict[str, Any]) -> Dict[str, Any]:
        """Take the wrapped scanner's own price, VWAP, EMAs and volume.

        `SymbolFeatures` measured the latest DAILY close and a session
        VWAP the framework computed itself. The wrapped scanner measured
        price, VWAP, EMA9, EMA21 and volume from the intraday frame at
        the instant it made its decision, and those are the numbers the
        decision rests on.

        `signal_price` is the one that really matters: every forward
        return, MFE and MAE in sections 12 and 13 is measured from it,
        so it has to be the price this scanner actually saw. Recording a
        daily close here would produce a premarket signal whose measured
        return starts from a price that did not exist at signal time --
        and the resulting month-end comparison against the other five
        scanners would be measuring the gap between two data sources
        rather than the scanners.

        The HMA levels and the 20/50/252-day highs stay as the framework
        computed them -- they are properties of the daily history, not
        of the moment. But everything derived from PRICE against those
        levels is recomputed from the intraday price, because a signal
        that reports one price and an extension measured from a
        different one is internally inconsistent, and section 8's
        extension fields are among the variables section 22 expects to
        be compared across scanners.
        """
        result = context.get("result") or {}
        price = result.get("price")
        if price is None:
            # Nothing to substitute; keep the framework's whole pass
            # rather than producing a half-overridden signal.
            return {}
        return {
            "signal_price": price,
            "vwap": result.get("vwap"),
            "ema9": result.get("ema9"),
            "ema21": result.get("ema21"),
            "volume": result.get("volume"),
            "avg_volume": result.get("avg_volume"),
            "volume_multiple": result.get("volume_multiple"),
            "premarket_gain_pct": result.get("premarket_gain_pct"),
            "adx": result.get("adx"),
            "distance_20d_high": ind.distance_pct(price, features.high_20d),
            "distance_50d_high": ind.distance_pct(price, features.high_50d),
            "distance_52w_high": ind.distance_pct(price, features.high_52w),
            "extension_hma89_pct": ind.extension_pct(price, features.hma89),
            "extension_hma200_pct": ind.extension_pct(price, features.hma200),
            "extension_vwap_pct": ind.extension_pct(price, result.get("vwap")),
        }
