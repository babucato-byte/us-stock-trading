"""ORB15 evaluated beside a live ORB5 -- a comparison, never an order.

Why
---
Every S6 session runs ORB5 live from 2026-09. ORB15 was the measured v1.0
reference and the question "did entering on the 5-minute range actually
reduce losses, or did it just produce more candidates" needs the two
judged on the same market at the same instants. This records what the
ORB15 watch WOULD have said for every symbol the live cycle evaluated,
minute by minute, so the daily outcome tracker can pair an ORB5 fill
with the ORB15 signal for the same opportunity.

What it cannot do
-----------------
It cannot place an order. It reads the collector's bar store (a pure
read: `kis_bar_features.build_from_bars` only inspects the store), it
builds a frozen `SessionFeatures` at 15 minutes, it calls
`precision_watch.evaluate` with that view injected, and it appends the
result to a JSONL file. It never touches `WatchedCandidateSource.
evaluations`, never publishes a candidate row, and imports nothing from
the execution package. tests/test_s6_orb5_shadow.py pins all of that.

State isolation
---------------
The live ORB5 features and this ORB15 view are two separate frozen
dataclasses built from the same read-only store. Nothing here mutates
the store or the live evaluation.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

SUBDIR = "range_shadow"


def log_path(trading_day, *, env=None) -> Optional[Path]:
    """No production default, for the reason shadow_signal_log gives."""
    env = env if env is not None else os.environ
    root = env.get("RANGE_SHADOW_DIR") or env.get("SCANNER_DATA_ROOT")
    if not root:
        return None
    return Path(root) / SUBDIR / f"{trading_day}.jsonl"


#: A collected bar is one minute wide by construction. Never inferred from
#: the gap between rows: extended-hours bars exist only for minutes that
#: traded, so that gap measures liquidity, not the width of a bar.
BAR_WIDTH_MINUTES = 1.0

# Observed live ORB15 work has taken roughly four seconds per symbol.  This
# is operational headroom for optional research, never a trading parameter.
MIN_OPERATION_BUDGET_SECONDS = 5.0


def evaluate_symbol(symbol, *, store, session, now, shadow_minutes, config=None,
                    live_evaluation=None, trading_day=None) -> Optional[Dict[str, Any]]:
    """One symbol's ORB15 verdict from the collected bars, or None when
    the store holds nothing for it (no stream, nothing to compare)."""
    from config import s6_sessions
    from s6_live import kis_bar_features, precision_watch

    feats = kis_bar_features.build_from_bars(
        symbol, store=store, session=session, now=now,
        range_minutes=int(shadow_minutes), closed_bar_only=True)
    if feats is None:
        return None
    evaluation = precision_watch.evaluate(
        symbol, session=session, now=now, features=feats, config=config,
        conn=None)
    quality = getattr(feats, "entry_quality", None)
    # The signal instant is the CLOSE of the newest bar the verdict used --
    # the same rule the live source applies -- and a theoretical entry
    # would be the next bar's open, i.e. that same instant. Its price is
    # not knowable at the decision, so the outcome tracker fills it in.
    bar_open = getattr(quality, "source_timestamp", None)
    # Only a real closed bar produces a signal instant. When the view
    # carries an error -- a stale feed, no closed bars, an uncovered
    # origin -- there is no snapshot and therefore no signal: falling back
    # to `market_data_asof` would stamp this row with a leftover timestamp
    # from whenever the snapshot was last written, which for a rolled-over
    # session is another session entirely.
    signal_at = (bar_open + timedelta(minutes=BAR_WIDTH_MINUTES)
                 if isinstance(bar_open, datetime) else None)
    origin = getattr(feats, "range_origin_timestamp", None)
    record = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "evaluated_at": now.isoformat() if isinstance(now, datetime) else str(now),
        "fast_watch_evaluated_at": (now.isoformat() if isinstance(now, datetime)
                                    else str(now)),
        "symbol": str(symbol).upper(),
        "session": session,
        "trading_day": trading_day,
        "official_session_origin": origin.isoformat() if origin else None,
        "signal_timestamp": signal_at.isoformat() if signal_at else None,
        "theoretical_entry_at": (signal_at.isoformat()
                                 if signal_at and evaluation.ready else None),
        "theoretical_entry_price": None,
        "provider": getattr(feats, "price_source", None),
        "source_timestamp": bar_open.isoformat() if isinstance(bar_open, datetime) else None,
        "bar_interval_minutes": BAR_WIDTH_MINUTES,
        "orb_minutes": int(shadow_minutes),
        "features_error": feats.error,
        "strategy_id": s6_sessions.STRATEGY_ID,
        "scanner_variant": s6_sessions.SHADOW_SCANNER_VARIANT,
        "range_minutes": int(shadow_minutes),
        "shadow": True,
        "order_capable": False,
        "state": evaluation.state,
        "ready": bool(evaluation.ready),
        "blocking": list(evaluation.blocking),
        "reason": evaluation.reason,
        "conditions": dict(evaluation.conditions),
        "price": feats.price,
        "range_high": feats.range_high,
        "range_low": feats.range_low,
        "extension_pct": feats.extension_pct,
        "volume_expansion": feats.volume_expansion,
        "market_data_asof": (feats.market_data_asof.isoformat()
                             if feats.market_data_asof else None),
        "entry_quality": quality.as_record() if quality is not None else None,
        "entry_quality_reason": evaluation.detail.get("entry_quality_reason"),
    }
    if live_evaluation is not None:
        record["live_state"] = getattr(live_evaluation, "state", None)
        record["live_ready"] = bool(getattr(live_evaluation, "ready", False))
        live_detail = dict(getattr(live_evaluation, "detail", {}) or {})
        record["live_scanner_variant"] = live_detail.get("scanner_variant")
    return record


def append(record, *, trading_day, env=None) -> bool:
    try:
        path = log_path(trading_day, env=env)
        if path is None:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
        return True
    except Exception:  # noqa: BLE001 - losing an observation costs no trade
        logger.warning("range shadow append failed", exc_info=True)
        return False


def record_cycle(source, *, trading_day, now, env=None, store=None,
                 deadline=None, remaining_seconds=None, stats=None) -> int:
    """After a live cycle: the ORB15 verdict for every symbol it judged.

    Returns how many rows were written. Silent (0) when the session's
    live range already IS 15 minutes -- nothing to compare -- or when
    the collector store has no bars.

    `deadline`, when given, is a zero-argument callable returning True
    once this research work should stop starting new symbols -- see
    scripts/run_live_buy_entry.py's hard tick budget. Omitted (the
    default) it is unlimited, exactly the prior behaviour: every
    existing caller that does not pass one is unaffected. `evaluate_symbol`
    reads the SAME kind of entry-quality baseline `s6_live/kis_bar_features.py`'s
    closed-bar shadow does -- a live py-spy trace on 2026-09-09 caught a
    tick still running here, ~4s/symbol, well after the tick's own 50s
    budget had passed, because this loop had no deadline of its own to
    check.
    """
    from config import s6_sessions
    from s6_live import kis_bar_features

    session = getattr(source, "_session", None) or getattr(source, "session", None)
    if not session:
        return 0
    shadow_minutes = s6_sessions.shadow_orb_minutes_for(session)
    if shadow_minutes is None:
        return 0
    evaluations = getattr(source, "evaluations", None) or {}
    if not evaluations:
        return 0
    if store is not None:
        bars = store
    else:
        # The shadow reads the same session window the live range does, so
        # a session that crosses midnight is scoped by its start date.
        from scanners.base import session_range as srange

        bars = kis_bar_features.load_store(
            session, trading_day, env=env,
            session_date=srange.current_session_date(session, now))
    if bars is None:
        return 0
    started = time.monotonic()
    written = attempted = 0
    stop_reason = "NO_WORK"
    timings = []
    for symbol, live in sorted(evaluations.items()):
        remaining = remaining_seconds() if remaining_seconds is not None else None
        if (deadline is not None and deadline()) or (
                remaining is not None and remaining < MIN_OPERATION_BUDGET_SECONDS):
            stop_reason = "GLOBAL_BUDGET"
            logger.info(
                "RANGE_SHADOW_DEFERRED_BUDGET written=%d remaining=%d",
                written, len(evaluations) - written)
            break
        attempted += 1
        symbol_started = time.monotonic()
        try:
            record = evaluate_symbol(symbol, store=bars, session=session, now=now,
                                     shadow_minutes=shadow_minutes,
                                     live_evaluation=live,
                                     trading_day=trading_day)
        except Exception:  # noqa: BLE001
            logger.debug("range shadow failed for %s", symbol, exc_info=True)
            continue
        if record is not None and append(record, trading_day=trading_day, env=env):
            written += 1
        timings.append((time.monotonic() - symbol_started) * 1000)
    else:
        stop_reason = "COMPLETE"
    elapsed_ms = (time.monotonic() - started) * 1000
    ordered = sorted(timings)
    def percentile(p):
        if not ordered:
            return 0.0
        return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * p))]
    summary = {"attempted": attempted, "completed": written,
               "deferred": max(0, len(evaluations) - attempted),
               "elapsed_ms": round(elapsed_ms, 1), "stop_reason": stop_reason,
               "p50_ms": round(percentile(.5), 1), "p95_ms": round(percentile(.95), 1),
               "max_ms": round(max(ordered) if ordered else 0.0, 1)}
    if stats is not None:
        stats.update(summary)
    logger.info("RANGE_SHADOW_ELAPSED attempted=%(attempted)d completed=%(completed)d deferred=%(deferred)d elapsed_ms=%(elapsed_ms).1f stop_reason=%(stop_reason)s p50_ms=%(p50_ms).1f p95_ms=%(p95_ms).1f max_ms=%(max_ms).1f", summary)
    return written


def read(trading_day, *, env=None) -> List[Dict[str, Any]]:
    path = log_path(trading_day, env=env)
    if path is None or not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def first_ready(rows: Iterable[Dict[str, Any]], *, session=None) -> Dict[str, Dict[str, Any]]:
    """Per symbol, the first row where the ORB15 watch said READY.

    One day's log holds every session, so a caller working on one session
    must SAY so: relabelling another session's row with this session's
    name would compare two different markets' ranges.
    """
    wanted = str(session).upper() if session else None
    out: Dict[str, Dict[str, Any]] = {}
    for row in sorted(rows, key=lambda r: str(r.get("evaluated_at") or "")):
        if wanted and str(row.get("session") or "").upper() != wanted:
            continue
        if row.get("ready") and row.get("symbol") not in out:
            out[row["symbol"]] = row
    return out
