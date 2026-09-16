"""Fetch the opening minutes the stream missed, and merge them in.

Why a backfill rather than a fallback
-------------------------------------
The collector is restarted AT the session boundary, so it connects a
few seconds after the official open -- measured 19.85s on 2026-09-16.
Those seconds are not recoverable from the stream, and a symbol whose
first print lands in them has no bar at the origin. `kis_bar_features`
then refuses the whole range, correctly: it cannot tell a quiet minute
from an unwatched one.

The REST chart CAN tell. It serves the same venue's completed minutes
after the fact, so the missing opening window is fetchable -- and the
rest of the session, which the stream has in full and at higher
fidelity, does not need replacing to get it.

So this fetches only the opening window, merges it UNDER the stream's
own bars, and leaves everything else alone. `market_data.bar_merge`
already owns that precedence: a minute the stream observed completely
wins, because REST is a summary of the same trades and can only agree
or be staler.

What it refuses to do
---------------------
One chart call returns at most `BARS_PER_CALL` minutes ending at now.
Late in a session the origin is simply outside that reach, and there is
no second call that would extend it. That is reported as its own reason
-- `ORIGIN_OUT_OF_REACH` -- and never as a covered origin. An origin
this cannot restore leaves the features exactly as it found them.
"""

import logging
from datetime import timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

BACKFILLED = "ORIGIN_BACKFILLED"
OUT_OF_REACH = "ORIGIN_OUT_OF_REACH"
NO_ROWS = "ORIGIN_REST_EMPTY"
NO_ORIGIN = "NO_OFFICIAL_ORIGIN"
NO_BROKER = "NO_BROKER"
FAILED = "ORIGIN_REST_FAILED"


def _exchange_for(symbol, store, session):
    """The venue this symbol trades on.

    From `exchange_registry`, which is the authority the rest of the KIS
    path already uses. An earlier version of this read the venue off the
    store's accumulator, which does not carry one -- `SessionAccumulator`
    holds symbol, session and bars and nothing about where they came
    from -- so every lookup returned None and every backfill failed with
    "no exchange mapping" before it reached the wire. The unit tests all
    passed an explicit `exchange` and never exercised this path; it was
    the live log that showed it, on HPE, MRNA, STX, GS and LRCX.
    """
    try:
        from market_data.exchange_registry import resolve_exchange

        record = resolve_exchange(symbol)
    except Exception:  # noqa: BLE001 - an unresolvable symbol is a reason
        record = None
    venue = getattr(record, "exchange", None) or record
    if venue is None:
        return None
    # `USExchange.NASDAQ` stringifies to "USExchange.NASDAQ", which the
    # KIS code table does not know. Its VALUE is the spelling the rest of
    # the path uses, and `excd_for_session` accepts either that or the
    # enum -- never the repr.
    return str(getattr(venue, "value", None) or getattr(venue, "name", None)
               or venue) or None


