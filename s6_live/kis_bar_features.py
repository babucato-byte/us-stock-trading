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
from datetime import datetime, timedelta, timezone
from typing import Optional

from market_data import realtime_bars as rb

logger = logging.getLogger(__name__)

SOURCE = rb.SOURCE

#: A feature snapshot is only built from a feed that is currently
#: delivering. STALE and DISCONNECTED are refusals, not degradations.
USABLE_FEED_STATES = (rb.FEED_LIVE,)


def _gap_overlaps(store, bars):
    """Does any recorded collector gap fall inside these bars?

    Compared against the bars actually used, not the whole session: a
    disconnect an hour ago says nothing about the volume in the last
    twenty minutes, and treating it as permanent contamination would
    stand the strategy down for the rest of a session over a blip.
    """
    if not bars:
        return False
    gaps = getattr(store, "gaps", None) or ()
    if not gaps:
        return False
    first = bars[0].minute
    last = bars[-1].last_trade_at
    for gap in gaps:
        start = _parse_iso(gap.get("from"))
        end = _parse_iso(gap.get("to"))
        if start is None or end is None:
            # A gap we cannot place is a gap we cannot rule out.
            return True
        if end >= first and start <= last:
            return True
    return False


def _parse_iso(text):
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _ema(values, span):
    if not values:
        return None
    multiplier = 2.0 / (span + 1.0)
    average = values[0]
    for value in values[1:]:
        average = (value - average) * multiplier + average
    return average


