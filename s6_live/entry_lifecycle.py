"""Registering an S6 BUY that has been sent, in S6's canonical store.

The gap this closes
-------------------
Every stage of S6's live position lifecycle existed and was tested --
`record_submission`, `sync_buy_fills`/`apply_fill`, `run_exits`,
`sync_sell_fills`, an every-minute exit monitor -- except the one that
starts it. `record_submission` and `open_from_fill` had no production
caller at all, and `s6_positions` had never held a row. The exit runtime
was reading a store nothing wrote.

That is not only an exit problem. `strategy_registry.POSITION_TABLES`
maps S6 to `s6_positions`, and `entry_limits._held_symbols_by_slot`
reads it to decide WHOSE a held position is. A position the store does
not know about is `unattributed`, and unattributed symbols count against
EVERY slot -- so one unrecorded S6 position would have blocked new
entries account-wide, not just S6's.

One store, not two
------------------
S6 records here and NOWHERE else. The general `positions` lifecycle
(`kis_position_manager`) runs its own stop / target / time / EOD exits,
and S6 has its own policy in `s6_live/exit_policy.py` run by the S6
runtime. A position written to both would have two exit engines deciding
about the same shares, which is worse than either. `positions` is also
absent from POSITION_TABLES, so a row there answers nobody's question
about attribution.

Recorded AFTER transport, deliberately
--------------------------------------
`record_submission`'s own docstring prefers "before the broker answers",
and for the broker call that is what happens: this is invoked once the
order has actually been sent, on the success path AND on the ambiguous
one. What it deliberately does NOT do is record before the GATE. A row
written before a gate that then blocks would sit at SUBMITTED forever,
holding S6's only slot, with no broker order for the fill sync to
resolve it against -- `sync_buy_fills` can abandon a submission only when
the broker positively reports it never filled, which it cannot do for an
order that was never placed.

An ambiguous response is exactly why this exists: the row is what turns
"we do not know" into something reconciliation can settle, instead of a
share held at KIS that nothing internal has ever heard of.
"""

import logging
from typing import Any, Dict, Optional

from config import s6_sessions
from s6_live import position_store

logger = logging.getLogger(__name__)

#: Candidate-row key -> `record_submission` keyword. The scanner's
#: published vocabulary and the store's column names are not the same
#: words, and mapping them in one table keeps the translation from being
#: re-guessed at each call site.
_FIELD_MAP = {
    "range_minutes": "range_minutes",
    "range_high": "range_high",
    "range_low": "range_low",
    "vwap": "entry_vwap",
    "ema9": "entry_ema9",
    "ema21": "entry_ema21",
    "volume_expansion": "entry_volume_expansion",
}


def is_s6(strategy_id) -> bool:
    """Does this strategy id belong to S6, under any of its spellings?"""
    from config import strategy_registry

    return strategy_registry.slot_for(strategy_id) == strategy_registry.SLOT_S6


def record_entry_submission(conn, *, symbol, session, client_order_id,
                            candidate_row: Optional[Dict[str, Any]] = None,
                            now=None) -> str:
    """Record a SENT S6 BUY and return its position id.

    The ORB measurements come from the candidate the order was built
    from, so the position carries the range it broke out of rather than
    one re-derived later against different bars. A missing field is left
    NULL rather than defaulted: `exit_policy` can say "not measured", and
    a fabricated range would silently move the structural stop.
    """
    fields = {}
    for source_key, store_key in _FIELD_MAP.items():
        if candidate_row is not None and source_key in candidate_row:
            fields[store_key] = candidate_row.get(source_key)

    variant = s6_sessions.variant_for(session)
    position_id = position_store.record_submission(
        conn, symbol=symbol, variant=variant or None,
        entry_session=str(session).strip().upper() if session else None,
        client_order_id=client_order_id, now=now, **fields)
    logger.info(
        "S6 entry recorded in the canonical store: position=%s symbol=%s "
        "session=%s variant=%s client_order_id=%s",
        position_id, symbol, session, variant, client_order_id)
    return position_id
