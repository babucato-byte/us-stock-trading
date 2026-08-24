"""Names today made liquid that yesterday's ranking could not know about.

The problem
-----------
The intraday universe is ranked by the PREVIOUS trading day's dollar
volume, refreshed by the daily profile at 16:17 ET. On a Monday that
ranking is Friday's -- correct by construction, since the profile can
only run after a close, but blind to a name whose volume exploded this
morning. MARA sat at Friday's rank 306 while trading 21.8M shares on the
Monday.

What this is, and firmly is not
-------------------------------
It decides WHICH SYMBOLS TO LOOK AT. It is not a strategy gate and it
gives no symbol an easier path: a supplement name still faces ORB15, the
close-breakout test, VWAP, EMA9>EMA21, 1.2x volume expansion and the 6%
extension ceiling, unchanged and in the same order. Today's move is
never itself a reason to accept a candidate -- if it were, the scanner
would be buying the thing it should be measuring.

Why it is bounded the way it is
-------------------------------
The provider has no batch endpoint: one symbol is one round trip. A
market-wide sweep every 15 minutes would cost more fetches than the scan
it supplements, so this reads a WINDOW immediately below the cut --
where a name displaced by stale ranking actually sits -- and it runs at
most once per (trading day, session), cached on disk. Ranks 301-500 at
one daily bar each, four times a day, against a daily profile that
already walks 10,500 names after the close.

A symbol far outside that window is out of reach here, and deliberately
so: this closes the "stale by a day or a weekend" gap, not the "discover
any small cap in the market" gap, and claiming the second would be a
promise the fetch budget cannot keep.
"""

import json
import logging
from datetime import datetime, timezone
from typing import List, Optional

logger = logging.getLogger(__name__)

#: How many ranking positions BELOW the cut to consider. Wide enough to
#: contain a name displaced by a stale ranking (MARA was 6 places out),
#: narrow enough that one daily bar each is affordable once a session.
DEFAULT_WINDOW = 200

#: How many of those may join the scan universe.
#:
#: Zero -- the supplement is OPT-IN, enabled per profile with
#: `--supplement-size`. Defaulting it on would change what
#: `--active-pool-size` means for every caller at once, including the S1
#: and S2 scans, which is a wider blast radius than the problem
#: justifies: the stale-ranking gap was measured on S6. It is switched
#: on for the S6 cron alone until there is evidence for the rest.
DEFAULT_SUPPLEMENT_SIZE = 0

#: What the S6 scan asks for. Separate from the default so the number
#: that is actually deployed is stated once, here, rather than living
#: only in a cron line.
S6_SUPPLEMENT_SIZE = 50

#: A candidate must trade enough today to be worth a scanner's time. Not
#: a strategy threshold -- it is the same liquidity floor the ranking
#: itself applies, expressed in today's numbers instead of yesterday's.
DEFAULT_MIN_DOLLAR_VOLUME = 1_000_000.0

SUBDIR = "supplement"


def _cache_path(trading_day, session):
    from scanners.base.result_store import analytics_dir

    directory = analytics_dir() / SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                   for ch in str(session or "NONE"))
    return directory / f"{trading_day}-{safe}.json"


def window_candidates(activity_store, eligibility_store, *, already,
                      window=DEFAULT_WINDOW, cut=300, today=None) -> List[str]:
    """The eligible names just below the cut, in ranking order.

    `already` is the set the scan is covering; a symbol in it is not a
    candidate for supplementing itself.
    """
    depth = int(cut) + int(window)
    ranked = activity_store.active_symbols(limit=depth, today=today) or []
    covered = {str(s).upper() for s in already}
    out = []
    for symbol in ranked:
        upper = str(symbol).upper()
        if upper in covered:
            continue
        if eligibility_store.should_skip(symbol, today=today):
            continue
        out.append(symbol)
    return out


def _today_dollar_volume(provider, symbol, trading_day) -> Optional[float]:
    """Price x volume from TODAY's daily bar, or None.

    None rather than 0.0 when the bar is missing: a symbol nobody could
    price is not a symbol that did not trade, and ranking the two
    together would put unfetchable names at the bottom as though that
    were a measurement.
    """
    try:
        frame = provider.get_daily_bars(symbol, lookback_days=5)
    except Exception:  # noqa: BLE001 - one symbol must not end the sweep
        return None
    if frame is None or len(frame) == 0:
        return None
    try:
        row = frame.iloc[-1]
        stamp = str(frame.index[-1])[:10]
        if trading_day and stamp != str(trading_day):
            return None            # the last bar is not today's
        close = float(row.get("Close", row.get("close")))
        volume = float(row.get("Volume", row.get("volume")))
    except Exception:  # noqa: BLE001
        return None
    if not (close > 0 and volume > 0):
        return None
    return close * volume


def select(provider, candidates, *, trading_day,
           size=DEFAULT_SUPPLEMENT_SIZE,
           min_dollar_volume=DEFAULT_MIN_DOLLAR_VOLUME) -> List[str]:
    """The `size` candidates with the most dollar volume TODAY."""
    measured = []
    for symbol in candidates:
        value = _today_dollar_volume(provider, symbol, trading_day)
        if value is None or value < float(min_dollar_volume):
            continue
        measured.append((value, symbol))
    measured.sort(reverse=True)
    return [symbol for _value, symbol in measured[:max(0, int(size))]]


def load_or_build(provider, activity_store, eligibility_store, *,
                  trading_day, session, already, cut=300,
                  window=DEFAULT_WINDOW, size=DEFAULT_SUPPLEMENT_SIZE,
                  today=None) -> List[str]:
    """This session's supplement, computed at most once. Never raises.

    Cached per (trading day, session): the intraday scans run every 15
    minutes and recomputing this on each of them would multiply the
    fetch cost by the tick count for an answer that moves slowly. A
    failure returns an empty supplement -- the scan proceeds on the
    ranking alone, which is exactly today's behaviour.
    """
    if int(size) <= 0:
        return []
    try:
        path = _cache_path(trading_day, session)
        if path.exists():
            cached = json.loads(path.read_text(encoding="utf-8"))
            return list(cached.get("symbols") or [])

        candidates = window_candidates(activity_store, eligibility_store,
                                       already=already, window=window,
                                       cut=cut, today=today)
        chosen = select(provider, candidates, trading_day=trading_day,
                        size=size)
        path.write_text(json.dumps({
            "trading_day": str(trading_day), "session": session,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "considered": len(candidates), "symbols": chosen,
        }), encoding="utf-8")
        logger.info("intraday supplement: %s of %s window candidates added",
                    len(chosen), len(candidates))
        return chosen
    except Exception:  # noqa: BLE001 - a supplement failure is not a scan failure
        logger.warning("intraday supplement unavailable; continuing on the "
                       "previous-day ranking alone", exc_info=True)
        return []
