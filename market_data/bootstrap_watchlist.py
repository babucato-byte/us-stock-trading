"""Which symbols to stream before any candidate exists.

The circular dependency this removes
------------------------------------
The collector took its watchlist from the current session's published
candidates. For premarket that cannot work: discovering a premarket
candidate needs premarket data, and premarket data is what the collector
supplies. The result was a collector that declined to start every five
minutes, a scanner that rejected 593 of 593 symbols for DATA_ERROR, and
a session that looked like it had nothing to trade when nothing had been
measured.

So the bootstrap pool is chosen from information that exists BEFORE the
session opens, and never from the session's own output.

Why it is small
---------------
One appkey streams at most `MAX_SUBSCRIPTIONS` symbols -- 41, measured.
A six-hundred symbol universe cannot be watched, so this is not a
filter that could be relaxed if we wanted more coverage; it is a hard
ceiling, and the pool has to be chosen well rather than chosen wide.

What it uses, in order
----------------------
1. The prior session's strongest candidates. A name that broke out into
   the close is the name most likely to gap, and it is already ranked by
   the same scanner that would rank it tomorrow.
2. The universe manifest, filtered statically -- common stock, eligible,
   tradable. No realtime data is consulted, which is the whole point.

Both, because either alone is wrong. Prior candidates alone would follow
yesterday's story and never see this morning's gap; the manifest alone
would spend all 41 slots on whatever happens to sort first.
"""

import logging
from typing import List, Optional, Sequence, Tuple

from market_data import kis_hdfscnt0 as wire

logger = logging.getLogger(__name__)

#: How much of the pool the prior session's candidates may claim. The
#: rest is left for names that had no reason to be interesting
#: yesterday, because a gap is exactly the thing yesterday did not know
#: about.
PRIOR_SESSION_SHARE = 0.5

SOURCE_PRIOR = "PRIOR_SESSION_CANDIDATES"
SOURCE_MANIFEST = "UNIVERSE_MANIFEST"


def _exchange_for(symbol):
    try:
        from market_data.exchange_registry import build_kis_instrument

        instrument, _record = build_kis_instrument(symbol)
        return getattr(instrument, "exchange", None) or "NAS"
    except Exception:  # noqa: BLE001 - a symbol we cannot address is a
        # symbol we cannot stream; skipping it is the whole handling.
        return None


def prior_session_symbols(*, trading_day, session, limit) -> List[str]:
    """The previous session's ranked candidates, best first."""
    try:
        from scanners.publish import candidates as publisher

        rows = publisher.read(trading_day, session) or []
    except Exception:  # noqa: BLE001
        logger.warning("bootstrap: could not read %s candidates for %s",
                       session, trading_day, exc_info=True)
        return []
    ranked = sorted(rows, key=lambda r: (int(r.get("rank") or 10 ** 6),
                                         str(r.get("symbol") or "")))
    out, seen = [], set()
    for row in ranked:
        symbol = str(row.get("symbol") or "").upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
        if len(out) >= limit:
            break
    return out


def manifest_symbols(*, limit, exclude=()) -> List[str]:
    """Statically eligible universe names, no realtime data consulted."""
    try:
        import os
        from datetime import datetime, timezone
        from pathlib import Path

        from discovery import manifest as manifest_module
        from market_hours import us_trading_day
        from scanners.runner import MANIFEST_DEFAULT_PATH

        # Derived the way the scanner cron derives it -- from the shared
        # candidate directory -- not from the module default. That
        # default is a RELATIVE path, so it resolves against whatever
        # cwd the caller happens to have; the scanner runs from the
        # release root and the manifest lives beside the shared candidate
        # store, outside the release. Reading the default silently found
        # nothing and reported MISSING, which is indistinguishable from
        # "discovery has not run today".
        candidate_dir = os.environ.get("SCANNER_CANDIDATE_DIR")
        data_root = os.environ.get("SCANNER_DATA_ROOT")
        if candidate_dir:
            path = Path(candidate_dir).parent / "discovery" / "manifest.json"
        elif data_root:
            path = Path(data_root).parent / "state" / "discovery" / "manifest.json"
        else:
            path = Path(MANIFEST_DEFAULT_PATH)

        verdict = manifest_module.validate(
            manifest_module.read(str(path)),
            trading_day=us_trading_day(datetime.now(timezone.utc)))
        manifest_source = str(path)
        # PARTIAL is usable for the same reason the scanner accepts it:
        # part of today's market beats all of yesterday's. Anything else
        # yields nothing rather than a stale pool -- an unusable manifest
        # must not quietly seed the stream with names nobody re-derived.
        if verdict.get("status") not in (manifest_module.VALID,
                                         manifest_module.PARTIAL):
            logger.warning("bootstrap: manifest unusable (%s) at %s",
                           verdict.get("status"), manifest_source)
            return []
        entries = verdict.get("symbols") or ()
    except Exception:  # noqa: BLE001
        logger.warning("bootstrap: universe manifest unavailable",
                       exc_info=True)
        return []
    skip = {str(s).upper() for s in exclude}
    out = []
    for entry in entries or ():
        symbol = str(entry if isinstance(entry, str)
                     else (entry.get("symbol") or "")).upper()
        if not symbol or symbol in skip or symbol in out:
            continue
        out.append(symbol)
        if len(out) >= limit:
            break
    return out


def build(*, session, trading_day, prior_session=None, prior_trading_day=None,
          limit=None) -> Tuple[List[Tuple[str, str]], dict]:
    """(symbol, exchange) pairs to subscribe, and how they were chosen.

    Never consults the CURRENT session's candidates. That is the
    dependency this exists to break, and a "just in case" fallback to
    them would quietly restore it.
    """
    cap = int(limit or wire.MAX_SUBSCRIPTIONS)
    prior_cap = max(1, int(cap * PRIOR_SESSION_SHARE))

    prior = []
    if prior_session and prior_trading_day:
        prior = prior_session_symbols(trading_day=prior_trading_day,
                                      session=prior_session, limit=prior_cap)

    filler = manifest_symbols(limit=cap - len(prior), exclude=prior)

    pairs, chosen_from = [], {}
    for symbol, origin in ([(s, SOURCE_PRIOR) for s in prior]
                           + [(s, SOURCE_MANIFEST) for s in filler]):
        exchange = _exchange_for(symbol)
        if exchange is None:
            continue
        pairs.append((symbol, exchange))
        chosen_from[symbol] = origin
        if len(pairs) >= cap:
            break

    return pairs, {
        "session": session,
        "trading_day": trading_day,
        "cap": cap,
        "from_prior_session": sum(1 for v in chosen_from.values()
                                  if v == SOURCE_PRIOR),
        "from_manifest": sum(1 for v in chosen_from.values()
                             if v == SOURCE_MANIFEST),
        "total": len(pairs),
        "prior_session": prior_session,
        "prior_trading_day": prior_trading_day,
    }
