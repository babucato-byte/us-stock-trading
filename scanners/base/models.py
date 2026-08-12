"""The one schema every scanner writes, per spec section 7.

Why one schema for six scanners that measure different things
-------------------------------------------------------------
The point of this project is not to run six scanners. It is to be able
to ask, a month from now, "which scanner found better stocks?" -- and
that question is only answerable if all six wrote down the SAME facts
about every symbol they flagged, in the same units, at the same moment.

So `ScannerSignal` carries the full technical field set even for
scanners that did not use most of it. An ORB signal still records
`hma200` and `high_52w`; a HMA-trend signal still records `vwap`. The
field a scanner did not filter on is exactly the field the month-end
analysis needs in order to discover that it SHOULD have filtered on it.

Fields a scanner genuinely could not compute are `None`, never 0.0.
Zero is a measurement ("ADX was 0"); None is an absence ("ADX was not
computable from the bars we had"). Collapsing the two would make the
month-one dataset lie in the direction of "we measured it and it was
low", which is the worst kind of lie for a calibration dataset.

Immutability
------------
`ScannerSignal` is frozen. A signal is an observation of a moment that
has already passed -- `signal_price` in particular is the anchor every
return, MFE and MAE in section 12/13 is measured against. Nothing
downstream (the analytics store, the performance tracker, a report) has
any business editing it after the fact, and freezing removes the
possibility of a tracker quietly rewriting the price its own numbers
are computed from.
"""

import hashlib
import math
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Optional


