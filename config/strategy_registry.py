"""Which live strategy a position or order attempt belongs to.

Why this exists
---------------
Attribution was never needed while one global cap governed everything: a
union of symbols needs no owner. A PER-STRATEGY cap does, and the moment
you go looking for the owner you find the codebase names each strategy
several different ways, all of them load-bearing and all of them written
to durable rows that already exist:

    S1   hma_early_trend            s1_live/executor.py, s1_positions
         S1_HMA_EARLY_TREND_V1      s1_live/qualification.py, positions
         PAPER_STRATEGY_ORDER_SCORE_V1
                                    the legacy/bootstrap signal id
    S2   accumulation               the scanner name
         S2_VOLUME_ACCUMULATION_V1  s2_live/*, s2_positions
    S6   orb                        the scanner name
         S6_ORB_BREAKOUT_V1         s6_live/*, s6_positions

Normalising at the point of USE -- in the limit checker, in the gate, in
the pre-live report -- would put a copy of this table in each of them,
and the copies would drift. The table is here, once.

Unknown is not "no strategy"
----------------------------
`slot_for()` returns None for a name it does not recognise, and every
caller must treat None as "could belong to ANY strategy", never as
"belongs to none". The difference decides which way the cap fails: an
unrecognised in-flight order treated as ownerless would free capacity
for whichever strategy asked next, which is the one outcome a cap must
never produce. Treated as belonging to all of them, it blocks -- which
is visible, reportable and safe.
"""

from typing import Dict, FrozenSet, Optional

SLOT_S1 = "S1"
SLOT_S2 = "S2"
SLOT_S6 = "S6"

#: Every strategy that may hold a live position, in report order.
LIVE_SLOTS = (SLOT_S1, SLOT_S2, SLOT_S6)

#: Alias -> slot. Keys are compared upper-cased and stripped, so the
#: scanner-name and strategy-id spellings both resolve.
_ALIASES: Dict[str, str] = {
    # S1
    "HMA_EARLY_TREND": SLOT_S1,
    "S1_HMA_EARLY_TREND_V1": SLOT_S1,
    # The bootstrap and the pre-S1 live path both signed their orders
    # this way. Rows carrying it are S1's by lineage, and counting them
    # anywhere else would let S1 exceed its cap using its own history.
    "PAPER_STRATEGY_ORDER_SCORE_V1": SLOT_S1,
    "S1": SLOT_S1,
    # S2
    "ACCUMULATION": SLOT_S2,
    "S2_VOLUME_ACCUMULATION_V1": SLOT_S2,
    "S2": SLOT_S2,
    # S6
    "ORB": SLOT_S6,
    "S6_ORB_BREAKOUT_V1": SLOT_S6,
    "S6": SLOT_S6,
}

#: The durable position store behind each slot. `strategy_id` is not
#: re-checked against the table's own column: the table IS the
#: attribution, and a row in `s6_positions` is S6's whatever string it
#: happens to carry.
POSITION_TABLES: Dict[str, str] = {
    SLOT_S1: "s1_positions",
    SLOT_S2: "s2_positions",
    SLOT_S6: "s6_positions",
}

#: Statuses in those tables that mean "this strategy is using its slot".
#: SUBMITTED counts: an order sent whose fill is unconfirmed is exactly
#: the case a cap exists to stop being doubled. EXIT_PENDING and
#: EXIT_SUBMITTED count too -- the shares are still held until the exit
#: actually fills, and a slot freed at the moment an exit was *decided*
#: would let a replacement be bought against a position that still
#: exists.
HOLDING_STATUSES: FrozenSet[str] = frozenset({
    "SUBMITTED", "OPEN", "EXIT_PENDING", "EXIT_SUBMITTED",
})

#: The one status that releases a slot.
CLOSED_STATUS = "CLOSED"


def slot_for(strategy_id) -> Optional[str]:
    """The canonical slot for a strategy id or scanner name, else None.

    None means UNRECOGNISED, which callers must fail closed on. It does
    not mean "no strategy".
    """
    if strategy_id is None:
        return None
    key = str(strategy_id).strip().upper()
    return _ALIASES.get(key) if key else None


def require_slot(strategy_id) -> str:
    """`slot_for`, raising instead of returning None.

    For the entry path, where an order whose strategy cannot be named
    must not be built at all -- as opposed to the counting path, which
    has to cope with rows that already exist.
    """
    slot = slot_for(strategy_id)
    if slot is None:
        raise ValueError(
            f"strategy {strategy_id!r} is not a known live strategy; "
            f"known: {sorted(set(_ALIASES.values()))}")
    return slot
