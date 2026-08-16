"""The two passes that produce a Manual Watchlist.

    build_tomorrow(session_day, target_day)
        Run after the daily scanners close on `session_day`. Reads that
        session's stored signals, keeps only the daily scanners, scores
        and files the result under `target_day` -- the day the list is
        FOR. Writes a file. Sends nothing.

    build_today(trading_day)
        Run after the premarket scanner on `trading_day`. Loads last
        night's list for this day, applies the premarket confirmation,
        re-ranks, and files the result. This is the one that gets
        posted.

Reading only
------------
Both passes call `result_store` read functions and nothing else. There
is no write path into the analytics store from this module, and no
import of anything that could reach an order. A watchlist that could
modify the dataset it is derived from would make month 1's signals
non-reproducible.

Why the morning pass does not add new symbols
---------------------------------------------
`build_today` re-scores the names the evening pass already found; a
symbol the premarket scanner flags that no daily scanner flagged the
night before is NOT added. That is a deliberate limit, not an
oversight: the premarket scanner measures this morning's gap, and a name
whose only evidence is a gap has no overnight thesis behind it. Adding
those would turn a curated reading list back into a raw signal feed,
which is what the scanner reports already are.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from scanners.base import result_store
from watchlist import config, ranking, store

logger = logging.getLogger(__name__)


class WatchlistBuildError(Exception):
    """The watchlist could not be built. Never raised for an empty day."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rows_for(trading_day: str, scanner_names) -> List[Dict[str, Any]]:
    wanted = set(scanner_names)
    return [row for row in result_store.read_signal_rows(trading_day)
            if str(row.get("scanner_name")) in wanted]


def build_tomorrow(session_day: str, target_day: str) -> Dict[str, Any]:
    """Evening pass. Returns the payload; the caller decides to write it."""
    daily_rows = _rows_for(session_day, config.DAILY_SOURCE_SCANNERS)
    intraday_rows = _rows_for(session_day, config.INTRADAY_OBSERVE_SCANNERS)

    by_symbol = ranking.group_by_symbol(daily_rows)
    intraday_by_symbol = ranking.group_by_symbol(intraday_rows)

    entries = [
        ranking.score_symbol(symbol, rows,
                             intraday_rows=intraday_by_symbol.get(symbol, []))
        for symbol, rows in by_symbol.items()
    ]
    ordered = ranking.rank(entries)
    total = len(ordered)
    kept = ordered[:config.STORE_TOP_N]

    return {
        "stage": config.STAGE_TOMORROW,
        "trading_day": target_day,
        "source_session_day": session_day,
        "generated_at": _now_iso(),
        "manual_watch_version": config.MANUAL_WATCH_VERSION,
        "source_scanners": list(config.DAILY_SOURCE_SCANNERS),
        "symbols_considered": total,
        "truncated_from": total if total > len(kept) else None,
        "entries": kept,
        "notice": "관측·수동 검토용 · 자동 주문 아님",
    }


def build_today(trading_day: str, *, tomorrow: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Morning pass. Re-ranks last night's list with premarket confirmation.

    A missing Tomorrow Watchlist is a normal, reported state -- the
    first day of a month, the day after a holiday, or a day whose
    evening scan failed. It produces an empty Today Watchlist that says
    so, rather than an error or a silently blank list.
    """
    previous = tomorrow if tomorrow is not None else store.read_json(
        trading_day, config.STAGE_TOMORROW)

    premarket_rows = _rows_for(trading_day, [config.PREMARKET_CONFIRM_SCANNER])
    intraday_rows = _rows_for(trading_day, config.INTRADAY_OBSERVE_SCANNERS)
    premarket_by_symbol = ranking.group_by_symbol(premarket_rows)
    intraday_by_symbol = ranking.group_by_symbol(intraday_rows)

    if not previous:
        return {
            "stage": config.STAGE_TODAY,
            "trading_day": trading_day,
            "generated_at": _now_iso(),
            "manual_watch_version": config.MANUAL_WATCH_VERSION,
            "source_session_day": None,
            "premarket_confirmations": len(premarket_by_symbol),
            "symbols_considered": 0,
            "entries": [],
            "empty_reason": "직전 세션의 Tomorrow Watchlist가 없습니다",
            "notice": "관측·수동 검토용 · 자동 주문 아님",
        }

    rescored = [
        _apply_premarket(dict(entry),
                         premarket_by_symbol.get(entry.get("symbol"), []),
                         intraday_by_symbol.get(entry.get("symbol"), []))
        for entry in (previous.get("entries") or [])
    ]
    ordered = ranking.rank(rescored)

    return {
        "stage": config.STAGE_TODAY,
        "trading_day": trading_day,
        "generated_at": _now_iso(),
        "manual_watch_version": config.MANUAL_WATCH_VERSION,
        "source_session_day": previous.get("source_session_day"),
        "tomorrow_generated_at": previous.get("generated_at"),
        "premarket_confirmations": sum(
            1 for entry in ordered if entry.get("premarket_confirmed")),
        "symbols_considered": len(ordered),
        "entries": ordered,
        "notice": "관측·수동 검토용 · 자동 주문 아님",
    }


def _apply_premarket(entry: Dict[str, Any], premarket_rows: List[Dict[str, Any]],
                     intraday_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Add this morning's confirmation to an evening entry.

    The evening components are reused verbatim rather than recomputed:
    last night's daily signals are what they were, and re-deriving them
    from a second read of the same files would only create a way for the
    two passes to disagree. Only the parts that this morning can change
    -- the confirmation flag, the overextension picture, and therefore
    the total -- are recomputed.
    """
    components = dict(entry.get("components") or {})
    confirmed = bool(premarket_rows)
    components["premarket_confirm"] = round(
        config.WEIGHT_PREMARKET_CONFIRM if confirmed else 0.0, 4)

    if premarket_rows:
        # Recompute extension including the premarket rows: a name that
        # gapped 12% overnight is overextended this morning even though
        # it was not at last night's close.
        merged = ranking.overextension(premarket_rows + [_entry_as_row(entry)])
        entry.update(merged)

    penalty = config.PENALTY_OVEREXTENDED if entry.get("overextended") else 0.0
    components["overextended_penalty"] = round(-penalty, 4)

    scores = [entry.get("max_scanner_score")]
    for row in premarket_rows:
        value = ranking.finite(row.get("scanner_score"))
        if value is not None:
            scores.append(value)
    usable = [value for value in scores if value is not None]
    if usable:
        entry["max_scanner_score"] = round(max(usable), 4)
        components["max_scanner_score"] = round(
            config.WEIGHT_MAX_SCORE * max(usable) / 100.0, 4)

    entry["components"] = components
    entry["manual_watch_score"] = round(sum(components.values()), 4)
    entry["premarket_confirmed"] = confirmed
    entry["premarket_signal_count"] = len(premarket_rows)
    if intraday_rows:
        entry["intraday_observed"] = sorted(
            {str(row.get("scanner_name")) for row in intraday_rows
             if row.get("scanner_name")})
    return entry


def _entry_as_row(entry: Dict[str, Any]) -> Dict[str, Any]:
    """The evening entry's extension figures, shaped like a signal row so
    `overextension()` can consider them alongside this morning's."""
    return {
        "extension_hma200_pct": entry.get("extension_hma200_pct"),
        "extension_hma89_pct": entry.get("extension_hma89_pct"),
        "price_change_pct": entry.get("price_change_pct"),
        "distance_52w_high": entry.get("distance_52w_high"),
    }