def _clean_float(value: Any) -> Optional[float]:
    """A float, or None -- never NaN, never inf.

    pandas hands back `nan` for "not computable" all over this codebase,
    and `nan` survives a round-trip through JSON as the literal `NaN`,
    which is not valid JSON and which `json.load` in one language and
    not another will read differently. Every numeric field is funneled
    through here so a non-finite value becomes an explicit null at the
    boundary rather than a landmine in the month-end dataset.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _clean_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    return bool(value)


#: Every numeric field on ScannerSignal, so cleaning stays in one list
#: rather than being repeated per-field in __post_init__.
NUMERIC_FIELDS = (
    "scanner_score",
    "signal_price",
    "hma89",
    "hma200",
    "hma200_slope",
    "ema9",
    "ema21",
    "vwap",
    "adx",
    "volume",
    "avg_volume",
    "volume_multiple",
    "price_change_pct",
    "high_20d",
    "high_50d",
    "high_52w",
    "distance_20d_high",
    "distance_50d_high",
    "distance_52w_high",
    "premarket_gain_pct",
    "extension_hma89_pct",
    "extension_hma200_pct",
    "extension_vwap_pct",
)


@dataclass(frozen=True)
class ScannerSignal:
    """One scanner's finding for one symbol at one moment.

    Field groups match spec section 7 (identity / technical) and section
    8 (extension). `metrics` holds whatever else a particular scanner
    measured that the common schema has no column for -- gap_pct,
    opening_range_high, pullback_volume_ratio and friends. It is a
    deliberate escape hatch: forcing every scanner-specific number into
    the shared column list would make the schema unreadable, and
    dropping them would lose exactly the variables section 22 wants an
    AI to compare a month from now.
    """

    # --- identity (section 7, required) ---
    timestamp: str
    trading_day: str
    symbol: str
    scanner_name: str
    scanner_version: str
    scanner_score: Optional[float]
    signal_price: Optional[float]

    # --- provenance (section 4/5/6/19) ---
    #
    # These answer, from the stored row alone and a month later, four
    # questions that are unanswerable afterwards if they are not written
    # down now:
    #
    #   which vendor's bars was this judged from?   market_data_provider
    #   how fresh were those bars?                  data_timestamp
    #   when did we judge?                          feature_timestamp
    #   which run produced this?                    scanner_run_id
    #
    # The gap between `data_timestamp` and `feature_timestamp` is the
    # one that catches a whole class of silent problem: a scan that ran
    # at 09:50 against bars stamped 09:31 was working from stale data,
    # and without both timestamps that is invisible in the dataset.
    #
    #: Vendor that served the bars, e.g. "yfinance". Never "cached" and
    #: never a wrapper's name -- see CachingMarketDataProvider.
    market_data_provider: Optional[str] = None
    #: Upstream feed if the vendor identifies one; None when it does not.
    #: Section 4: never a guess.
    market_data_feed: Optional[str] = None
    #: Timestamp of the newest bar the decision rested on, ISO-8601 with
    #: an offset. Taken from the timeframe named by `source_timeframe`,
    #: so a daily scanner reports its newest daily bar and an intraday
    #: scanner its newest minute bar.
    data_timestamp: Optional[str] = None
    #: When feature computation for this symbol finished, UTC ISO-8601.
    feature_timestamp: Optional[str] = None
    #: Groups every signal produced by one runner invocation from one
    #: data snapshot. Section 5/22: makes same-run intersections
    #: distinguishable from same-day ones.
    scanner_run_id: Optional[str] = None
    #: The bar interval this scanner's decision rests on ("1d", "1m").
    #: Section 19: a month later, nobody remembers whether a given
    #: scanner's ADX was daily or intraday.
    source_timeframe: Optional[str] = None

    # --- technical (section 7, null allowed) ---
    hma89: Optional[float] = None
    hma200: Optional[float] = None
    hma200_slope: Optional[float] = None
    ema9: Optional[float] = None
    ema21: Optional[float] = None
    vwap: Optional[float] = None
    adx: Optional[float] = None
    volume: Optional[float] = None
    avg_volume: Optional[float] = None
    volume_multiple: Optional[float] = None
    price_change_pct: Optional[float] = None
    high_20d: Optional[float] = None
    high_50d: Optional[float] = None
    high_52w: Optional[float] = None
    distance_20d_high: Optional[float] = None
    distance_50d_high: Optional[float] = None
    distance_52w_high: Optional[float] = None
    premarket_gain_pct: Optional[float] = None

    # --- extension (section 8, always stored when computable) ---
    extension_hma89_pct: Optional[float] = None
    extension_hma200_pct: Optional[float] = None
    extension_vwap_pct: Optional[float] = None

    # --- human-readable justification (section 27) ---
    reasons: List[str] = field(default_factory=list)

    # --- scanner-specific extras (gap_pct, orb levels, ...) ---
    metrics: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # frozen dataclass: normalisation has to go through object.__setattr__.
        for name in NUMERIC_FIELDS:
            object.__setattr__(self, name, _clean_float(getattr(self, name)))
        object.__setattr__(self, "symbol", str(self.symbol).strip().upper())
        object.__setattr__(self, "reasons", list(self.reasons or []))
        object.__setattr__(self, "metrics", _clean_metrics(self.metrics))

    @property
    def signal_id(self) -> str:
        """A stable identity for this signal, derived from its content.

        The performance tracker (section 12) re-runs many times over the
        same signal -- at +30m, +1h, next day, +5d -- and each run has to
        find the row it is updating. A content hash gives every run the
        same key without needing a counter, a database sequence, or any
        shared mutable state between the scanner process and the tracker
        process that runs hours later.

        Keyed on (scanner, version, symbol, timestamp) rather than
        including the price: the same scan re-published must collide, so
        a double-run of the scheduler cannot double-count a signal in the
        month-end statistics.
        """
        raw = "|".join([
            self.scanner_name,
            self.scanner_version,
            self.symbol,
            self.trading_day,
            self.timestamp,
        ])
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]

    @property
    def reason(self) -> str:
        """The reasons joined for a log line or a CSV cell."""
        return "; ".join(self.reasons)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["signal_id"] = self.signal_id
        return payload

    def to_flat_dict(self) -> Dict[str, Any]:
        """One row, no nesting -- for CSV export (section 22).

        `metrics` is flattened with a `metric_` prefix rather than
        dropped, because those are the scanner-specific variables the
        month-end comparison actually turns on.
        """
        payload = self.to_dict()
        metrics = payload.pop("metrics", {}) or {}
        payload["reasons"] = self.reason
        for key, value in metrics.items():
            payload[f"metric_{key}"] = value
        return payload

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ScannerSignal":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in payload.items() if k in known})


def _clean_metrics(metrics: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Same NaN/inf discipline as the typed fields, applied to the
    free-form extras -- they end up in the same JSON file and the same
    month-end DataFrame, so they get the same guarantees."""
    if not metrics:
        return {}
    cleaned: Dict[str, Any] = {}
    for key, value in metrics.items():
        if isinstance(value, bool) or value is None or isinstance(value, str):
            cleaned[str(key)] = value
        elif isinstance(value, (int, float)):
            cleaned[str(key)] = _clean_float(value)
        else:
            cleaned[str(key)] = value
    return cleaned


@dataclass(frozen=True)
class ScannerRejection:
    """Why a symbol did NOT become a signal.

    Section 27 says not to store a bare PASS/FAIL, and that applies to
    the FAIL side too. Calibration in month 2 needs to know which
    condition was doing the rejecting: "ADX below 20" rejecting 90% of
    the universe and "price below HMA200" rejecting 10% are very
    different findings about the same scanner, and neither is visible
    from the signals alone.

    These are logged, not stored in the analytics signal file -- the
    signal file is the thing that gets joined against forward returns,
    and rejections have no forward return to join to.
    """

    symbol: str
    scanner_name: str
    scanner_version: str
    reason: str


class ScannerDataError(Exception):
    """The bars needed for this evaluation were absent, too short, or
    unusable.

    Distinct from an unexpected crash: this is the expected outcome for
    a symbol that IPO'd last month and cannot have an HMA200. The
    framework logs it at debug level and moves to the next symbol, while
    a genuine exception is logged at error level with a traceback.
    """
