"""Which symbols an intraday scan actually looks at.

The gap this closes
-------------------
The runner took the activity ranking's top 300 and THEN removed the
ineligible ones, with no top-up. On 2026-08-24 that left 202 symbols
scanned: 98 of the top 300 carried a `provider_unavailable` record, so a
third of the intended universe silently vanished and the scan reported
`universe=300` while judging 202 names.

The fix is an ordering change, not a widening. Walk the same ranking, in
the same order, and keep going until 300 ELIGIBLE symbols have been
collected. The 300 that get scanned are still the most liquid 300 the
store can actually serve -- the limit is now on what is examined rather
than on what is considered.

What this does NOT change
-------------------------
Not the ranking (dollar volume, descending), not the pool size, and not
one S6 gate. A symbol that enters the universe under this rule still
faces ORB15, the close-breakout test, VWAP, EMA9>EMA21, volume
expansion 1.2x and the 6% extension ceiling unchanged. Coverage and
selectivity are different questions and this file only answers the
first.

Depth is bounded
----------------
Walking a 10,500-name ranking to fill 300 slots would be unbounded work
against a store that could, in principle, mark everything ineligible.
`max_depth` stops the walk, and how deep it actually went is reported so
a universe that is quietly scraping the bottom of the ranking is
visible rather than inferred.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: How far down the ranking the fill may walk, as a multiple of the
#: requested size. At the observed 33% ineligible rate a 300-slot
#: universe needs roughly 450 candidates; 5x leaves room for a much
#: worse day without becoming an unbounded scan.
DEFAULT_DEPTH_MULTIPLE = 5

#: Where a symbol in the scan universe came from. Recorded per symbol so
#: that a month from now "did the supplement find anything the ranking
#: missed" is a query rather than an argument.
SOURCE_PREVIOUS_DAY = "PREVIOUS_DAY_RANKING"
SOURCE_INTRADAY_SUPPLEMENT = "INTRADAY_SUPPLEMENT"


@dataclass
class UniverseSelection:
    """The symbols to scan, and the provenance of each."""

    symbols: List[str] = field(default_factory=list)
    #: symbol -> {"source": ..., "activity_rank": int|None}
    provenance: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    requested: int = 0
    considered: int = 0
    skipped_ineligible: int = 0
    depth_reached: int = 0
    depth_exhausted: bool = False
    supplement_added: int = 0

    def summary(self) -> Dict[str, Any]:
        return {
            "requested": self.requested,
            "selected": len(self.symbols),
            "considered": self.considered,
            "skipped_ineligible": self.skipped_ineligible,
            "depth_reached": self.depth_reached,
            "depth_exhausted": self.depth_exhausted,
            "supplement_added": self.supplement_added,
        }

    def source_of(self, symbol) -> Optional[str]:
        return (self.provenance.get(str(symbol).upper()) or {}).get("source")

    def rank_of(self, symbol) -> Optional[int]:
        return (self.provenance.get(str(symbol).upper()) or {}).get("activity_rank")


def eligible_top(activity_store, eligibility_store, *, limit,
                 today=None, max_depth=None) -> UniverseSelection:
    """`limit` eligible symbols, taken in activity-ranking order.

    The ranking is asked for `max_depth` names and filtered; it is not
    asked repeatedly with a growing limit, because `active_symbols`
    sorts the whole store on every call and doing that in a loop would
    turn a linear fill into a quadratic one.
    """
    wanted = max(0, int(limit))
    depth = int(max_depth) if max_depth else wanted * DEFAULT_DEPTH_MULTIPLE
    selection = UniverseSelection(requested=wanted)
    if wanted == 0:
        return selection

    ranked = activity_store.active_symbols(limit=depth, today=today) or []
    for position, symbol in enumerate(ranked, start=1):
        selection.considered += 1
        selection.depth_reached = position
        if eligibility_store.should_skip(symbol, today=today):
            selection.skipped_ineligible += 1
            continue
        upper = str(symbol).upper()
        selection.symbols.append(symbol)
        selection.provenance[upper] = {"source": SOURCE_PREVIOUS_DAY,
                                       "activity_rank": position}
        if len(selection.symbols) >= wanted:
            break

    selection.depth_exhausted = (len(selection.symbols) < wanted
                                 and selection.considered >= len(ranked))
    if selection.depth_exhausted:
        # Not an error: a small store or a very bad provider day can
        # genuinely have fewer than `limit` usable names. Said out loud
        # so it is not mistaken for a quiet market later.
        logger.info("universe fill reached the end of the ranking: %s of %s "
                    "eligible after %s considered",
                    len(selection.symbols), wanted, selection.considered)
    return selection


def merge_supplement(selection: UniverseSelection, supplement,
                     *, limit=None) -> UniverseSelection:
    """Add intraday names the previous day's ranking could not know about.

    Order matters and is deliberate: the ranking-derived symbols keep
    their places and the supplement is appended. A supplement symbol
    that is ALREADY in the selection keeps its original provenance --
    it was found by the ranking, and relabelling it would inflate the
    supplement's apparent contribution in exactly the comparison this
    provenance exists to support.

    This decides what to LOOK AT. It is not a strategy gate: nothing
    here gives a supplement symbol an easier path through ORB15, VWAP,
    the EMA structure or the 1.2x volume expansion than a ranking symbol
    gets.
    """
    if not supplement:
        return selection
    room = None if limit is None else max(0, int(limit))
    added = 0
    for symbol in supplement:
        if room is not None and added >= room:
            break
        upper = str(symbol).upper()
        if upper in selection.provenance:
            continue                      # already covered by the ranking
        selection.symbols.append(symbol)
        selection.provenance[upper] = {"source": SOURCE_INTRADAY_SUPPLEMENT,
                                       "activity_rank": None}
        added += 1
    selection.supplement_added = added
    return selection
