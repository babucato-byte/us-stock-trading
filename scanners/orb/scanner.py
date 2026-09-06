"""S6 -- Opening Range Breakout.

The theory
----------
The first N minutes of the regular session set a range; a decisive move
through the top of it, with the session's other momentum tells aligned
(above VWAP, EMA9 over EMA21, volume expanding), is the candidate.

ORB5 / ORB15 / ORB30
--------------------
`orb_minutes` in config.json selects the window, validated against
`supported_orb_minutes`. Section S6 asks for all three to be supported;
15 is the v1.0 value. The window is applied from the session's FIRST BAR
rather than from a hardcoded 09:30, so a halted or late-opening name
gets a range measured from when it actually started trading.

Wick versus close, which is the substance of this scanner
----------------------------------------------------------
Section S6 singles this out: a wick through the range high is not the
same event as a bar CLOSING above it. One is a print, often a single
sweep; the other is acceptance. Both are recorded --

    breakout_touched     some bar's HIGH exceeded the range high
    breakout_confirmed   some bar CLOSED above it

-- and `require_close_breakout` decides which one qualifies in v1.0
(it does: confirmed). Keeping `breakout_touched` on every signal means
month 1 can measure what the wick-only names actually did, which is the
evidence needed to keep or drop that requirement in month 2.

`retest_confirmed` is recorded but never required. A retest is a
higher-quality entry when it happens, and waiting for one means missing
the moves that never look back; which trade-off is better is a question
for the data, so it scores rather than filters.

Not an entry signal
-------------------
Section 23 keeps "which symbol" and "when to buy" apart, and this
scanner is firmly the first. It says a name broke its opening range with
momentum behind it -- not that this instant is the price to pay.

When, not only whether
----------------------
None of the gates above mentions the clock. The session slice is the
WHOLE session so far, `breakout_confirmed` is true if ANY post-range bar
closed above the range, and `volume_expansion` is the mean over EVERY
post-range bar -- so a name that broke out at 10:00 is, at 15:30, the
same candidate it was at 10:05, with the morning's volume still in the
mean and nothing asking when the last new high was. S6-R "fresh"
candidates can therefore be generated in the last hour from a breakout
five hours old.

Four timing measurements are now recorded on every signal:

    session_elapsed_minutes        first session bar -> newest bar
    breakout_age_minutes           first breakout bar -> newest bar
    minutes_since_session_high     bar that set the post-range HIGH -> newest
    recent_volume_expansion_<W>m   mean volume of the last W minutes /
                                   mean range volume, for each W in
                                   `recent_volume_observation_minutes`

and four gates read them -- `entry_window_minutes`,
`max_breakout_age_minutes`, `recent_volume_window_minutes` with
`recent_volume_expansion_min`, and `max_minutes_since_session_high`.
Every gate ships null, which is OFF. The measurements come first so the
windows are chosen from the distribution real candidates produce rather
than fixed by guess and then measured under the guess. Minutes are bar
LABELS (a 5m bar's start), so an elapsed figure excludes the newest
bar's own width.
"""

from typing import Any, Dict, List, Optional

import pandas as pd

from scanners.base import indicators as ind
from scanners.base import session as sess
from scanners.base.config import ScannerConfigError
from scanners.base.features import SymbolFeatures
from scanners.base.market_data_provider import SymbolData
from scanners.base.models import ScannerDataError
from scanners.base.scanner_base import bar_timestamp, BaseScanner, fmt, require