def restore_origin(store, symbol, *, session, official_origin, range_minutes,
                   broker=None, exchange=None, now=None, chart=None) -> Dict[str, Any]:
    """Fetch `[origin, origin + range_minutes)` and merge it into `store`.

    Returns a report; the store is mutated only on success. Never raises
    for one symbol -- an origin that cannot be restored is a reason, not
    an exception, because the caller's next move is to leave the refusal
    standing either way.
    """
    if official_origin is None:
        return {"status": NO_ORIGIN, "symbol": symbol, "bars_added": 0}
    if broker is None:
        return {"status": NO_BROKER, "symbol": symbol, "bars_added": 0}

    module = chart
    if module is None:
        from market_data import kis_minute_chart as module  # noqa: N813

    venue = exchange or _exchange_for(symbol, store, session)
    if not venue:
        return {"status": FAILED, "symbol": symbol, "bars_added": 0,
                "detail": "no exchange mapping for this symbol"}

    cutoff = official_origin + timedelta(minutes=int(range_minutes))
    try:
        records = module.fetch(broker, symbol=symbol, exchange=venue,
                               session=session)
    except Exception as exc:  # noqa: BLE001 - one symbol, not the sweep
        logger.warning("ORIGIN_BACKFILL_FAILED symbol=%s error=%s",
                       symbol, type(exc).__name__)
        return {"status": FAILED, "symbol": symbol, "bars_added": 0,
                "detail": f"{type(exc).__name__}: {str(exc)[:120]}"}

    if not records:
        return {"status": NO_ROWS, "symbol": symbol, "bars_added": 0}

    stamps = [r["at"] for r in records if r.get("at") is not None]
    if not stamps:
        return {"status": NO_ROWS, "symbol": symbol, "bars_added": 0}

    # The reach test, and why it is about the OLDEST row.
    #
    # One call returns the most recent N minutes. If its oldest row is
    # already past the origin then the origin is behind the window and
    # no amount of parsing gets it back -- saying so is the whole point,
    # because the alternative is a range built from a later minute
    # wearing the origin's name.
    oldest = min(stamps)
    if oldest > official_origin:
        logger.warning(
            "ORIGIN_OUT_OF_REACH symbol=%s origin=%s oldest_rest_bar=%s "
            "rows=%d -- one chart call cannot reach back this far",
            symbol, official_origin.isoformat(), oldest.isoformat(),
            len(records))
        return {"status": OUT_OF_REACH, "symbol": symbol, "bars_added": 0,
                "oldest_rest_bar": oldest.isoformat(),
                "official_origin": official_origin.isoformat(),
                "rows": len(records), "bars_per_call": module.BARS_PER_CALL}

    bars = module.to_bars(records, symbol=symbol, session=session)
    window = [b for b in bars if official_origin <= b.minute < cutoff]
    if not window:
        # The origin is within reach and the venue published nothing in
        # the opening window. That is a measured quiet market, not a
        # coverage gap, but it is also not a range -- reported as its
        # own answer rather than dressed up as either.
        return {"status": NO_ROWS, "symbol": symbol, "bars_added": 0,
                "detail": "REST reached the origin and holds no bar in the "
                          "opening window"}

    added = _merge_into(store, symbol, session, window)
    marker = getattr(store, "origin_backfilled", None)
    if marker is None:
        marker = set()
        try:
            store.origin_backfilled = marker
        except Exception:  # noqa: BLE001
            marker = None
    if marker is not None:
        marker.add(symbol)
    logger.info("ORIGIN_BACKFILLED symbol=%s origin=%s bars_added=%d",
                symbol, official_origin.isoformat(), added)
    return {"status": BACKFILLED, "symbol": symbol, "bars_added": added,
            "official_origin": official_origin.isoformat()}


def _merge_into(store, symbol, session, rest_bars) -> int:
    """Add REST bars for minutes the stream does not already hold.

    The stream wins every minute it has. `bar_merge` states the rule and
    the reason; this applies it to one accumulator without rebuilding
    the session.
    """
    from market_data.realtime_bars import SessionAccumulator

    key = (symbol, session)
    accumulator = store._accumulators.get(key)
    if accumulator is None:
        accumulator = SessionAccumulator(symbol=symbol, session=session)
        store._accumulators[key] = accumulator

    added = 0
    for bar in rest_bars:
        if bar.minute in accumulator.bars:
            continue  # the stream saw this minute; it is the better copy
        if not getattr(bar, "price_volume", None):
            try:
                typical = (float(bar.high) + float(bar.low) + float(bar.close)) / 3.0
                object.__setattr__(bar, "price_volume", typical * float(bar.volume))
            except Exception:  # noqa: BLE001
                pass
        accumulator.bars[bar.minute] = bar
        added += 1

    if added:
        merged = list(accumulator.bars.values())
        accumulator.volume = sum(float(b.volume or 0.0) for b in merged)
        accumulator.price_volume = sum(
            float(getattr(b, "price_volume", 0.0) or 0.0) for b in merged)
        accumulator.trade_count = max(accumulator.trade_count, len(merged))
        stamps = [b.last_trade_at for b in merged if b.last_trade_at]
        if stamps:
            accumulator.last_trade_at = max(stamps)
    return added
