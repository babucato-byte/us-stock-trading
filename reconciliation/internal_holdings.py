"""What the internal system believes it holds, and which strategy holds it.

The mistake this module exists to prevent
-----------------------------------------
The obvious implementation of "include S2 in reconciliation" is

    internal = s1_positions.holdings() + s2_positions.holdings()

and it is wrong. The strategy tables are not the account-level record.
Fill synchronisation writes every filled position into `positions` --
for S1, for S2, for anything -- and `s1_positions` / `s2_positions` are
strategy BOOKKEEPING layered on top of it: the exit state that the
account-level row has no place for.

So TX today exists in `positions` AND in `s1_positions`. Summing the
strategy tables on top of the account table would report 2 against the
broker's 1, and the resulting "mismatch" would fail-close every new
entry -- including S1's, which is trading correctly. A reconciliation
bug that halts trading is not a safe failure; it is an outage that looks
like a safety feature.

What the strategy tables ARE good for
-------------------------------------
Two things the account-level store cannot answer on its own:

* ATTRIBUTION. "Which strategy holds this" makes a mismatch actionable:
  an operator reading `TX / NYSE / 1` learns nothing about where to
  look, and `S1: TX / NYSE / 1` tells them exactly.

* COVERAGE. A position in `s2_positions` with no row in `positions`
  means fill synchronisation did not record it -- which is precisely the
  failure that cost S1 its bookkeeping once already, when an exit guard
  excluded a strategy wholesale and took fill sync with it. Nothing else
  in the system notices that: the account table looks consistent with
  the broker, and the strategy simply has a position nobody is counting.

Venue is part of identity
-------------------------
Aggregation keys on (symbol, venue) using the broker's own exchange code,
never the one we requested. KIS answers a NASD request with NYSE rows,
so the requested code is not an identity -- the correction TX needed.
Quantities for the same (symbol, venue) are SUMMED, so a future posture
that lets two strategies hold one name reconciles correctly without this
module changing.
"""

import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

S1_STRATEGY_ID = "S1_HMA_EARLY_TREND_V1"
S2_STRATEGY_ID = "S2_VOLUME_ACCUMULATION_V1"

#: A strategy position with no account-level row behind it.
GAP_NOT_IN_ACCOUNT = "STRATEGY_POSITION_NOT_IN_ACCOUNT_STORE"
#: An account-level row no strategy claims. Not automatically wrong --
#: the legacy watchlist path holds positions too -- so it is reported
#: separately rather than as a fault.
GAP_UNATTRIBUTED = "ACCOUNT_POSITION_NOT_CLAIMED_BY_A_STRATEGY"


def _key(symbol, venue) -> Tuple[str, Optional[str]]:
    return (str(symbol or "").upper(), str(venue).upper() if venue else None)


def aggregate(rows) -> Dict[Tuple[str, Optional[str]], int]:
    """(symbol, venue) -> total quantity.

    Summed rather than counted. A row count would answer "how many
    position records" when the broker is answering "how many shares",
    and the two agree only while every strategy holds at most one.
    """
    totals: Dict[Tuple[str, Optional[str]], int] = defaultdict(int)
    for row in rows or []:
        if isinstance(row, dict):
            symbol, venue, qty = (row.get("symbol"), row.get("venue"),
                                  row.get("quantity"))
        else:
            symbol, venue, qty = (list(row) + [None, None])[:3]
        try:
            quantity = int(qty)
        except (TypeError, ValueError):
            continue
        if quantity:
            totals[_key(symbol, venue)] += quantity
    return dict(totals)


def strategy_holdings(conn) -> Dict[str, List[Tuple[str, Optional[str], int]]]:
    """Per-strategy bookkeeping, for attribution. NOT summed into totals.

    A strategy whose table is missing or unreadable contributes nothing
    and is logged. Attribution is a diagnostic: losing it must not turn
    a healthy reconciliation into a mismatch.
    """
    holdings: Dict[str, List[Tuple[str, Optional[str], int]]] = {}
    for strategy_id, module_path in ((S1_STRATEGY_ID, "s1_live.position_store"),
                                     (S2_STRATEGY_ID, "s2_live.position_store")):
        try:
            module = __import__(module_path, fromlist=["holdings"])
            rows = module.holdings(conn) if hasattr(module, "holdings") else []
            holdings[strategy_id] = [
                (str(s).upper(), (str(v).upper() if v else None), int(q))
                for s, v, q in rows]
        except Exception:  # noqa: BLE001 - a diagnostic must not be able
            # to fail the reconciliation it is describing.
            logger.warning("could not read %s holdings for attribution",
                           strategy_id, exc_info=True)
            holdings[strategy_id] = []
    return holdings


def attribution(conn) -> List[str]:
    """Human-readable strategy attribution lines, for the log.

    "TX / NYSE / 1" tells an operator nothing about where to look;
    "S1: TX / NYSE / 1" tells them exactly.
    """
    lines = []
    for strategy_id, rows in sorted(strategy_holdings(conn).items()):
        label = strategy_id.split("_")[0]
        if not rows:
            lines.append(f"{label}: none")
            continue
        for symbol, venue, quantity in sorted(rows):
            lines.append(f"{label}: {symbol} / {venue or '-'} / {quantity}")
    return lines


def coverage_gaps(conn, account_rows) -> List[Dict[str, Any]]:
    """Positions a strategy holds that the account store does not.

    This is the check the account-vs-broker comparison structurally
    cannot make: if fill synchronisation skipped a strategy, the account
    table still agrees with the broker and the strategy simply holds
    something nobody counts. That is how S1 lost its bookkeeping once.

    The reverse -- an account row no strategy claims -- is reported too
    but is NOT presumed to be a fault: the legacy watchlist path holds
    positions and claims no strategy.
    """
    account = aggregate(account_rows)
    gaps: List[Dict[str, Any]] = []
    claimed: Dict[Tuple[str, Optional[str]], int] = defaultdict(int)

    for strategy_id, rows in strategy_holdings(conn).items():
        for symbol, venue, quantity in rows:
            key = _key(symbol, venue)
            claimed[key] += quantity
            if account.get(key, 0) < quantity:
                gaps.append({
                    "gap": GAP_NOT_IN_ACCOUNT, "strategy_id": strategy_id,
                    "symbol": key[0], "venue": key[1],
                    "strategy_quantity": quantity,
                    "account_quantity": account.get(key, 0),
                })

    for key, quantity in account.items():
        if claimed.get(key, 0) < quantity:
            gaps.append({
                "gap": GAP_UNATTRIBUTED, "strategy_id": None,
                "symbol": key[0], "venue": key[1],
                "strategy_quantity": claimed.get(key, 0),
                "account_quantity": quantity,
            })
    return gaps


def summary(conn, account_rows) -> Dict[str, Any]:
    """Everything the reconciliation log needs about the internal side."""
    totals = aggregate(account_rows)
    gaps = coverage_gaps(conn, account_rows)
    return {
        "internal_holdings": [
            {"symbol": s, "venue": v, "quantity": q}
            for (s, v), q in sorted(totals.items())],
        "attribution": attribution(conn),
        "coverage_gaps": gaps,
        # Only a missing account row is a fault. An unattributed account
        # row is normal for the legacy path and is reported, not raised.
        "coverage_healthy": not any(g["gap"] == GAP_NOT_IN_ACCOUNT
                                    for g in gaps),
    }
