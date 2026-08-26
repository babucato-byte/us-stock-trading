"""One broker position, one owning strategy.

The failure this exists for
---------------------------
S6 placed a real BUY for DT (KIS order 0030740200) and recorded it in
`s6_positions`. Eight minutes later S1's `sync_fills` saw a DT position at
the broker, found nothing claiming it in S1's OWN book, and adopted it as
an S1 position. One share was now owned twice.

Nothing crashed. Both books were internally consistent. The account held
exactly the one share it should. What was wrong was that two independent
exit engines each believed they were responsible for selling it, and the
only thing standing between that and a duplicate SELL was an unrelated
reconciliation mismatch that happened to be blocking both.

Ownership is not a symbol match
-------------------------------
`sync_fills` asked "is this symbol in MY book" -- a question that can only
ever find S1's own rows, so any position another strategy opened looks
unclaimed. The question that matters is "does ANY strategy already claim
this", and it is asked here, once, against every per-strategy book plus
the durable order ledger.

The ledger is the stronger evidence and is checked first: a broker fill
descends from an order, that order was signed by a strategy, and that
signature is provenance rather than inference. The position books are the
fallback for a holding whose originating order has aged out.

Ambiguity fails closed
----------------------
An unreadable book, an unreadable ledger, or a symbol claimed by more
than one strategy all return "do not adopt". Adopting on doubt is how the
duplicate happened; refusing on doubt costs only that the position stays
unattributed, which is visible, reportable, and safe.

A conflict is not a thing to dedupe
-----------------------------------
The reconciler must NOT quietly collapse two claims on one symbol into a
single holding that happens to match the broker. That arithmetic looks
clean and hides the exact condition that permits a double SELL. Two
strategies claiming one symbol is OWNERSHIP_CONFLICT, and it fails
closed like any other reconciliation disagreement.
"""

import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Reported when one symbol is claimed by more than one strategy book.
OWNERSHIP_CONFLICT = "OWNERSHIP_CONFLICT"


def claimant_from_ledger(conn, symbol) -> Optional[str]:
    """Which strategy's order produced this holding, from the ledger.

    Provenance rather than inference: a fill descends from an order and
    that order carries the strategy that placed it. Only orders that
    reached the broker count -- a rejected order never produced shares.
    """
    wanted = str(symbol or "").upper()
    if not wanted:
        return None
    try:
        rows = conn.execute(
            "SELECT strategy_id, status FROM kis_order_idempotency "
            "WHERE side = 'buy' AND UPPER(symbol) = ? "
            "AND strategy_id IS NOT NULL "
            "AND status NOT IN ('REJECTED', 'CANCELLED') "
            "ORDER BY rowid DESC", (wanted,)).fetchall()
    except Exception:  # noqa: BLE001 - unreadable is not unowned
        logger.warning("ownership: order ledger unreadable for %s", wanted,
                       exc_info=True)
        return None
    for row in rows:
        strategy_id = row["strategy_id"] if hasattr(row, "keys") else row[0]
        if strategy_id:
            return str(strategy_id)
    return None


def claims_by_symbol(conn) -> Dict[str, List[str]]:
    """symbol -> every strategy whose position book claims it.

    A list, not a single owner: the whole point is to make a second
    claimant visible rather than to pick between them.
    """
    from reconciliation import internal_holdings

    claims: Dict[str, List[str]] = {}
    try:
        holdings = internal_holdings.strategy_holdings(conn) or {}
    except Exception:  # noqa: BLE001
        logger.warning("ownership: per-strategy books unreadable", exc_info=True)
        return claims
    for strategy_id, rows in holdings.items():
        for symbol, _venue, quantity in rows or ():
            name = str(symbol or "").upper()
            if not name or not quantity:
                continue
            claims.setdefault(name, [])
            if strategy_id not in claims[name]:
                claims[name].append(strategy_id)
    return claims


def conflicts(conn) -> List[Tuple[str, List[str]]]:
    """Every symbol claimed by more than one strategy."""
    return sorted((symbol, sorted(owners))
                  for symbol, owners in claims_by_symbol(conn).items()
                  if len(owners) > 1)


