"""TCN-02A: one call that runs `external_close` over every position book.

`reconciliation/external_close.py` has been able to retire a row the
broker no longer holds since 2026-08-31, but nothing in the runtime or
the scheduler ever called it: the TCN-01 audit found no production
caller. This is the application-layer entry point that call would use.

It is deliberately NOT wired to cron or systemd here. TCN-02B decides
where and how often it runs. What this module settles is the contract:

    * every per-strategy book (S1, S2, S6) and the general lifecycle book
    * the same evidence bar as `external_close.evaluate` -- broker read
      succeeded, no position, no open order, no unresolved ledger order,
      no unresolved exit intent, no exit in flight
    * `apply=False` by default: a dry run reports what WOULD be retired
      and changes nothing, which is how it is meant to be run first
    * at most one retirement per row, because a retired row leaves the
      live set and is never seen again

Nothing here submits, cancels, or invents a price.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from reconciliation import external_close

logger = logging.getLogger(__name__)

#: Retirement outcome vocabulary re-exported so a caller need not import
#: two modules to read a report.
RETIRED = external_close.RETIRED
GENERAL_BOOK = "GENERAL"


def _strategy_stores():
    """The per-strategy books, keyed by canonical strategy id. Imported
    lazily so a book that fails to import is reported, not fatal."""
    stores = {}
    for module_name in ("s1_live.position_store", "s2_live.position_store",
                        "s6_live.position_store"):
        try:
            module = __import__(module_name, fromlist=["STRATEGY_ID"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("position store %s unavailable: %s", module_name, exc)
            continue
        strategy_id = getattr(module, "STRATEGY_ID", module_name)
        stores[strategy_id] = module
    return stores


def retire_all(conn, broker, *, now=None, apply=False,
               include_general=True) -> Dict[str, List[Dict[str, Any]]]:
    """Run the retirement over every book. Returns outcomes per book.

    `apply=False` (the default) is a dry run. Pass `apply=True` only from
    a caller that has decided retirement is wanted -- the scheduler that
    TCN-02B adds, or an operator running `scripts/run_external_close.py`
    with `--apply`.
    """
    current = now or datetime.now(timezone.utc)
    report: Dict[str, List[Dict[str, Any]]] = {}

    for strategy_id, store in _strategy_stores().items():
        try:
            report[strategy_id] = external_close.retire_externally_closed(
                conn, broker, strategy_id=strategy_id, store=store,
                now=current, apply=apply)
        except Exception as exc:  # noqa: BLE001 - one book failing must
            # not stop the others from being reported.
            logger.warning("external close over %s failed: %s", strategy_id,
                           exc, exc_info=True)
            report[strategy_id] = [{"outcome": "BOOK_ERROR",
                                    "detail": str(exc)[:200]}]

    if include_general:
        try:
            report[GENERAL_BOOK] = external_close.retire_general_store(
                broker, conn, now=current, apply=apply)
        except Exception as exc:  # noqa: BLE001
            logger.warning("external close over the general book failed: %s",
                           exc, exc_info=True)
            report[GENERAL_BOOK] = [{"outcome": "BOOK_ERROR",
                                    "detail": str(exc)[:200]}]
    return report


def candidates(conn, broker, *, now=None) -> List[Dict[str, Any]]:
    """Rows that WOULD be retired right now. A dry run, flattened."""
    found = []
    for book, outcomes in retire_all(conn, broker, now=now, apply=False).items():
        for outcome in outcomes:
            if outcome.get("outcome") == RETIRED:
                found.append({"book": book, **outcome})
    return found


def summarize(report: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Counts per outcome, per book, for a log line."""
    summary: Dict[str, Any] = {}
    for book, outcomes in report.items():
        counts: Dict[str, int] = {}
        for outcome in outcomes:
            key = str(outcome.get("outcome") or "UNKNOWN")
            counts[key] = counts.get(key, 0) + 1
        summary[book] = counts
    return summary
