"""Three tiers, because the realtime budget is 41 and the universe is not.

The constraint that shapes everything
-------------------------------------
KIS allows 41 concurrent subscriptions on one appkey, measured, and one
connection per appkey. The tradeable universe is thousands of symbols.
So the only question this module answers is which 41 symbols are worth a
stream right now, and it answers it in stages because the stages have
genuinely different costs and different jobs.

  Tier0  the whole universe, cheaply. Its job is ELIMINATION -- deciding
         which symbols are not worth a realtime slot. It must never
         decide a BUY: it runs on coarse, often delayed data, and a
         decision made there would be made on exactly the data quality
         the realtime layer exists to replace.

  Tier1  a ranked shortlist. Its size is bounded by what the data source
         can actually be asked for, NOT by a round number -- a fixed
         150 or 200 written in without a reason is a number nobody can
         later defend or adjust.

  Tier2  the <=41 that get a stream, and the only tier where READY can
         be decided.

Lifecycle outranks rank, always
-------------------------------
A symbol we hold, are exiting, or have an order in flight for keeps its
slot regardless of how attractive it looks. Ranking is about opportunity;
these states are about obligation. Dropping an EXIT_PENDING symbol
because something scored higher would leave a real position unwatched
in order to look at a better one -- and the position still has to be
sold.
"""

import logging
from typing import Any, Dict, List, Optional, Sequence

from market_data import kis_hdfscnt0 as wire

logger = logging.getLogger(__name__)

TIER0 = "TIER0_COARSE"
TIER1 = "TIER1_SHORTLIST"
TIER2 = "TIER2_REALTIME"

#: The measured hard limit, not a chosen one.
REALTIME_BUDGET = wire.MAX_SUBSCRIPTIONS

#: Slot priority. Lower wins. The first four are LIFECYCLE states: a
#: position or an order exists, so the slot is an obligation rather than
#: a choice. Everything below is opportunity, ordered by how close the
#: candidate is to being actionable.
PRIORITY = {
    "EXIT_PENDING": 0,
    "SELL_SUBMITTED": 1,
    "OPEN": 2,
    "BUY_SUBMITTED": 3,
    "EXECUTABLE": 4,
    "READY_TO_BUY": 5,
    "WATCHING": 6,
    "WARMING_UP": 7,
}

#: States that may NEVER be evicted to make room, whatever the ranking
#: says. Ranking is about opportunity; these are about obligation.
NEVER_EVICT = frozenset({"EXIT_PENDING", "SELL_SUBMITTED", "OPEN",
                         "BUY_SUBMITTED"})

DEFAULT_PRIORITY = max(PRIORITY.values()) + 1


def priority_of(state) -> int:
    return PRIORITY.get(str(state or "").upper(), DEFAULT_PRIORITY)


def may_evict(state) -> bool:
    return str(state or "").upper() not in NEVER_EVICT


def select_tier2(candidates, *, budget=REALTIME_BUDGET) -> Dict[str, Any]:
    """Which symbols get a stream, and what was left out.

    `candidates` are dicts with `symbol`, `state` and an optional `rank`
    (lower is better). Sorted by lifecycle priority first, then rank,
    then symbol so the result is stable rather than dependent on dict
    ordering.

    Returns the admitted list AND the dropped one. A silent truncation
    reads afterwards as "everything was covered".
    """
    prepared = []
    for entry in candidates or ():
        symbol = str(entry.get("symbol") or "").upper()
        if not symbol:
            continue
        prepared.append({
            "symbol": symbol,
            "state": entry.get("state"),
            "rank": entry.get("rank"),
            "priority": priority_of(entry.get("state")),
        })

    # Deduplicate on symbol, keeping the strongest claim: the same
    # symbol can arrive as both a held position and a fresh candidate,
    # and the held claim is the one that matters.
    best: Dict[str, Dict[str, Any]] = {}
    for entry in prepared:
        current = best.get(entry["symbol"])
        if current is None or entry["priority"] < current["priority"]:
            best[entry["symbol"]] = entry

    ordered = sorted(
        best.values(),
        key=lambda e: (e["priority"],
                       e["rank"] if isinstance(e["rank"], (int, float))
                       else float("inf"),
                       e["symbol"]))

    admitted = ordered[:budget]
    dropped = ordered[budget:]

    # An obligation that could not be given a slot is not an ordinary
    # miss. It means more positions are held than the feed can watch,
    # and it must be visible rather than inferred from a short list.
    starved = [e for e in dropped if not may_evict(e["state"])]
    if starved:
        logger.error(
            "REALTIME_BUDGET_STARVED %d held/in-flight symbol(s) could not be "
            "subscribed: %s", len(starved),
            ", ".join(e["symbol"] for e in starved))

    return {
        "tier": TIER2,
        "budget": budget,
        "admitted": [e["symbol"] for e in admitted],
        "dropped": [e["symbol"] for e in dropped],
        "starved_obligations": [e["symbol"] for e in starved],
        "detail": admitted,
    }


def shortlist(ranked, *, limit) -> Dict[str, Any]:
    """Tier1: the ranked pool Tier2 draws from.

    `limit` is passed in rather than defaulted. What the shortlist can
    hold depends on what the data source can be asked for within its
    rate limit, and that belongs to whoever knows the source -- not to a
    constant here that would get copied around and outlive its reason.
    """
    if limit is None:
        raise ValueError(
            "Tier1 needs an explicit limit derived from the source's rate "
            "budget; a default here would be a number with no reason behind it")
    entries = [str(s).upper() for s in (ranked or ()) if s]
    return {
        "tier": TIER1,
        "limit": limit,
        "symbols": entries[:limit],
        "dropped": len(entries[max(0, limit):]),
    }


def coarse_eliminate(universe, *, keep) -> Dict[str, Any]:
    """Tier0: reduce the universe to something rankable.

    Elimination only. This tier never decides that a symbol IS a buy --
    it runs on coarse and often delayed data, and a buy decided there
    would rest on exactly the data quality the realtime layer exists to
    replace.
    """
    entries = [str(s).upper() for s in (universe or ()) if s]
    return {
        "tier": TIER0,
        "considered": len(entries),
        "survivors": entries[:keep],
        "eliminated": max(0, len(entries) - keep),
        #: Stated in the output so no caller can read a Tier0 survivor
        #: as an endorsement.
        "decides_buys": False,
    }