def build_from_bars(symbol, *, store, session, now=None,
                    range_minutes=15, average_window=20,
                    closed_bar_only=False):
    """A SessionFeatures built from this session's KIS bars.

    Returns None when the store has nothing for this symbol and session,
    so the caller can fall back to its existing provider rather than
    receiving an empty snapshot that looks like a measured emptiness.
    """
    from s6_live.realtime_features import (
        DATA_INCOMPLETE, SessionFeatures, VOLUME_DATA_UNAVAILABLE, VOLUME_OK,
        VOLUME_ZERO_CONFIRMED,
    )

    moment = now or datetime.now(timezone.utc)
    accumulator = store.accumulator(symbol, session)
    all_bars = store.bars(symbol, session)
    feed_status = store.feed_status(now=moment)

    if accumulator is None or not all_bars:
        return None

    # Explicit production policy: decisions use completed one-minute bars.
    # The current minute is still accumulating and cannot trigger an entry.
    current_minute = moment.replace(second=0, microsecond=0)
    bars = ([bar for bar in all_bars if bar.minute < current_minute]
            if closed_bar_only else list(all_bars))
    if not bars:
        return SessionFeatures(
            symbol=symbol, session=session, built_at=moment,
            price_source=SOURCE, volume_source=SOURCE,
            unavailable={k: "no completed one-minute bars" for k in
                         ("price", "vwap", "ema9", "ema21", "volume",
                          "volume_expansion", "range_high")},
            error="CLOSED_BAR_UNAVAILABLE", closed_bar_only=closed_bar_only)

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
            error=f"realtime feed is {feed_status}",
            closed_bar_only=closed_bar_only)

    # Carried into the snapshot, not just logged. A restart gap makes
    # our summed volume a lower bound on the session's real volume, and
    # whoever reads a READY decision afterwards needs to know that from
    # the decision itself.
    cross_check = accumulator.volume_cross_check()

    closes = [b.close for b in bars]
    volumes = [b.volume for b in bars]
    price = closes[-1]
    asof = bars[-1].market_data_asof

    unavailable = {}

    # VWAP from THIS session's trades. Never TAMT/TVOL: those are KIS's
    # cumulative counters and they were measured to cover a wider window
    # than a collector that joined mid-session, so using them would
    # silently mix in prints from before we were listening.
    closed_volume = sum(b.volume for b in bars)
    vwap = (sum(float(getattr(b, "price_volume", 0.0)) for b in bars) /
            closed_volume) if closed_volume > 0 else None
    if vwap is None:
        unavailable["vwap"] = "no volume to weight by"

    total_volume = closed_volume
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
    # A gap INSIDE the window these features are computed over makes the
    # volume a lower bound where it matters. Expansion is a ratio of one
    # bar's volume to the average of the preceding ones, so trades we did
    # not hear deflate the denominator, inflate the ratio, and push a
    # candidate towards READY for a reason that is an artefact of our own
    # downtime. That is worse than not trading.
    #
    # Only the window matters, not the session: once the gap has aged out
    # of the bars being compared, the comparison is sound again and this
    # recovers on its own.
    window_bars = bars[-(average_window + 1):]
    gap_detected = _gap_overlaps(store, window_bars)

    volume_expansion = None
    if gap_detected:
        unavailable["volume_expansion"] = DATA_INCOMPLETE
    elif len(volumes) >= 2:
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
    from scanners.base import session_range as srange

    session_date = srange.current_session_date(session, moment)
    origin_et = srange.official_origin(session, session_date)
    official = origin_et.astimezone(timezone.utc) if origin_et else None
    # Every S6 session anchors on its OWN canonical open, not on the first
    # bar that happens to exist. A session with no window in the table has
    # no official origin to require.
    strict_origin = srange.window_for(session) is not None and closed_bar_only
    # Coverage is a BAR at or before the open, never a claim about when a
    # collector says it started listening. A long-lived collector whose
    # snapshot rolled at midnight reports coverage_started_at from 20:00
    # while holding no bar before 00:30; trusting that produced an empty
    # opening range and a plain WATCHING state, which reads as "no
    # breakout yet" rather than "we cannot see this session's open".
    coverage_started = (getattr(store, "coverage_started_at", None)
                        or getattr(store, "connected_at", None))
    origin_covered = bool(official is not None and bars[0].minute <= official)
    coverage_claimed = bool(official is not None and coverage_started is not None
                            and coverage_started <= official)
    if strict_origin and not origin_covered:
        unavailable["range_high"] = "OFFICIAL_ORIGIN_NOT_COVERED"
        if coverage_claimed:
            unavailable["origin_coverage_claim"] = (
                "collector reported coverage from "
                f"{coverage_started.isoformat()} but holds no bar at or before "
                f"{official.isoformat()}")
        return SessionFeatures(
            symbol=symbol, session=session, built_at=moment,
            market_data_asof=asof, price=price, vwap=vwap,
            ema9=ema9, ema21=ema21, volume=total_volume,
            volume_status=volume_status, volume_expansion=volume_expansion,
            bar_count=len(bars), price_source=SOURCE, volume_source=SOURCE,
            feed_status=feed_status, unavailable=unavailable,
            error="OFFICIAL_ORIGIN_NOT_COVERED", range_minutes=int(range_minutes),
            range_origin_timestamp=official, closed_bar_only=closed_bar_only)
    origin = official if strict_origin else bars[0].minute
    cutoff = origin + timedelta(minutes=int(range_minutes))
    opening = ([b for b in bars if origin <= b.minute < cutoff]
               if strict_origin else bars[:int(range_minutes)])
    post = [b for b in bars if b.minute >= cutoff] if strict_origin else bars[len(opening):]
    if strict_origin and not opening:
        # The origin is covered but its first `range_minutes` hold no
        # bars. Same canonical failure, stated rather than shown as an
        # ordinary "not ready".
        unavailable["range_high"] = "OFFICIAL_ORIGIN_NOT_COVERED"
        return SessionFeatures(
            symbol=symbol, session=session, built_at=moment,
            market_data_asof=asof, price=price, vwap=vwap,
            ema9=ema9, ema21=ema21, volume=total_volume,
            volume_status=volume_status, volume_expansion=volume_expansion,
            bar_count=len(bars), price_source=SOURCE, volume_source=SOURCE,
            feed_status=feed_status, unavailable=unavailable,
            error="OFFICIAL_ORIGIN_NOT_COVERED", range_minutes=int(range_minutes),
            range_origin_timestamp=official, closed_bar_only=closed_bar_only)
    range_high = max((b.high for b in opening), default=None)
    range_low = min((b.low for b in opening), default=None)
    opening_mean = (sum(b.volume for b in opening) / len(opening)) if opening else None
    post_mean = (sum(b.volume for b in post) / len(post)) if post else None
    scanner_expansion = (post_mean / opening_mean
                         if opening_mean not in (None, 0) and post_mean is not None
                         else None)
    extension_pct = None
    if range_high and range_low is not None and range_high > 0:
        extension_pct = (price - range_high) / range_high * 100.0

    quality = _entry_quality(bars, symbol=symbol, session=session,
                             range_minutes=range_minutes, now=moment,
                             vwap=vwap, ema9=ema9, ema21=ema21,
                             range_origin_timestamp=origin,
                             origin_covered=origin_covered,
                             closed_bar_only=closed_bar_only)
    return SessionFeatures(
        symbol=symbol, session=session, built_at=moment,
        market_data_asof=asof, price=price, vwap=vwap,
        ema9=ema9, ema21=ema21,
        volume=total_volume, volume_status=volume_status,
        volume_expansion=volume_expansion,
        scanner_volume_expansion=scanner_expansion,
        range_high=range_high, range_low=range_low,
        extension_pct=extension_pct, bar_count=len(bars),
        price_source=SOURCE, volume_source=SOURCE, feed_status=feed_status,
        volume_cross_check=cross_check, gap_detected=gap_detected,
        unavailable=unavailable, range_minutes=int(range_minutes),
        range_origin_timestamp=origin, closed_bar_only=closed_bar_only,
        entry_quality=quality)


