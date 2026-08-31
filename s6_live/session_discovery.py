"""Every session builds its own candidate pool, from nothing if it must.

The failure this removes
------------------------
On 2026-08-31 the Monday DAYTIME session opened with candidates=0, and
the reason was not a quiet market:

    manifest unusable (WRONG_TRADING_DAY: manifest says '2026-08-28')
    no active universe available; run the daily profile first
    universe fill: 0 of 300 eligible after 0 considered

Three producers had to have run for S6 to have anything to look at, and
none of them had. The daily profile builds the activity ranking after
the regular close; the manifest is dated to a session; the ranking
itself lives wherever the process that wrote it happened to point. A
session that opens before all of that has aligned trades nothing, and
waits hours for the next producer.

The store split that made it worse
----------------------------------
The daily profile runs from /home/ubuntu/trading and writes its ranking
to that tree's `logs/scanners/activity`. The S6 scanner runs from the
immutable release with SCANNER_ANALYTICS_DIR pointed at shared state, and
read an empty directory two paths away from a 2MB ranking that was
sitting there the whole time. "No active universe" was true of the
directory and false of the system.

So the ranking is looked for in more than one place, and WHICH one
answered is reported rather than assumed.

What this is not
----------------
It is not a strategy gate and it grants nothing. It decides which
symbols are worth watching; every one of them still faces ORB15, the
close-breakout test, VWAP, EMA9>EMA21, 1.2x volume expansion and the
extension ceiling, unchanged. A session that genuinely has no candidates
still reports zero -- with the reason it was zero, which is the part
that was missing.
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Why a pool came back empty. "candidates=0" alone sent people looking
#: for a market explanation of a configuration problem.
NO_UNIVERSE = "NO_UNIVERSE"
NO_ACTIVITY_RANKING = "NO_ACTIVITY_RANKING"
ACTIVITY_RANKING_STALE = "ACTIVITY_RANKING_STALE"
NO_ELIGIBLE_SYMBOLS = "NO_ELIGIBLE_SYMBOLS"
DISCOVERY_FAILED = "DISCOVERY_FAILED"
OK = "OK"

#: Where a source came from, so a pool built entirely on last week's
#: ranking is not mistaken for one built on this morning's.
SOURCE_HELD = "HELD_POSITION"
SOURCE_PRIOR = "PRIOR_SESSION"
SOURCE_COARSE = "COARSE_DISCOVERY"


def activity_search_paths(env=None) -> List[Path]:
    """Every location an activity ranking might legitimately live in.

    Ordered: the explicitly configured one, then shared state, then the
    legacy project tree the daily profile still writes to. The legacy
    entry is a bridge, not a design -- it is here because the ranking is
    genuinely there and a session that refuses to read it trades
    nothing, and it is listed last so a properly-placed store always
    wins.
    """
    env = env if env is not None else os.environ
    found = []
    explicit = env.get("SCANNER_ACTIVITY_DIR")
    if explicit:
        found.append(Path(explicit))
    analytics = env.get("SCANNER_ANALYTICS_DIR")
    if analytics:
        found.append(Path(analytics) / "activity")
    data_root = env.get("SCANNER_DATA_ROOT")
    if data_root:
        found.append(Path(data_root) / "logs" / "scanners" / "activity")
    legacy = env.get("SCANNER_LEGACY_ANALYTICS_DIR")
    if legacy:
        found.append(Path(legacy) / "activity")
    return found


def locate_activity_store(provider="yfinance", *, env=None):
    """The first readable ranking, and where it came from.

    Returns (path, records) or (None, None). Reporting the path is the
    point: two directories claiming to be the activity store is how a
    populated ranking went unread.
    """
    import json

    for directory in activity_search_paths(env=env):
        candidate = directory / f"{provider}.json"
        try:
            if not candidate.exists():
                continue
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - an unreadable store is not the
            # only store; keep looking rather than failing the session.
            logger.warning("activity store at %s is unreadable", candidate,
                           exc_info=True)
            continue
        symbols = payload.get("symbols") if isinstance(payload, dict) else None
        if symbols:
            return candidate, payload
    return None, None


def _ranked_symbols(payload, *, limit) -> List[str]:
    """Symbols in descending dollar volume.

    Dollar volume rather than share volume: a penny stock trading
    millions of shares is not more worth a realtime slot than a liquid
    name, and share count alone would fill the pool with the former.
    """
    symbols = (payload or {}).get("symbols") or {}
    scored = []
    for symbol, record in symbols.items():
        if not isinstance(record, dict):
            continue
        value = record.get("dollar_volume")
        if not isinstance(value, (int, float)):
            continue
        scored.append((float(value), str(symbol).upper()))
    scored.sort(reverse=True)
    return [symbol for _value, symbol in scored[:max(0, int(limit))]]


def execution_priority(ranked, prices, *, orderable_usd) -> List[str]:
    """Activity order, with names the account can actually buy first.

    This does NOT change any strategy score and does NOT remove a symbol
    from the universe. It decides which names are worth one of 41
    realtime slots, and a name the account cannot buy a single share of
    can only ever produce INSUFFICIENT_CASH -- it would occupy a slot to
    prove that.

    Both partitions keep their activity order, and the unaffordable ones
    are kept, after. Prices move, the orderable amount changes when a
    position closes, and a symbol dropped outright could not come back.

    With no orderable figure the order is unchanged: guessing
    affordability from a stale number would reorder the pool on
    something less reliable than the ranking it is reordering.
    """
    try:
        budget = float(orderable_usd)
    except (TypeError, ValueError):
        return list(ranked)
    if budget <= 0:
        return list(ranked)

    affordable, beyond = [], []
    for symbol in ranked:
        price = prices.get(symbol)
        if isinstance(price, (int, float)) and 0 < float(price) <= budget:
            affordable.append(symbol)
        else:
            beyond.append(symbol)
    return affordable + beyond


def _prices(payload) -> Dict[str, float]:
    out = {}
    for symbol, record in ((payload or {}).get("symbols") or {}).items():
        if isinstance(record, dict) and isinstance(record.get("price"),
                                                   (int, float)):
            out[str(symbol).upper()] = float(record["price"])
    return out


def coarse_pool(*, limit, provider="yfinance", env=None,
                eligibility=None, orderable_usd=None) -> Dict[str, Any]:
    """Tier1: the ranked shortlist this session can watch.

    Depends on NOTHING produced by another session. Given a ranking and
    an eligibility view it returns a pool; given neither it returns an
    empty pool and says which was missing.
    """
    path, payload = locate_activity_store(provider, env=env)
    if payload is None:
        return {"symbols": [], "reason": NO_ACTIVITY_RANKING,
                "activity_store": None, "considered": 0}

    ranked = _ranked_symbols(payload, limit=limit * 3 if limit else 0)
    considered = len(ranked)
    if eligibility is not None:
        kept = []
        for symbol in ranked:
            try:
                if eligibility.should_skip(symbol):
                    continue
            except Exception:  # noqa: BLE001 - an unreadable eligibility
                # view must not empty the pool; the strategy gates still
                # apply to everything in it.
                pass
            kept.append(symbol)
        ranked = kept

    prices = _prices(payload)
    ordered = execution_priority(ranked, prices, orderable_usd=orderable_usd)
    ordered = ordered[:max(0, int(limit))]
    return {
        "symbols": ordered,
        "affordable": sum(1 for s in ordered
                          if isinstance(prices.get(s), (int, float))
                          and isinstance(orderable_usd, (int, float))
                          and 0 < prices[s] <= orderable_usd),
        "orderable_usd": orderable_usd,
        "reason": OK if ordered else NO_ELIGIBLE_SYMBOLS,
        "activity_store": str(path) if path else None,
        "activity_updated_at": (payload or {}).get("updated_at"),
        "considered": considered,
    }


def build_pool(*, session, operational_trading_day, limit,
               held_symbols=(), prior_symbols=(), env=None,
               eligibility=None, provider="yfinance",
               orderable_usd=None) -> Dict[str, Any]:
    """The session's whole watch pool, in priority order.

    Held positions first and unconditionally -- they are obligations, and
    a slot for one is not a choice. Then anything the previous session
    already knew about, then coarse discovery. The third is what makes
    the session independent: with the first two empty it still returns a
    pool.
    """
    try:
        ordered: List[str] = []
        provenance: Dict[str, str] = {}

        def _add(symbols, source):
            for symbol in symbols or ():
                name = str(symbol or "").upper()
                if not name or name in provenance:
                    continue
                ordered.append(name)
                provenance[name] = source

        _add(held_symbols, SOURCE_HELD)
        _add(prior_symbols, SOURCE_PRIOR)

        remaining = max(0, int(limit) - len(ordered))
        coarse = coarse_pool(limit=remaining, provider=provider, env=env,
                             eligibility=eligibility,
                             orderable_usd=orderable_usd)
        _add(coarse["symbols"], SOURCE_COARSE)

        reason = OK
        if not ordered:
            reason = coarse["reason"]

        return {
            "session": session,
            "operational_trading_day": operational_trading_day,
            "symbols": ordered[:max(0, int(limit))],
            "provenance": provenance,
            "reason": reason,
            "from_held": sum(1 for s in provenance.values() if s == SOURCE_HELD),
            "from_prior": sum(1 for s in provenance.values() if s == SOURCE_PRIOR),
            "from_coarse": sum(1 for s in provenance.values()
                               if s == SOURCE_COARSE),
            "activity_store": coarse.get("activity_store"),
            "activity_updated_at": coarse.get("activity_updated_at"),
            "coarse_considered": coarse.get("considered", 0),
            "affordable": coarse.get("affordable"),
            "orderable_usd": coarse.get("orderable_usd"),
        }
    except Exception:  # noqa: BLE001 - discovery failing is a reportable
        # outcome, not a crash that takes the session with it.
        logger.warning("session discovery failed for %s", session,
                       exc_info=True)
        return {"session": session,
                "operational_trading_day": operational_trading_day,
                "symbols": [], "provenance": {}, "reason": DISCOVERY_FAILED,
                "from_held": 0, "from_prior": 0, "from_coarse": 0,
                "activity_store": None, "coarse_considered": 0}


def describe(pool) -> str:
    """One line for the funnel, naming the reason when it is empty."""
    return (
        "[S6 DISCOVERY] session=%s day=%s pool=%d held=%d prior=%d coarse=%d "
        "considered=%d store=%s reason=%s" % (
            pool.get("session"), pool.get("operational_trading_day"),
            len(pool.get("symbols") or ()), pool.get("from_held", 0),
            pool.get("from_prior", 0), pool.get("from_coarse", 0),
            pool.get("coarse_considered", 0),
            pool.get("activity_store") or "-", pool.get("reason")))
