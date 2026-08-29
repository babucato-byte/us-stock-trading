"""How long a symbol keeps its realtime slot, and why it lost it.

Measurement before policy
-------------------------
The obvious control for slot churn is hysteresis: require a challenger
to out-rank the incumbent by some margin before swapping. The margin has
to be a number, and there is currently no evidence for any particular
one. Picking 1.15 today would mean every later question about churn
gets answered by a constant nobody measured -- and it would be hard to
argue with afterwards precisely because it would already be in
production.

So this records what actually happens and decides nothing. How often
slots turn over, how long symbols hold them, and how much rank actually
separated the incumbent from its replacement are all answerable from
this log, and a hysteresis value chosen afterwards can be defended.

Churn is not automatically bad
------------------------------
A slot changing hands often may be the ranking working. What makes it
bad is a symbol losing its slot before it finished WARMING_UP, because
that slot produced nothing: the stream was spent accumulating history
that gets discarded. That case is worth naming separately, and it is.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

REASON_OUTRANKED = "OUTRANKED"
REASON_LIFECYCLE_CLAIM = "LIFECYCLE_CLAIM"
REASON_WARMUP_FAILED = "WARMUP_FAILED"
REASON_INVALIDATED = "INVALIDATED"
REASON_SESSION_END = "SESSION_END"


def log_path(trading_day, *, env=None):
    """No production default -- see `slippage_log.log_path`."""
    env = env if env is not None else os.environ
    root = env.get("SLOT_ROTATION_DIR") or env.get("SCANNER_DATA_ROOT")
    if not root:
        return None
    return Path(root) / "slot_rotation" / f"{trading_day}.jsonl"


def build_record(*, symbol, session, entered_at, removed_at,
                 replacement_reason, state_at_removal=None,
                 incumbent_rank=None, replacement_symbol=None,
                 replacement_rank=None, now=None) -> Dict[str, Any]:
    """One slot's tenure, and what ended it."""
    duration = None
    if isinstance(entered_at, datetime) and isinstance(removed_at, datetime):
        seconds = (removed_at - entered_at).total_seconds()
        duration = seconds if seconds >= 0 else None

    #: How much better the replacement actually was. The input to any
    #: future hysteresis argument -- a swap for a 0.1% rank difference
    #: and one for a 40% difference are not the same event.
    rank_delta = None
    if isinstance(incumbent_rank, (int, float)) and isinstance(
            replacement_rank, (int, float)):
        rank_delta = incumbent_rank - replacement_rank

    return {
        "logged_at": (now or datetime.now(timezone.utc)).isoformat(),
        "symbol": str(symbol or "").upper(),
        "session": session,
        "entered_at": _iso(entered_at),
        "removed_at": _iso(removed_at),
        "slot_duration_seconds": duration,
        "replacement_reason": replacement_reason,
        "state_at_removal": state_at_removal,
        "incumbent_rank": incumbent_rank,
        "replacement_symbol": (str(replacement_symbol).upper()
                               if replacement_symbol else None),
        "replacement_rank": replacement_rank,
        "rank_delta": rank_delta,
        #: A slot lost mid-warmup produced nothing at all: the stream
        #: was spent accumulating history that is now discarded.
        "wasted_warmup": str(state_at_removal or "").upper() == "WARMING_UP",
    }


def append(record, *, trading_day, env=None) -> bool:
    try:
        path = log_path(trading_day, env=env)
        if path is None:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
        return True
    except Exception:  # noqa: BLE001
        logger.warning("could not append a slot rotation record", exc_info=True)
        return False


def read(trading_day, *, env=None) -> List[Dict[str, Any]]:
    path = log_path(trading_day, env=env)
    rows = []
    try:
        if path is None or not path.exists():
            return rows
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    logger.warning("skipping an unreadable rotation row")
    except Exception:  # noqa: BLE001
        logger.warning("could not read slot rotation for %s", trading_day,
                       exc_info=True)
    return rows


def churn_summary(trading_day, *, env=None) -> Dict[str, Any]:
    """What the day's rotation actually looked like.

    Deliberately descriptive. It reports tenure and how much rank
    separated swaps; it does not recommend a hysteresis value, because
    that is the decision this data exists to inform rather than
    pre-empt.
    """
    rows = read(trading_day, env=env)
    durations = sorted(r["slot_duration_seconds"] for r in rows
                       if isinstance(r.get("slot_duration_seconds"),
                                     (int, float)))
    deltas = sorted(abs(r["rank_delta"]) for r in rows
                    if isinstance(r.get("rank_delta"), (int, float)))
    by_reason: Dict[str, int] = {}
    for row in rows:
        reason = row.get("replacement_reason") or "UNKNOWN"
        by_reason[reason] = by_reason.get(reason, 0) + 1
    return {
        "rotations": len(rows),
        "by_reason": by_reason,
        "median_slot_seconds": (durations[len(durations) // 2]
                                if durations else None),
        "shortest_slot_seconds": durations[0] if durations else None,
        "median_rank_delta": deltas[len(deltas) // 2] if deltas else None,
        #: Slots that produced nothing because warmup never finished.
        "wasted_warmups": sum(1 for r in rows if r.get("wasted_warmup")),
    }


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value
