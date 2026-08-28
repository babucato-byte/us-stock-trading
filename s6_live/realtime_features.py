"""The one intraday view S6 entry and S6 exit both read.

The defect this exists for
--------------------------
S6's exit engine was handed `s1_executor.make_features_fn()`, which
fetches with `intraday_lookback_days=0, require_intraday=False` because
S1's exit needs a DAILY trend axis. Intraday-derived fields are left
None when there are no minute bars, so every S6 exit tick received:

    vwap = None, ema9 = None, ema21 = None

and `volume_expansion` was never a `SymbolFeatures` field at all -- the
ORB scanner computes it into the candidate row and nothing recomputes it
afterwards. Three of S6's seven exit rules therefore could not fire:

    VWAP_FAILURE                 vwap is None      -> predicate returns None
    EMA_STRUCTURE_FAILURE        ema9/21 None      -> predicate returns None
    VOLUME_DECAY_PRICE_WEAKNESS  no such attribute -> predicate returns None

Not a tuning problem. The rules were correct and were being asked
questions about values that never arrived. On the DT position this left
only two live price rules, sitting 3.8% and 7.1% below the entry.

Why one module for both sides
-----------------------------
The scanner saw a volume figure the exit engine did not have. Any design
where entry and exit compute their own view can drift like that again,
silently, because a missing value reads as "condition not met" rather
than as an error. Both sides now read this, and anything that could not
be computed is NAMED in `unavailable` rather than being None-shaped like
a false.

Unavailable is not false
------------------------
`SessionFeatures.status_of()` answers TRUE / FALSE / UNAVAILABLE for
each predicate input. A caller that cannot tell the difference will
treat a broken feed as a calm market, which is exactly how this went
unnoticed.

No thresholds are set here. This module supplies values; the rules that
judge them are unchanged in `config/s6_exit_v0.py` and
`s6_live/exit_policy.py`.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: Feature-availability vocabulary. UNAVAILABLE is a first-class answer.
AVAILABLE = "AVAILABLE"
UNAVAILABLE = "UNAVAILABLE"

#: Volume states that must never be conflated. A provider that does not
#: publish extended-hours volume reports 0 for every bar, which is
#: indistinguishable from a genuinely untraded bar unless it is asked
#: separately -- and S6's volume condition means nothing in either case.
VOLUME_OK = "VOLUME_OK"
VOLUME_ZERO_CONFIRMED = "ZERO_VOLUME_CONFIRMED"
VOLUME_DATA_UNAVAILABLE = "VOLUME_DATA_UNAVAILABLE"

#: The session happening now has published no bars at all.
#:
#: Deliberately not the same as a stale view: a stale view describes a
#: real moment that has passed, and this describes nothing. The previous
#: session's bars are never offered in its place -- that substitution is
#: what made a PREMARKET candidate on 2026-08-27 carry data from
#: 2026-08-26 19:30 ET while claiming to be current.
NO_CURRENT_SESSION_DATA = "NO_CURRENT_SESSION_DATA"

#: How old the newest intraday bar may be before the view is stale.
#: Deliberately generous: this is a data-integrity bound, not a trading
#: threshold, and a 5-minute bar feed is normally a few minutes behind.
DEFAULT_MAX_BAR_AGE_SECONDS = 15 * 60


@dataclass(frozen=True)
class SessionFeatures:
    """One symbol's intraday state, for one session, at one moment."""

    symbol: str
    session: Optional[str]
    # `market_data_asof` is the newest BAR's timestamp -- when the market
    # was last observed. Never the time this object was built: those two
    # are different facts and conflating them is what let a candidate
    # look fresh while carrying hours-old prices.
    market_data_asof: Optional[datetime] = None
    built_at: Optional[datetime] = None
    price: Optional[float] = None
    vwap: Optional[float] = None
    ema9: Optional[float] = None
    ema21: Optional[float] = None
    volume: Optional[float] = None
    volume_status: str = VOLUME_DATA_UNAVAILABLE
    volume_expansion: Optional[float] = None
    range_high: Optional[float] = None
    range_low: Optional[float] = None
    extension_pct: Optional[float] = None
    bar_count: int = 0
    #: Where each number came from. A READY candidate has to be
    #: traceable to a source: the same feature computed from the daily
    #: provider and from the KIS trade stream are different measurements,
    #: and "which one said this was READY" is the first question after a
    #: surprising trade.
    price_source: Optional[str] = None
    volume_source: Optional[str] = None
    #: LIVE / STALE / DISCONNECTED / UNKNOWN for a streaming source;
    #: None when the source does not stream.
    feed_status: Optional[str] = None
    #: Names of the inputs that could not be computed, and why.
    unavailable: Dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def volume_available(self) -> bool:
        return self.volume_status == VOLUME_OK

    def age_seconds(self, now=None) -> Optional[float]:
        if self.market_data_asof is None:
            return None
        moment = now or datetime.now(timezone.utc)
        return (moment - self.market_data_asof).total_seconds()

    def is_stale(self, now=None, max_age=DEFAULT_MAX_BAR_AGE_SECONDS) -> bool:
        """Unknown age counts as stale -- see the module docstring."""
        age = self.age_seconds(now)
        return age is None or age > max_age

    def status_of(self, name) -> str:
        """AVAILABLE / UNAVAILABLE for one named input."""
        return UNAVAILABLE if name in self.unavailable else AVAILABLE

    def as_record(self, now=None) -> Dict[str, Any]:
        """The flat shape an observability log stores."""
        return {
            "symbol": self.symbol,
            "session": self.session,
            "market_data_asof": (self.market_data_asof.isoformat()
                                 if self.market_data_asof else None),
            "age_seconds": self.age_seconds(now),
            "price": self.price,
            "vwap": self.vwap,
            "ema9": self.ema9,
            "ema21": self.ema21,
            "volume": self.volume,
            "volume_status": self.volume_status,
            "volume_expansion": self.volume_expansion,
            "range_high": self.range_high,
            "range_low": self.range_low,
            "extension_pct": self.extension_pct,
            "bar_count": self.bar_count,
            "price_source": self.price_source,
            "volume_source": self.volume_source,
            "feed_status": self.feed_status,
            "unavailable": dict(self.unavailable),
            "error": self.error,
        }


