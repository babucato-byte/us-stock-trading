"""S6's features for a session, computed from KIS trade bars.

The premarket and after-hours half of the feature layer. The daily-bar
provider reports zero volume outside regular hours -- a number that
reads as "nobody traded" rather than "no data" -- so volume expansion
was unanswerable there and those sessions could scan but never produce a
READY candidate. KIS does carry the volume; this reads it.

Deliberately NOT a second strategy. The conditions, the thresholds and
the arithmetic are S6's existing ones; only the bars underneath differ.
A separate "simplified premarket strategy" would be a second thing to
verify and a second thing to be wrong, and the whole premise of S6 is
that a breakout is the same shape in every session.

What it refuses to do
---------------------
Report a number it cannot support. No volume means no VWAP and no
expansion ratio, stated as unavailable rather than defaulted -- a
fabricated denominator produces a price a strategy compares against, and
being wrong there is worse than abstaining. A stale or disconnected feed
is not a source of features at all.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from market_data import realtime_bars as rb

logger = logging.getLogger(__name__)

SOURCE = rb.SOURCE

#: A feature snapshot is only built from a feed that is currently
#: delivering. STALE and DISCONNECTED are refusals, not degradations.
USABLE_FEED_STATES = (rb.FEED_LIVE,)


def _ema(values, span):
    if not values:
        return None
    multiplier = 2.0 / (span + 1.0)
    average = values[0]
    for value in values[1:]:
        average = (value - average) * multiplier + average
    return average


def build_from_bars(symbol, *, store, session, now=None,
                    range_minutes=15, average_window=20):
    """A SessionFeatures built from this session's KIS bars.

    Returns None when the store has nothing for this symbol and session,
    so the caller can fall back to its existing provider rather than
    receiving an empty snapshot that looks like a measured emptiness.
    """
    from s6_live.realtime_features import (
        SessionFeatures, VOLUME_DATA_UNAVAILABLE, VOLUME_OK,
        VOLUME_ZERO_CONFIRMED,
    )

    moment = now or datetime.now(timezone.utc)
    accumulator = store.accumulator(symbol, session)
    bars = store.bars(symbol, session)
    feed_status = store.feed_status(now=moment)

    if accumulator is None or not bars:
        return None

    if feed_status not in USABLE_FEED_STATES:
        # The bars may be perfectly good and simply old. Saying so is the
        # point: an entry decided on a frozen view of a moving market is
        # the failure this whole layer exists to prevent.
        return SessionFeatures(
            symbol=symbol, session=session, built_at=moment,
            market_data_asof=bars[-1].market_data_asof,
            bar_count=len(bars), price_source=SOURCE, volume_source=SOURCE,
            feed_status=feed_status,
            unavailable={k: f"feed is {feed_status}" for k in
                         ("price", "vwap", "ema9", "ema21", "volume",
                          "volume_expansion")},
            error=f"realtime feed is {feed_status}")

    closes = [b.close for b in bars]
    volumes = [b.volume for b in bars]
    price = closes[-1]
    asof = bars[-1].market_data_asof

    unavailable = {}

    # VWAP from THIS session's trades. Never TAMT/TVOL: those are KIS's
    # cumulative counters and they were measured to cover a wider window
    # than a collector that joined mid-session, so using them would
    # silently mix in prints from before we were listening.
    vwap = accumulator.vwap
    if vwap is None:
        unavailable["vwap"] = "no volume to weight by"

    total_volume = accumulator.volume
    if total_volume > 0:
        volume_status = VOLUME_OK
    elif accumulator.trade_count:
        volume_status = VOLUME_ZERO_CONFIRMED
    else:
        volume_status = VOLUME_DATA_UNAVAILABLE
        unavailable["volume"] = "no trades observed this session"

    # Expansion against this session's own recent average, on the same
    # definition S6 already uses -- the latest bar against the mean of
    # the preceding ones. Undefined with a single bar, and stated as
    # such rather than defaulted to 1.0, which would read as "average".
    volume_expansion = None
    if len(volumes) >= 2:
        window = volumes[-(average_window + 1):-1]
        average = sum(window) / len(window) if window else 0.0
        if average > 0:
            volume_expansion = volumes[-1] / average
        else:
            unavailable["volume_expansion"] = "no traded volume to compare against"
    else:
        unavailable["volume_expansion"] = "need at least two bars"

    ema9 = _ema(closes, 9) if len(closes) >= 2 else None
    ema21 = _ema(closes, 21) if len(closes) >= 2 else None
    if ema9 is None:
        unavailable["ema9"] = "need at least two bars"
    if ema21 is None:
        unavailable["ema21"] = "need at least two bars"

    # The opening range is this SESSION's opening range: the first
    # `range_minutes` of bars we have for it. A premarket ORB measured
    # from regular-session bars would be a different market's range.
    opening = bars[:range_minutes]
    range_high = max((b.high for b in opening), default=None)
    range_low = min((b.low for b in opening), default=None)
    extension_pct = None
    if range_high and range_low is not None and range_high > 0:
        extension_pct = (price - range_high) / range_high * 100.0

    return SessionFeatures(
        symbol=symbol, session=session, built_at=moment,
        market_data_asof=asof, price=price, vwap=vwap,
        ema9=ema9, ema21=ema21,
        volume=total_volume, volume_status=volume_status,
        volume_expansion=volume_expansion,
        range_high=range_high, range_low=range_low,
        extension_pct=extension_pct, bar_count=len(bars),
        price_source=SOURCE, volume_source=SOURCE, feed_status=feed_status,
        unavailable=unavailable)


def load_store(session, trading_day, *, env=None, stale_after_seconds=None):
    """This session's collected bars, or None if there are none.

    The snapshot is keyed by session AND trading day, and only the
    matching one is loaded -- that keying is what stops yesterday's
    regular-session volume from becoming this morning's premarket
    volume, which is the same class of mistake as an unscoped session
    slice returning an eighteen-hour-old view.

    Returns None on anything unreadable. The caller then falls back to
    its existing provider, which is the behaviour that was in place
    before this layer existed; raising here would take down a cycle over
    a data file.
    """
    import json
    import os
    from pathlib import Path

    mapping = env if env is not None else os.environ
    root = (mapping.get("REALTIME_BAR_DIR")
            or mapping.get("SCANNER_DATA_ROOT")
            or "/home/ubuntu/releases/us-stock-trading/shared/scanner")
    path = Path(root) / "realtime_bars" / f"{trading_day}-{session}.json"
    try:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.warning("could not read realtime bars at %s", path, exc_info=True)
        return None

    kwargs = {}
    if stale_after_seconds is not None:
        kwargs["stale_after_seconds"] = stale_after_seconds
    store = rb.RealtimeBarStore.restore(payload, **kwargs)

    # A snapshot carries no clock of its own, so a collector that died
    # leaves a file that looks current forever. The file's own mtime is
    # the honest answer to "when did anyone last write to this", and a
    # store nobody is feeding must not present itself as LIVE.
    try:
        written_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        store.snapshot_written_at = written_at
    except OSError:
        pass
    return store
