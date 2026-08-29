"""Every READY candidate, and what happened to it.

The measurement that has to exist before any strategy change is worth
discussing. Right now the only candidates leaving a trace are the ones
that became orders: a READY signal refused at a gate disappears, so
"does this gate block good trades" is unanswerable, and every proposed
threshold is an opinion.

This records the signal as it stood, the features it was computed from,
which gate refused it first, and -- separately, later -- what the price
did afterwards. A candidate blocked for insufficient cash that then rose
4% is evidence; a candidate blocked by a volume threshold that then went
nowhere is also evidence. Neither exists without this.

Deliberately NOT a decision input
---------------------------------
Nothing here feeds the gate, the watch or the sizing. It is written
after the decision is made and read by people. A log that could change
an outcome would need the same scrutiny as the execution path, and it is
not worth buying observability at that price.

Written as JSONL beside the other shared state rather than into the
order database. The trading path already contends on that database, and
an observability write must never be the thing that delays an order.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: The candidate reached the gate and no check refused it.
OUTCOME_EXECUTABLE = "EXECUTABLE"
#: A gate refused it. `first_blocked_by` names which.
OUTCOME_BLOCKED = "BLOCKED"
#: The precision watch never offered it for entry.
OUTCOME_NOT_READY = "NOT_READY"
#: An order was actually submitted for it.
OUTCOME_SUBMITTED = "BUY_SUBMITTED"


def log_path(trading_day, *, env=None):
    """Where today's records live, or None if nowhere is configured.

    No production default, for the reason `slippage_log.log_path`
    documents: a module that guessed one had fixture data written into
    the real dataset by the test suite, and nothing about it looked
    wrong afterwards.
    """
    env = env if env is not None else os.environ
    root = env.get("SHADOW_SIGNAL_DIR") or env.get("SCANNER_DATA_ROOT")
    if not root:
        return None
    return Path(root) / "shadow_signals" / f"{trading_day}.jsonl"


def _feature_fields(features) -> Dict[str, Any]:
    """The values the signal was computed from, as they stood.

    Recorded from the snapshot rather than recomputed later: the point is
    what the strategy SAW, and a value re-derived afterwards is a
    different number that happens to share a name.
    """
    if features is None:
        return {}
    def _get(name):
        value = getattr(features, name, None)
        if isinstance(value, datetime):
            return value.isoformat()
        return value

    return {
        "price": _get("price"),
        "vwap": _get("vwap"),
        "ema9": _get("ema9"),
        "ema21": _get("ema21"),
        "range_high": _get("range_high"),
        "range_low": _get("range_low"),
        "extension_pct": _get("extension_pct"),
        "volume": _get("volume"),
        "volume_status": _get("volume_status"),
        "volume_expansion": _get("volume_expansion"),
        "bar_count": _get("bar_count"),
        "market_data_asof": _get("market_data_asof"),
        "price_source": _get("price_source"),
        "volume_source": _get("volume_source"),
        "feed_status": _get("feed_status"),
        "gap_detected": _get("gap_detected"),
    }


def build_record(*, symbol, session, outcome, strategy_id,
                 strategy_version=None, features=None, candidate=None,
                 gate_results=None, first_blocked_by=None, watch_blocking=None,
                 target_qty=None, orderable_usd=None, now=None) -> Dict[str, Any]:
    """One candidate's signal, flattened for storage.

    `first_blocked_by` is stored separately from the full gate map on
    purpose. Every later gate's verdict is conditional on the earlier
    ones having passed, so a list of failures invites the reader to treat
    them as independent reasons when only the first one actually decided
    anything.
    """
    moment = now or datetime.now(timezone.utc)
    row = {
        "logged_at": moment.isoformat(),
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "symbol": str(symbol or "").upper(),
        "session": session,
        "outcome": outcome,
        "first_blocked_by": first_blocked_by,
        "watch_blocking": list(watch_blocking or ()),
        "gate_results": dict(gate_results or {}),
        "target_qty": target_qty,
        "orderable_usd": orderable_usd,
    }
    row.update(_feature_fields(features))
    if candidate:
        for key in ("rank", "score", "variant", "generated_at",
                    "scanner_run_id"):
            if key in candidate:
                row[f"candidate_{key}"] = candidate.get(key)
    return row


def append(record, *, trading_day, env=None) -> bool:
    """Append one row. Never raises.

    Losing an observation must never cost a trade, so every failure here
    is swallowed and reported as False. Append-only because a signal is a
    historical fact: rewriting one would destroy the record this exists
    to keep.
    """
    try:
        path = log_path(trading_day, env=env)
        if path is None:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
        return True
    except Exception:  # noqa: BLE001
        logger.warning("could not append a shadow signal record",
                       exc_info=True)
        return False


def read(trading_day, *, env=None) -> List[Dict[str, Any]]:
    """Every row for a day. A missing file is an empty day, not an error."""
    path = log_path(trading_day, env=env)
    rows = []
    try:
        if path is None or not path.exists():
            return rows
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                # One malformed line must not discard the rest of the day.
                logger.warning("skipping an unreadable shadow signal row")
    except Exception:  # noqa: BLE001
        logger.warning("could not read shadow signals for %s", trading_day,
                       exc_info=True)
    return rows


def blocked_reasons(trading_day, *, env=None) -> Dict[str, int]:
    """How often each gate was the FIRST to refuse a candidate.

    The question this answers is "which gate is actually deciding", which
    is not the same as "which conditions were unmet".
    """
    counts: Dict[str, int] = {}
    for row in read(trading_day, env=env):
        reason = row.get("first_blocked_by")
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
    return counts