class OpeningRangeBreakoutScanner(BaseScanner):

    @property
    def market_data_basis(self):
        """Minute bars, not daily: this scanner's gates are computed from
        the intraday session."""
        from scanners.base.scanner_base import MARKET_DATA_BASIS_INTRADAY

        return MARKET_DATA_BASIS_INTRADAY
    scanner_dir = "orb"
    scanner_name = "orb"
    requires_intraday = True
    #: Its verdict is an intraday one: price, VWAP and EMAs all come
    #: from minute bars, so `data_timestamp` must report the newest
    #: MINUTE bar, not the newest daily bar the feature pass also read.
    source_timeframe = "1m"

    def orb_minutes(self) -> int:
        """The configured window, validated against the supported set.

        Rejecting an unsupported value outright rather than falling back
        to 15 matters for section 11: a silently-corrected typo would
        mean a month of data labelled ORB15 that was collected under
        whatever the operator thought they had set.
        """
        minutes = self.config.require_int("orb_minutes")
        supported = self.config.get("supported_orb_minutes") or []
        if supported and minutes not in [int(value) for value in supported]:
            raise ScannerConfigError(
                f"orb_minutes={minutes} is not one of the supported values {supported}")
        return minutes

    def check(self, features: SymbolFeatures, data: SymbolData,
              context: Dict[str, Any]) -> List[str]:
        config = self.config
        reasons: List[str] = []
        minutes = self.orb_minutes()

        # Which session's range this run is judging. REGULAR is the
        # default and takes the ORIGINAL path byte for byte -- S6-R is
        # the measured v1.0 behaviour and is not being changed. The other
        # sessions route through the session-aware engine, which is the
        # only thing that knows a 20:00->04:00 window wraps midnight.
        requested = str(context.get("session") or "REGULAR").strip().upper()

        if requested == "REGULAR":
            session = sess.slice_session(
                data.intraday,
                regular_only=config.require_bool("regular_session_only"),
            )
            if session is None or len(session) == 0:
                raise ScannerDataError(f"{data.symbol}: no regular-session bars today")

            range_high, range_low, range_bars = sess.opening_range(session, minutes)
            if range_high is None or range_low is None or len(range_bars) == 0:
                raise ScannerDataError(
                    f"{data.symbol}: opening range ({minutes}m) not computable")
        else:
            from scanners.base import session_range as srange

            # Scoped to the session happening NOW. Without the date,
            # `slice_session_bars` takes the most recent date that HAS
            # bars for this session -- so on a morning when the provider
            # has published nothing, a PREMARKET slice returns
            # YESTERDAY's premarket and the scan describes an
            # eighteen-hour-old market as current. That produced a live
            # PTC candidate on 2026-08-27 whose data was from
            # 2026-08-26 19:30 ET.
            session_date = srange.current_session_date(requested)
            session = srange.slice_session_bars(data.intraday, requested,
                                                session_date=session_date)
            if session is None or len(session) == 0:
                # NO_CURRENT_SESSION_DATA, not "no setups". The previous
                # session's bars are never offered in its place.
                raise ScannerDataError(
                    f"{data.symbol}: NO_CURRENT_SESSION_DATA -- no "
                    f"{requested} bars for {session_date}")

            window = srange.opening_range(data.intraday, requested,
                                          minutes=minutes,
                                          session_date=session_date)
            if not window.complete:
                # Not a rejection. A session whose range has not formed
                # yet has nothing to say; calling it a market judgement
                # would make an early scan look like a session with no
                # setups.
                raise ScannerDataError(
                    f"{data.symbol}: {requested} opening range ({minutes}m) "
                    "not computable")
            range_high, range_low = window.range_high, window.range_low
            range_bars = session[[stamp <= window.range_end
                                  for stamp in session.index]] \
                if window.range_end is not None else session.iloc[:0]

        post = session.iloc[len(range_bars):]
        minimum_post = config.require_int("min_post_range_bars")
        if len(post) < minimum_post:
            # Not a rejection: the opening range simply has not been
            # given a chance to break yet. Recording this as a market
            # judgement would make an early-run scan look like a day
            # with no setups.
            raise ScannerDataError(
                f"{data.symbol}: {len(post)} bars since the {minutes}m opening range, "
                f"need {minimum_post}")

        range_mid = (range_high + range_low) / 2.0
        closes = ind.close_series(session).dropna()
        require(len(closes) > 0, "session bars have no usable closes")
        price = ind.to_float(closes.iloc[-1])
        require(price is not None and price > 0, "no usable current price")

        post_highs = ind.high_series(post).dropna()
        post_closes = ind.close_series(post).dropna()
        breakout_touched = bool(len(post_highs) and float(post_highs.max()) > range_high)
        confirmed_positions = [
            position for position, value in enumerate(post_closes.tolist())
            if value > range_high
        ]
        breakout_confirmed = bool(confirmed_positions)

        # The two branches are genuinely different tests, which is the
        # point of the flag. Note that `price` IS the latest close, so
        # "price above the range high" already implies some close was
        # above it -- checking both in the strict branch is not
        # redundant only because the second one asks something else:
        # whether the breakout is STILL holding.
        if config.require_bool("require_close_breakout"):
            require(breakout_confirmed,
                    f"no bar has CLOSED above the opening range high {fmt(range_high)}"
                    + (" (wick only)" if breakout_touched else ""))
            require(price > range_high,
                    f"broke the opening range high {fmt(range_high)} but has fallen back "
                    f"inside the range (now {fmt(price)})")
            reasons.append(f"breakout confirmed on a closing basis, holding at {fmt(price)} "
                           f"above the {minutes}m range high {fmt(range_high)}")
        else:
            # Relaxed branch: an intrabar poke through the range counts,
            # even if price has since slipped back inside. Kept
            # reachable so the wick-only population can be MEASURED in
            # month 1 -- section S6 wants to know how those resolve, and
            # a config that could only ever be set one way would never
            # produce the evidence.
            require(breakout_touched,
                    f"the opening range high {fmt(range_high)} has not been touched")
            if price > range_high:
                reasons.append(f"price {fmt(price)} above the {minutes}m opening range "
                               f"high {fmt(range_high)}")
            else:
                reasons.append(f"opening range high {fmt(range_high)} touched intrabar; "
                               f"price back inside at {fmt(price)}")

        extension_pct = (price / range_high - 1.0) * 100.0
        ceiling = config.require_float("max_extension_above_or_high_pct")
        require(extension_pct <= ceiling,
                f"already {fmt(extension_pct)}% above the opening range high, "
                f"past the {fmt(ceiling)}% limit")
        reasons.append(f"{fmt(extension_pct)}% above the range high")

        vwap = ind.last_valid(ind.session_vwap(session))
        vwap_distance = None
        if config.require_bool("require_price_above_vwap"):
            require(vwap not in (None, 0), "session VWAP not computable")
            require(price > vwap, f"price {fmt(price)} at/below VWAP {fmt(vwap)}")
            vwap_distance = (price / vwap - 1.0) * 100.0
            reasons.append(f"price above VWAP {fmt(vwap)} ({fmt(vwap_distance)}%)")
        elif vwap not in (None, 0):
            vwap_distance = (price / vwap - 1.0) * 100.0

        ema_fast = ind.last_valid(ind.ema(closes, 9))
        ema_slow = ind.last_valid(ind.ema(closes, 21))
        if config.require_bool("require_ema9_above_ema21"):
            require(ema_fast is not None and ema_slow is not None,
                    "session EMA9/EMA21 not computable")
            require(ema_fast > ema_slow,
                    f"EMA9 {fmt(ema_fast)} at/below EMA21 {fmt(ema_slow)}")
            reasons.append(f"EMA9 {fmt(ema_fast)} above EMA21 {fmt(ema_slow)}")

        range_volume = _mean_volume(range_bars)
        post_volume = _mean_volume(post)
        expansion = ind.safe_ratio(post_volume, range_volume)
        minimum_expansion = config.require_float("volume_expansion_min")
        require(expansion is not None,
                f"volume expansion not computable (range mean={fmt(range_volume, 0)})")
        require(expansion >= minimum_expansion,
                f"volume expansion {fmt(expansion)}x below {fmt(minimum_expansion)}x")
        reasons.append(f"volume expansion {fmt(expansion)}x the opening range")

        # ---- timing: recorded always, gated only when configured -------
        # After every existing gate on purpose: the reasons month one's
        # rejection statistics are keyed on keep speaking first, and a
        # timing gate that is switched on cannot re-label an existing
        # rejection as a late one.
        first_breakout = (confirmed_positions[0] if confirmed_positions
                          else _first_touch(post_highs, range_high))
        timing = _timing_metrics(
            session, post, len(range_bars), price=price,
            range_volume=range_volume, breakout_position=first_breakout,
            observation_windows=config.get(
                "recent_volume_observation_minutes") or ())
        _apply_timing_gates(config, timing, session=session,
                            range_volume=range_volume, symbol=data.symbol)

        retest_confirmed = _retest_confirmed(
            post, range_high,
            first_confirmed=confirmed_positions[0] if confirmed_positions else None,
            tolerance_pct=config.require_float("retest_tolerance_pct"),
        )
        if retest_confirmed:
            reasons.append("retested the range high and held")

        context.update({
            "orb_minutes": minutes,
            "opening_range_high": range_high,
            "opening_range_low": range_low,
            "opening_range_mid": range_mid,
            "opening_range_bars": len(range_bars),
            "post_range_bars": len(post),
            "price": price,
            "breakout_touched": breakout_touched,
            "breakout_confirmed": breakout_confirmed,
            "retest_confirmed": retest_confirmed,
            "extension_above_or_high_pct": extension_pct,
            "vwap": vwap,
            "vwap_distance": vwap_distance,
            "session_ema9": ema_fast,
            "session_ema21": ema_slow,
            "opening_range_volume": range_volume,
            "post_range_volume": post_volume,
            "volume_expansion": expansion,
            # When these numbers were last true, as distinct from when
            # the row carrying them is written. Everything above is
            # computed from `session`, so its final bar IS the moment
            # this judgement describes.
            "market_data_asof": bar_timestamp(session),
            **timing,
        })
        return reasons

    def score(self, features: SymbolFeatures, data: SymbolData,
              context: Dict[str, Any]) -> float:
        config = self.config

        # A confirmed close through the range beats a wick that has
        # since been held; a wick alone (only reachable when
        # require_close_breakout is off) earns partial credit.
        if context.get("breakout_confirmed"):
            quality = 1.0
        elif context.get("breakout_touched"):
            quality = 0.5
        else:
            quality = 0.0
        quality_part = quality * config.require_float("score_weight_breakout_quality")

        volume_part = _ramp(
            context.get("volume_expansion"),
            config.require_float("volume_expansion_min"),
            config.require_float("score_volume_expansion_strong"),
        ) * config.require_float("score_weight_volume_expansion")

        # Just through the range is a better place to be looking than
        # already several percent past it -- the latter is the chase
        # this project is trying to distinguish itself from.
        proximity_part = _decay(
            context.get("extension_above_or_high_pct"),
            config.require_float("score_ideal_extension_pct"),
            config.require_float("max_extension_above_or_high_pct"),
        ) * config.require_float("score_weight_entry_proximity")

        vwap_part = _ramp(
            context.get("vwap_distance"), 0.0, 2.0,
        ) * config.require_float("score_weight_vwap")

        retest_part = (config.require_float("score_weight_retest")
                       if context.get("retest_confirmed") else 0.0)

        return quality_part + volume_part + proximity_part + vwap_part + retest_part

    def extra_metrics(self, features: SymbolFeatures, data: SymbolData,
                      context: Dict[str, Any]) -> Dict[str, Any]:
        metrics = {key: context.get(key) for key in (
            "orb_minutes", "opening_range_high", "opening_range_low",
            "opening_range_mid", "opening_range_bars", "post_range_bars",
            "breakout_touched", "breakout_confirmed", "retest_confirmed",
            "extension_above_or_high_pct", "vwap_distance", "session_ema9",
            "session_ema21", "opening_range_volume", "post_range_volume",
            "volume_expansion",
        )}
        for key in ENTRY_TIMING_KEYS:
            metrics[key] = context.get(key)
        for key, value in context.items():
            if key.startswith(RECENT_VOLUME_PREFIX):
                metrics[key] = value
        return metrics

    def override_schema_fields(self, features: SymbolFeatures, data: SymbolData,
                               context: Dict[str, Any]) -> Dict[str, Any]:
        """Record the intraday moment this scanner judged.

        Same reasoning as the gap-pullback scanner: the forward returns
        of sections 12 and 13 are measured from `signal_price`, so it
        has to be the price at the instant of the breakout, and the
        EMA/VWAP columns should describe that instant too rather than
        the framework's broader intraday pass.
        """
        price = context.get("price")
        if price is None:
            return {}
        vwap = context.get("vwap")
        return {
            "signal_price": price,
            "vwap": vwap,
            "ema9": context.get("session_ema9"),
            "ema21": context.get("session_ema21"),
            "distance_20d_high": ind.distance_pct(price, features.high_20d),
            "distance_50d_high": ind.distance_pct(price, features.high_50d),
            "distance_52w_high": ind.distance_pct(price, features.high_52w),
            "extension_hma89_pct": ind.extension_pct(price, features.hma89),
            "extension_hma200_pct": ind.extension_pct(price, features.hma200),
            "extension_vwap_pct": ind.extension_pct(price, vwap),
        }


