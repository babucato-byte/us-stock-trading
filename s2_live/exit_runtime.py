"""S2 exit decisions turned into real SELLs, through S1's submission path.

What is shared and what is not
------------------------------
The DECISION is S2's: `s2_live.exit_policy` owns when an S2 position
leaves, and S1's exit policy never sees one. The SUBMISSION is shared --
`s1_live.exit_runtime._submit_sell`, with S2's position store passed in.

That split is the point. The submission code carries behaviours that were
learned the hard way on S1: an ambiguous send goes to SUBMISSION_UNKNOWN
and is never auto-retried, a rejection does not chase the price or
enlarge the quantity, and the exit intent ledger refuses a second order
for a position that already has one live. A second copy of that would be
a second idea of what is safe, and the two would diverge quietly -- the
same failure the buy cycle's docstring warns about, on the sell side.

Ownership is enforced, not assumed
----------------------------------
Only positions in `s2_positions` are evaluated here, and S1's exit
manager is guarded against S2's symbols the same way it already is
against its own. A position managed by two exit policies would get two
SELLs for one holding.

Exits are not gated by entry risk
---------------------------------
A reconciliation mismatch, a full position book, a tripped entry limit:
none of them blocks an exit. They block ENTRIES. The distinction is what
keeps a risk control from trapping the account in the position it exists
to escape.
"""

import logging
from typing import Any, Dict, List, Optional

from s1_live.exit_runtime import ExitOutcome, _submit_sell
from s2_live import exit_policy, position_store

logger = logging.getLogger(__name__)

ACTION_HELD = "HELD"
ACTION_SOLD = "SOLD"
ACTION_BLOCKED = "BLOCKED"
ACTION_LATCHED = "LATCHED"

CLIENT_ORDER_PREFIX = "s2exit"


def evaluate_position(conn, *, broker_adapter, position_id, row,
                      features=None, current_price=None, session=None,
                      now=None, orders_allowed=True,
                      emergency=False) -> ExitOutcome:
    """Decide, and submit if the decision is SELL.

    The observation is recorded BEFORE the decision is asked for, so the
    peak the decision reads is the one including this tick. Asking first
    would judge a position against a peak it had already exceeded.
    """
    symbol = row["symbol"]

    multiple = None
    baseline = row.get("baseline_volume")
    volume = getattr(features, "volume", None) if features else None
    if baseline and volume is not None:
        try:
            multiple = float(volume) / float(baseline)
        except (TypeError, ValueError, ZeroDivisionError):
            multiple = None

    state = position_store.to_state(row)
    decayed = exit_policy.volume_has_decayed(state, features)
    position_store.observe(conn, position_id, volume_multiple=multiple,
                           price=current_price, decayed=decayed, now=now)

    refreshed = position_store.load_by_symbol(conn, symbol) or row
    decision = exit_policy.decide(
        position_store.to_state(refreshed), current_price=current_price,
        features=features, session=session, now=now, emergency=emergency)

    if not decision.sells:
        return ExitOutcome(position_id, symbol, ACTION_HELD,
                           decision.reason, "")

    if not orders_allowed:
        # The decision stands and is latched rather than dropped. An
        # unverified session is a reason to wait, never a reason to
        # forget that the position should be leaving.
        position_store.latch_pending_exit(conn, position_id, decision.reason,
                                          now=now)
        logger.info("S2 exit latched for %s: session does not permit orders",
                    symbol)
        return ExitOutcome(position_id, symbol, ACTION_LATCHED,
                           decision.reason, "session does not permit orders")

    return _submit_sell(conn, broker_adapter=broker_adapter,
                        position_id=position_id, row=refreshed,
                        reason=decision.reason, now=now,
                        store=position_store, prefix=CLIENT_ORDER_PREFIX)


def run_exits(conn, *, broker_adapter, features_fn, price_fn, session=None,
              now=None, orders_allowed=True, emergency=False
              ) -> List[Dict[str, Any]]:
    """Every live S2 position, evaluated once.

    One position's failure does not cost the others their evaluation --
    and a failure is reported rather than dropped, because an exit that
    was never evaluated looks exactly like one that decided to hold.
    """
    outcomes: List[Dict[str, Any]] = []
    for position_id, row in position_store.load_live(conn):
        symbol = row["symbol"]
        try:
            outcome = evaluate_position(
                conn, broker_adapter=broker_adapter, position_id=position_id,
                row=row, features=features_fn(symbol),
                current_price=price_fn(symbol), session=session, now=now,
                orders_allowed=orders_allowed, emergency=emergency)
            outcomes.append(outcome.as_dict())
        except Exception as exc:  # noqa: BLE001
            logger.error("S2 exit evaluation failed for %s", symbol,
                         exc_info=True)
            outcomes.append(ExitOutcome(position_id, symbol, ACTION_BLOCKED,
                                        None, f"evaluation failed: {exc}"
                                        ).as_dict())
    return outcomes