def _entry_quality(bars, *, symbol, session, range_minutes, now, vwap, ema9, ema21,
                   range_origin_timestamp=None, origin_covered=None,
                   closed_bar_only=False):
    """The freshness snapshot from the collected bars. Never raises."""
    try:
        from s6_live import entry_quality as eq
        from scanners.base import session_range as srange

        simple = eq.bars_from_store(bars)
        if not simple:
            return None
        return eq.compute(
            simple, symbol=symbol, session=session, orb_minutes=range_minutes,
            now=now, provider=SOURCE, vwap=vwap, ema9=ema9, ema21=ema21,
            scanner_variant=f"S6_ORB{int(range_minutes)}",
            range_origin_timestamp=range_origin_timestamp,
            origin_covered=origin_covered,
            require_official_origin=(srange.window_for(session) is not None),
            closed_bar_only=closed_bar_only,
            baseline=eq.time_bucket_baseline)
    except Exception:  # noqa: BLE001
        logger.debug("entry quality unavailable for %s", symbol, exc_info=True)
        return None


#: The session whose collector process is still running when this one
#: opens.  `run_realtime_bar_collector` resolves its session ONCE and then
#: holds it for `--seconds 3600`, so the first up-to-an-hour of every
#: session -- which is exactly where its opening range lives -- is written
#: into the PRECEDING session's snapshot file.
PRECEDING_SESSION = {
    "PREMARKET": "OVERNIGHT_DAYTIME",
    "REGULAR": "PREMARKET",
    "AFTER_HOURS": "REGULAR",
    "OVERNIGHT_DAYTIME": "AFTER_HOURS",
}

#: Parsed snapshots, keyed by path and invalidated by (mtime, size).
#: `realtime_features.build` loads the store once PER SYMBOL, so a 41-name
#: tick re-parsed the same 7 MB file 41 times -- 6 to 9 seconds measured,
#: against a 30-second pretrade budget. Reading a session's bars from more
#: than one file would have tripled that.
_SNAPSHOT_CACHE = {}
_SNAPSHOT_CACHE_LIMIT = 8


def _read_snapshot(path):
    """A parsed snapshot payload, or None. Cached on (mtime, size)."""
    try:
        stat = path.stat()
    except OSError:
        return None
    key = str(path)
    stamp = (stat.st_mtime_ns, stat.st_size)
    hit = _SNAPSHOT_CACHE.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    try:
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.warning("could not read realtime bars at %s", path, exc_info=True)
        return None
    if len(_SNAPSHOT_CACHE) >= _SNAPSHOT_CACHE_LIMIT:
        _SNAPSHOT_CACHE.clear()
    _SNAPSHOT_CACHE[key] = (stamp, payload, stat.st_mtime)
    return payload


def _snapshot_written_at(path):
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return None


def _bars_root(env=None):
    import os
    from pathlib import Path

    mapping = env if env is not None else os.environ
    root = (mapping.get("REALTIME_BAR_DIR")
            or mapping.get("SCANNER_DATA_ROOT")
            or "/home/ubuntu/releases/us-stock-trading/shared/scanner")
    return Path(root) / "realtime_bars"


def session_window(session, session_date):
    """[open, close) for one session, in UTC. None when unknown."""
    from datetime import timedelta

    from scanners.base import session_range as srange

    window = srange.window_for(session)
    origin = srange.official_origin(session, session_date)
    if window is None or origin is None:
        return None, None
    end_day = session_date + timedelta(days=1) if srange.wraps_midnight(session) else session_date
    end = datetime.combine(end_day, window[1], tzinfo=origin.tzinfo)
    return origin.astimezone(timezone.utc), end.astimezone(timezone.utc)