#: The fixed-name timing measurements. The per-window volume ratios are
#: named `recent_volume_expansion_<W>m` and found by prefix, so a change
#: to the observation windows is a config edit rather than a code one.
ENTRY_TIMING_KEYS = (
    "session_elapsed_minutes",
    "breakout_at",
    "breakout_age_minutes",
    "bars_since_breakout",
    "session_high",
    "session_high_at",
    "minutes_since_session_high",
    "drawdown_from_session_high_pct",
)
RECENT_VOLUME_PREFIX = "recent_volume_expansion_"


def _minutes_between(frame, start_pos: int, end_pos: int) -> Optional[float]:
    """Minutes from one bar to another, by label.

    A frame without a datetime index counts rows as minutes -- the same
    convention `session.opening_range` uses for the one-minute fixtures,
    so a test frame and a market frame measure the same thing.
    """
    if frame is None or len(frame) == 0:
        return None
    if start_pos < 0 or end_pos >= len(frame) or start_pos > end_pos:
        return None
    if sess.has_datetime_index(frame):
        delta = frame.index[end_pos] - frame.index[start_pos]
        return float(delta.total_seconds() / 60.0)
    return float(end_pos - start_pos)


def _label(frame, position: int) -> Optional[str]:
    if frame is None or not sess.has_datetime_index(frame):
        return None
    try:
        stamp = frame.index[position]
        return stamp.isoformat()
    except (IndexError, AttributeError):
        return None