def _finite(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"),
                                                         float("-inf")) else None


def _as_utc(stamp) -> Optional[datetime]:
    if stamp is None:
        return None
    try:
        moment = stamp.to_pydatetime() if hasattr(stamp, "to_pydatetime") else stamp
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(moment, datetime):
        return None
    return moment.astimezone(timezone.utc) if moment.tzinfo else moment.replace(
        tzinfo=timezone.utc)


def _volume_status(session_bars) -> Tuple[str, Optional[float]]:
    """Distinguish "nothing traded" from "nobody told us what traded".

    A provider without extended-hours volume returns 0 for every bar of
    the session. A genuinely quiet session has SOME non-zero bars. The
    test is therefore whether the whole session is zero, not whether the
    latest bar is.
    """
    from scanners.base import indicators as ind

    try:
        volumes = ind.numeric(ind.volume_series(session_bars)).dropna()
    except Exception:  # noqa: BLE001
        return VOLUME_DATA_UNAVAILABLE, None
    if len(volumes) == 0:
        return VOLUME_DATA_UNAVAILABLE, None
    total = float(volumes.sum())
    latest = _finite(volumes.iloc[-1])
    if total <= 0:
        # Every bar zero. Cannot tell an untraded session from a feed
        # that omits this session's volume, and S6's volume condition is
        # unanswerable either way -- which is the part that matters.
        return VOLUME_DATA_UNAVAILABLE, latest
    if latest is not None and latest <= 0:
        return VOLUME_ZERO_CONFIRMED, latest
    return VOLUME_OK, latest