def load_store(session, trading_day, *, env=None, stale_after_seconds=None,
               session_date=None):
    """This session's collected bars, or None if there are none.

    A session's bars are the bars that FELL INSIDE ITS WINDOW, not the
    bars that happen to sit in the file named after it. Those are not the
    same set: the collector labels a snapshot with the session that was
    current when its process started and keeps that label for an hour, so
    on 2026-09-08 the PREMARKET file began at 04:40 and the session's own
    04:00 opening range sat in the OVERNIGHT_DAYTIME file. Measured
    against the real production snapshots, that left 0 of 58 symbols with
    a constructible ORB5 range.

    So this reads the session's own file AND the preceding session's, for
    the session-start date and -- for a window that wraps midnight -- the
    following date too, keeps only bars inside [open, close), and merges
    them per symbol. Bars are keyed by minute, so the merge deduplicates
    by construction and no minute is counted twice. Absolute UTC
    timestamps stay canonical and the official origin is unchanged: this
    makes the origin's bars READABLE, it does not move the origin.

    Returns None on anything unreadable. The caller then falls back to
    its existing provider, which is the behaviour that was in place
    before this layer existed; raising here would take down a cycle over
    a data file.
    """
    from datetime import date as _date, timedelta

    root = _bars_root(env)
    if session_date is None:
        try:
            session_date = _date.fromisoformat(str(trading_day))
        except (TypeError, ValueError):
            return None
    start, end = session_window(session, session_date)

    from scanners.base import session_range as srange

    days = [session_date]
    # A wrapping session is persisted under BOTH date keys -- not because
    # its UTC start and end differ (20:00 ET and 04:00 ET share a UTC
    # date), but because the writer's own day key changes underneath it.
    if srange.wraps_midnight(session):
        days.append(session_date + timedelta(days=1))
    # The day key is not stable inside a wrapping session either:
    # `operational_trading_day` answers None at 20:05 ET and the next
    # trading day by 23:50, so the same session is persisted under two.
    labels = [session, PRECEDING_SESSION.get(str(session).upper())]

    payloads = []
    written = []
    for day in days:
        for label in labels:
            if not label:
                continue
            path = root / f"{day.isoformat()}-{label}.json"
            payload = _read_snapshot(path)
            if payload is None:
                continue
            payloads.append(payload)
            stamp = _snapshot_written_at(path)
            if stamp is not None:
                written.append(stamp)
    if not payloads:
        return None

    kwargs = {}
    if stale_after_seconds is not None:
        kwargs["stale_after_seconds"] = stale_after_seconds
    merged = rb.RealtimeBarStore(**kwargs)
    coverage = []
    for payload in payloads:
        part = rb.RealtimeBarStore.restore(payload, **kwargs)
        if part.coverage_started_at is not None:
            coverage.append(part.coverage_started_at)
        for (symbol, _label), accumulator in part._accumulators.items():
            kept = {minute: bar for minute, bar in accumulator.bars.items()
                    if start is None or start <= minute < end}
            if not kept:
                continue
            key = (symbol, session)
            target = merged._accumulators.get(key)
            if target is None:
                target = rb.SessionAccumulator(symbol=symbol, session=session)
                merged._accumulators[key] = target
            target.bars.update(kept)
    if not merged._accumulators:
        return None
    for accumulator in merged._accumulators.values():
        _reaggregate(accumulator)

    # The earliest point ANY contributing snapshot can prove it was
    # listening. It is only ever used to explain a gap; `_entry_quality`
    # and the origin check below require a real bar at the open.
    merged.coverage_started_at = min(coverage) if coverage else None
    if written:
        merged.snapshot_written_at = max(written)
    return merged


def _reaggregate(accumulator):
    """Recompute the per-session totals from the bars actually retained.

    Trimming to the session window changes the totals, and a VWAP built
    from one set of bars over another set's volume is not a VWAP.
    """
    bars = list(accumulator.bars.values())
    accumulator.volume = sum(b.volume for b in bars)
    accumulator.price_volume = sum(float(getattr(b, "price_volume", 0.0)) for b in bars)
    accumulator.trade_count = sum(int(b.trade_count) for b in bars)
    stamps = [b.last_trade_at for b in bars if b.last_trade_at is not None]
    accumulator.last_trade_at = max(stamps) if stamps else None
    # KIS's own cumulative counters belong to trades we may just have
    # dropped, so the cross-check is withdrawn rather than misreported.
    accumulator.first_cumulative = None
    accumulator.last_cumulative = None
    accumulator.first_amount = None
    accumulator.last_amount = None