def _first_touch(post_highs, range_high) -> Optional[int]:
    """Position in `post` of the first bar whose HIGH exceeded the range."""
    for position, value in enumerate(post_highs.tolist()):
        if value > range_high:
            return position
    return None


def _recent_volume_expansion(session, minutes, range_volume) -> Optional[float]:
    """Mean volume of the last `minutes` of the session / mean range volume.

    The window is taken from the newest bar's label back `minutes`,
    exclusive of the far end, so on 5m bars a 30-minute window is six
    bars. Early in the session it overlaps the opening range itself;
    `session_elapsed_minutes` travels alongside so a reader can tell.
    """
    if session is None or len(session) == 0 or range_volume in (None, 0):
        return None
    try:
        span = int(minutes)
    except (TypeError, ValueError):
        return None
    if span <= 0:
        return None
    if sess.has_datetime_index(session):
        cutoff = session.index[-1] - pd.Timedelta(minutes=span)
        window = session[session.index > cutoff]
    else:
        window = session.iloc[-span:]
    return ind.safe_ratio(_mean_volume(window), range_volume)


def _timing_metrics(session, post, range_length: int, *, price,
                    range_volume, breakout_position, observation_windows
                    ) -> Dict[str, Any]:
    """When the breakout happened, relative to the bar being judged."""
    last = len(session) - 1
    out: Dict[str, Any] = {
        "session_elapsed_minutes": _minutes_between(session, 0, last),
        "breakout_at": None,
        "breakout_age_minutes": None,
        "bars_since_breakout": None,
        "session_high": None,
        "session_high_at": None,
        "minutes_since_session_high": None,
        "drawdown_from_session_high_pct": None,
    }
    if breakout_position is not None:
        absolute = range_length + int(breakout_position)
        out["breakout_at"] = _label(session, absolute)
        out["breakout_age_minutes"] = _minutes_between(session, absolute, last)
        out["bars_since_breakout"] = len(post) - 1 - int(breakout_position)

    highs = ind.numeric(ind.high_series(post)).reset_index(drop=True)
    if len(highs.dropna()):
        top = float(highs.max())
        # The LAST bar that printed the high: "how long since a new
        # high" is measured from the most recent one, not the first.
        position = int(highs[highs == top].index[-1])
        absolute = range_length + position
        out["session_high"] = top
        out["session_high_at"] = _label(session, absolute)
        out["minutes_since_session_high"] = _minutes_between(
            session, absolute, last)
        if price is not None and top > 0:
            out["drawdown_from_session_high_pct"] = (top - price) / top * 100.0

    for window in observation_windows:
        try:
            key = f"{RECENT_VOLUME_PREFIX}{int(window)}m"
        except (TypeError, ValueError):
            continue
        out[key] = _recent_volume_expansion(session, window, range_volume)
    return out