def build(symbol, *, session=None, now=None, provider=None,
          intraday_interval="5m", intraday_lookback_days=2,
          range_minutes=15) -> SessionFeatures:
    """The current intraday view, or a view that says what is missing.

    Never raises. A failure produces a SessionFeatures whose `error` and
    `unavailable` say so, because a caller that gets an exception has to
    invent a fallback and the fallback is always "carry on".
    """
    moment = now or datetime.now(timezone.utc)
    from scanners.base import scan_session

    resolved = scan_session.normalize(session) or scan_session.session_at(moment)
    missing: Dict[str, str] = {}

    try:
        from scanners.base import indicators as ind
        from scanners.base import session_range as srange
        from scanners.base.market_data_provider import default_provider

        source = provider or default_provider(cached=False)
        data = source.get_symbol_data(
            symbol, daily_lookback_days=5,
            intraday_interval=intraday_interval,
            intraday_lookback_days=intraday_lookback_days,
            include_prepost=True, want_premarket=True)
        intraday = getattr(data, "intraday", None)
        if intraday is None or len(intraday) == 0:
            return SessionFeatures(
                symbol=symbol, session=resolved, built_at=moment,
                unavailable={k: "no intraday bars" for k in
                             ("price", "vwap", "ema9", "ema21", "volume",
                              "volume_expansion")},
                error="no intraday bars")

        # Scoped to the session happening NOW, not to the most recent
        # date that happens to have bars for it. Without the date,
        # `slice_session_bars` takes max(candidates) -- so on a morning
        # when the provider has published nothing, a PREMARKET slice
        # returns YESTERDAY's premarket and every value below describes
        # a session that ended eighteen hours ago.
        session_date = srange.current_session_date(resolved, moment)
        bars = srange.slice_session_bars(intraday, resolved,
                                         session_date=session_date)
        if bars is None or len(bars) == 0:
            # The current session has published nothing. Distinct from
            # "the feed is broken", and distinct again from "yesterday
            # had bars" -- which is not an answer to what is happening
            # now, and is never substituted for one.
            return SessionFeatures(
                symbol=symbol, session=resolved, built_at=moment,
                market_data_asof=None,
                unavailable={k: NO_CURRENT_SESSION_DATA for k in
                             ("price", "vwap", "ema9", "ema21", "volume",
                              "volume_expansion")},
                error=f"{NO_CURRENT_SESSION_DATA}: no {resolved} bars for "
                      f"{session_date}")

        closes = ind.close_series(bars)
        price = _finite(ind.last_valid(closes))
        if price is None:
            missing["price"] = "no usable close"

        vwap = _finite(ind.last_valid(ind.session_vwap(bars)))
        if vwap is None:
            missing["vwap"] = "session VWAP not computable"

        ema9 = _finite(ind.last_valid(ind.ema(closes, 9)))
        ema21 = _finite(ind.last_valid(ind.ema(closes, 21)))
        if ema9 is None:
            missing["ema9"] = "EMA9 not computable"
        if ema21 is None:
            missing["ema21"] = "EMA21 not computable"

        volume_status, volume = _volume_status(bars)
        if volume_status != VOLUME_OK:
            missing["volume"] = volume_status

        # Opening range and the expansion measured against it -- the same
        # quantity the ORB scanner computes, recomputed each tick rather
        # than frozen at entry.
        range_high = range_low = expansion = None
        try:
            window = srange.opening_range(intraday, resolved,
                                          minutes=range_minutes)
            if window is not None:
                # SessionRange names them range_high/range_low, and
                # `complete` is its own check that a high WITHOUT a low
                # is not a range.
                if getattr(window, "complete", False):
                    range_high = _finite(getattr(window, "range_high", None))
                    range_low = _finite(getattr(window, "range_low", None))
                end = getattr(window, "range_end", None)
                if end is not None:
                    range_bars = bars[[stamp <= end for stamp in bars.index]]
                    post = bars.iloc[len(range_bars):]
                    if volume_status == VOLUME_OK and len(range_bars) and len(post):
                        expansion = ind.safe_ratio(
                            float(ind.numeric(ind.volume_series(post)).mean()),
                            float(ind.numeric(ind.volume_series(range_bars)).mean()))
                        expansion = _finite(expansion)
        except Exception:  # noqa: BLE001 - the range is a bonus here
            logger.debug("S6 opening range unavailable for %s", symbol,
                         exc_info=True)
        if range_high is None:
            missing["range_high"] = "opening range not computable"
        if expansion is None and "volume_expansion" not in missing:
            missing["volume_expansion"] = (
                volume_status if volume_status != VOLUME_OK
                else "expansion not computable")

        extension = None
        if price is not None and range_high:
            extension = (price / range_high - 1.0) * 100.0

        return SessionFeatures(
            symbol=symbol, session=resolved,
            market_data_asof=_as_utc(bars.index[-1]), built_at=moment,
            price=price, vwap=vwap, ema9=ema9, ema21=ema21,
            volume=volume, volume_status=volume_status,
            volume_expansion=expansion,
            range_high=range_high, range_low=range_low,
            extension_pct=extension, bar_count=len(bars),
            unavailable=missing)
    except Exception as exc:  # noqa: BLE001 - a view that failed says so
        logger.warning("S6 realtime features failed for %s", symbol,
                       exc_info=True)
        return SessionFeatures(
            symbol=symbol, session=resolved, built_at=moment,
            unavailable={k: "build failed" for k in
                         ("price", "vwap", "ema9", "ema21", "volume",
                          "volume_expansion")},
            error=f"{type(exc).__name__}: {exc}")


def make_features_fn(*, session=None, now=None, provider=None):
    """A `features_fn(symbol)` for the S6 exit runtime.

    Same call shape the runtime already uses, so the exit engine's own
    code does not change -- only what it is handed.
    """
    def features_fn(symbol):
        return build(symbol, session=session, now=now, provider=provider)

    return features_fn