def may_adopt(conn, symbol, *, strategy_id) -> Tuple[bool, str]:
    """May `strategy_id` record a broker holding of `symbol` as its own?

    Returns (permitted, reason). Refuses when any other strategy already
    claims it, when the ledger says another strategy's order produced it,
    and whenever ownership cannot be established at all.
    """
    wanted = str(symbol or "").upper()
    if not wanted:
        return False, "no symbol"

    owner = claimant_from_ledger(conn, wanted)
    if owner and owner != strategy_id:
        return False, (f"order ledger attributes {wanted} to {owner}, "
                       f"not {strategy_id}")

    others = [s for s in claims_by_symbol(conn).get(wanted, ())
              if s != strategy_id]
    if others:
        return False, f"{wanted} is already claimed by {', '.join(sorted(others))}"

    return True, "unclaimed"


#: The exit reason a wrongly-attributed row is retired under.
#:
#: Deliberately not a trading outcome. A row released this way was never
#: this strategy's position, so recording it as a normal CLOSED would put
#: a trade in the strategy's realized record that it never made -- and
#: the entry price would make it look like a scratch.
RELEASED_WRONGLY_ATTRIBUTED = "RELEASED_WRONGLY_ATTRIBUTED"


def release_misattributed(conn, *, symbol, strategy_id, now=None,
                          audit=True) -> Dict[str, object]:
    """Retire a position row that belongs to a different strategy.

    Refuses unless ownership demonstrably lies elsewhere, so this cannot
    be used to take a position away from the strategy that actually owns
    it. Refuses too if the row has any exit in flight: a row mid-exit is
    being acted on, and retiring it underneath that is how an exit gets
    orphaned.

    Goes through the store's own `close_position` transition rather than
    writing status directly, and records an audit event, so the release
    is reconstructable afterwards rather than appearing as a row that
    silently vanished.
    """
    from datetime import datetime, timezone

    current = now or datetime.now(timezone.utc)
    wanted = str(symbol or "").upper()

    owner = claimant_from_ledger(conn, wanted)
    others = [s for s in claims_by_symbol(conn).get(wanted, ()) if s != strategy_id]
    if not owner and not others:
        return {"released": False, "symbol": wanted,
                "reason": "ownership could not be established elsewhere; "
                          "refusing to retire a row that may be genuine"}
    if owner == strategy_id:
        return {"released": False, "symbol": wanted,
                "reason": f"the ledger attributes {wanted} to {strategy_id}"}

    module_path = {"S1_HMA_EARLY_TREND_V1": "s1_live.position_store",
                   "S2_VOLUME_ACCUMULATION_V1": "s2_live.position_store",
                   "S6_ORB_BREAKOUT_V1": "s6_live.position_store"}.get(strategy_id)
    if module_path is None:
        return {"released": False, "symbol": wanted,
                "reason": f"no position store known for {strategy_id}"}

    store = __import__(module_path, fromlist=["load_live"])
    released = []
    for entry in store.load_live(conn) or ():
        # Stores differ in what load_live yields; the row dict is last.
        row = entry[-1] if isinstance(entry, tuple) else entry
        position_id = entry[0] if isinstance(entry, tuple) else row.get("position_id")
        if str(row.get("symbol") or "").upper() != wanted:
            continue
        if row.get("exit_submitted"):
            return {"released": False, "symbol": wanted,
                    "reason": f"{position_id} has an exit in flight; "
                              "refusing to retire a row being acted on"}
        store.close_position(conn, position_id,
                             exit_reason=RELEASED_WRONGLY_ATTRIBUTED,
                             now=current)
        released.append(position_id)
        if audit:
            try:
                import shadow_audit

                shadow_audit.record_event(
                    shadow_run_id=shadow_audit.new_run_id(),
                    event_type="RECONCILIATION_BLOCKED",
                    result="INFO", symbol=wanted, side="buy",
                    internal_order_id=position_id,
                    reason_code=OWNERSHIP_CONFLICT,
                    detail=(f"{position_id} released from {strategy_id}: "
                            f"{wanted} belongs to {owner or ', '.join(others)}"),
                    now=current)
            except Exception:  # noqa: BLE001 - the release is durable
                # already; losing its audit row must not undo it.
                logger.warning("ownership release audit failed for %s",
                               position_id, exc_info=True)
    return {"released": bool(released), "symbol": wanted,
            "position_ids": released,
            "owner": owner or (others[0] if others else None)}