def _gate_input(value, *, symbol, what):
    """A configured gate that cannot read its input refuses to judge.

    ScannerDataError, not Rejected: "could not be timed" is a data
    finding, and letting it pass would make the gate a no-op on exactly
    the frames it cannot see.
    """
    if value is None:
        raise ScannerDataError(f"{symbol}: {what} not computable, and a "
                               "timing gate depends on it")
    return float(value)


def _apply_timing_gates(config, timing: Dict[str, Any], *, session,
                        range_volume, symbol) -> None:
    """The four timing gates. Each is off while its parameter is null."""
    window = config.get("entry_window_minutes")
    if window is not None:
        elapsed = _gate_input(timing.get("session_elapsed_minutes"),
                              symbol=symbol, what="session elapsed time")
        require(elapsed <= float(window),
                f"{fmt(elapsed, 0)} min into the session, past the "
                f"{fmt(window, 0)} min entry window")

    max_age = config.get("max_breakout_age_minutes")
    if max_age is not None:
        age = _gate_input(timing.get("breakout_age_minutes"),
                          symbol=symbol, what="breakout age")
        require(age <= float(max_age),
                f"breakout is {fmt(age, 0)} min old, past the "
                f"{fmt(max_age, 0)} min limit")

    minimum_recent = config.get("recent_volume_expansion_min")
    recent_window = config.get("recent_volume_window_minutes")
    if minimum_recent is not None:
        if recent_window is None:
            raise ScannerConfigError(
                f"{config.scanner_name}: recent_volume_expansion_min is set "
                "but recent_volume_window_minutes is null; a threshold "
                "without a window measures nothing")
        key = f"{RECENT_VOLUME_PREFIX}{int(recent_window)}m"
        if key not in timing:
            timing[key] = _recent_volume_expansion(session, recent_window,
                                                   range_volume)
        recent = _gate_input(timing.get(key), symbol=symbol,
                             what=f"recent volume over {recent_window} min")
        require(recent >= float(minimum_recent),
                f"recent volume expansion {fmt(recent)}x over the last "
                f"{fmt(recent_window, 0)} min below {fmt(minimum_recent)}x")

    max_since_high = config.get("max_minutes_since_session_high")
    if max_since_high is not None:
        since = _gate_input(timing.get("minutes_since_session_high"),
                            symbol=symbol, what="time since the session high")
        require(since <= float(max_since_high),
                f"no new session high for {fmt(since, 0)} min, past the "
                f"{fmt(max_since_high, 0)} min limit")


def _mean_volume(frame) -> Optional[float]:
    volumes = ind.volume_series(frame).dropna()
    if len(volumes) == 0:
        return None
    return ind.to_float(volumes.mean())


def _retest_confirmed(post, range_high, *, first_confirmed, tolerance_pct) -> bool:
    """Did price come back to the range high after breaking, and hold?

    Three things have to happen in order, and the ORDER is what makes it
    a retest rather than a coincidence:

      1. a bar closes above the range high,
      2. a LATER bar trades back down to within `tolerance_pct` of it
         (its low reaches the band),
      3. a bar after THAT closes above the range high again.

    Without the third step, a name that broke out and then failed back
    into the range would be recorded as a successful retest -- the exact
    opposite of what the field is meant to mean.
    """
    if first_confirmed is None or post is None or len(post) == 0:
        return False
    lows = ind.low_series(post).reset_index(drop=True)
    closes = ind.close_series(post).reset_index(drop=True)
    if len(lows) == 0 or len(closes) == 0:
        return False
    band = float(range_high) * (1.0 + float(tolerance_pct) / 100.0)
    for position in range(int(first_confirmed) + 1, len(lows)):
        low = ind.to_float(lows.iloc[position])
        if low is None or low > band:
            continue
        later = closes.iloc[position + 1:].dropna()
        if len(later) and float(later.max()) > float(range_high):
            return True
    return False


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
