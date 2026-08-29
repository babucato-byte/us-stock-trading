"""The same features computed twice: as they are, and on closed bars only.

The question this answers
-------------------------
Every live feature is currently computed over ALL bars, and the last of
those is the minute in progress. Its close is whatever the most recent
print happened to be, and its volume is a fraction of what that minute
will finish with -- both keep moving after the decision is made. A
breakout read off a partial bar can un-break before the minute ends.

Whether that matters is an empirical question, and it has never been
measured here. This computes both readings side by side and records the
difference. It changes NOTHING: production continues to use the live
reading, and this exists so that a later argument for switching to
closed bars can be made from evidence rather than from the plausible
story above.

What can and cannot be compared
-------------------------------
`build_from_bars` draws its price, EMAs and expansion from the BAR
list, and its volume and VWAP from the session ACCUMULATOR, which
aggregates trades as they arrive and cannot be replayed without the
in-progress minute's trades. So the bar-derived fields are compared and
the accumulator-derived ones are reported NOT_COMPARABLE rather than
compared against themselves -- a difference of zero there would be an
artefact of how the value is stored, and it would read as evidence that
the in-progress bar does not matter.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MODE_LIVE = "CURRENT_LIVE"
MODE_SHADOW = "CLOSED_BAR_SHADOW"

#: Fields drawn from the bar list, which the shadow genuinely recomputes.
COMPARABLE_FIELDS = ("price", "ema9", "ema21", "volume_expansion", "bar_count")

#: Drawn from the session accumulator, which cannot be replayed without
#: the in-progress minute's trades.
NOT_COMPARABLE = "NOT_COMPARABLE"
ACCUMULATOR_FIELDS = ("vwap", "volume")


def log_path(trading_day, *, env=None):
    env = env if env is not None else os.environ
    root = (env.get("CLOSED_BAR_SHADOW_DIR") or env.get("SCANNER_DATA_ROOT")
            or "/home/ubuntu/releases/us-stock-trading/shared/scanner")
    return Path(root) / "closed_bar_shadow" / f"{trading_day}.jsonl"


def closed_bars(bars, *, now):
    """Only the bars whose minute has finished.

    The boundary is the minute `now` falls in: a bar stamped 15:42 is
    still accumulating for every moment inside 15:42, and is complete
    from 15:43 onwards.
    """
    if not bars:
        return []
    current_minute = now.replace(second=0, microsecond=0)
    return [b for b in bars if b.minute < current_minute]


class _ClosedBarView:
    """The store as it would have looked without the minute in progress.

    A view rather than a copy: it wraps the real store and filters one
    method. Nothing here writes, so the live path cannot be perturbed by
    the shadow being computed.
    """

    def __init__(self, store, *, now):
        self._store = store
        self._now = now

    def bars(self, symbol, session):
        return closed_bars(self._store.bars(symbol, session), now=self._now)

    def accumulator(self, symbol, session):
        return self._store.accumulator(symbol, session)

    def feed_status(self, *, now=None):
        return self._store.feed_status(now=now)


def _value(features, name):
    if features is None:
        return None
    return getattr(features, name, None)


def compare(symbol, *, store, session, now=None) -> Optional[Dict[str, Any]]:
    """Both readings and the gap between them, or None if neither exists."""
    from s6_live import kis_bar_features

    moment = now or datetime.now(timezone.utc)
    try:
        live = kis_bar_features.build_from_bars(symbol, store=store,
                                                session=session, now=moment)
        shadow = kis_bar_features.build_from_bars(
            symbol, store=_ClosedBarView(store, now=moment), session=session,
            now=moment)
    except Exception:  # noqa: BLE001 - a research comparison must not raise
        logger.warning("closed-bar comparison failed for %s", symbol,
                       exc_info=True)
        return None
    if live is None and shadow is None:
        return None

    differences = {}
    for field in COMPARABLE_FIELDS:
        live_value = _value(live, field)
        shadow_value = _value(shadow, field)
        entry = {"live": live_value, "shadow": shadow_value}
        if isinstance(live_value, (int, float)) and isinstance(
                shadow_value, (int, float)):
            entry["delta"] = live_value - shadow_value
            entry["delta_bps"] = ((live_value - shadow_value) / shadow_value
                                  * 10000.0) if shadow_value else None
        else:
            # One side could not be computed. That is itself the finding
            # -- most often the shadow having no closed bar yet, early in
            # a session -- and it is recorded rather than skipped.
            entry["delta"] = None
            entry["delta_bps"] = None
        differences[field] = entry

    for field in ACCUMULATOR_FIELDS:
        differences[field] = {"live": _value(live, field),
                              "shadow": NOT_COMPARABLE, "delta": None,
                              "delta_bps": None}

    return {
        "observed_at": moment.isoformat(),
        "symbol": str(symbol or "").upper(),
        "session": session,
        "live_bar_count": _value(live, "bar_count"),
        "shadow_bar_count": _value(shadow, "bar_count"),
        #: True when the in-progress minute is the ONLY bar there is, so
        #: the shadow has nothing at all to work from. Early-session
        #: decisions fall here, and they are exactly the ones a closed-bar
        #: rule would have to defer.
        "shadow_has_nothing": shadow is None,
        "fields": differences,
    }


def append(record, *, trading_day, env=None) -> bool:
    """Append one comparison. Never raises."""
    try:
        path = log_path(trading_day, env=env)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
        return True
    except Exception:  # noqa: BLE001
        logger.warning("could not append a closed-bar comparison",
                       exc_info=True)
        return False


def read(trading_day, *, env=None) -> List[Dict[str, Any]]:
    path = log_path(trading_day, env=env)
    rows = []
    try:
        if not path.exists():
            return rows
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    logger.warning("skipping an unreadable comparison row")
    except Exception:  # noqa: BLE001
        logger.warning("could not read comparisons for %s", trading_day,
                       exc_info=True)
    return rows


def disagreements(trading_day, *, field="price", env=None) -> Dict[str, Any]:
    """How far apart the two readings ran, for one field.

    Rows where the shadow had nothing are counted separately rather than
    averaged in: "no closed bar yet" is a different fact from "the two
    readings agreed", and pooling them would understate the gap by
    exactly the early-session moments where it is largest.
    """
    rows = read(trading_day, env=env)
    deltas = []
    nothing = 0
    for row in rows:
        if row.get("shadow_has_nothing"):
            nothing += 1
            continue
        entry = (row.get("fields") or {}).get(field) or {}
        if isinstance(entry.get("delta_bps"), (int, float)):
            deltas.append(abs(entry["delta_bps"]))
    ordered = sorted(deltas)
    return {
        "field": field,
        "observations": len(rows),
        "compared": len(ordered),
        "shadow_had_nothing": nothing,
        "median_abs_bps": ordered[len(ordered) // 2] if ordered else None,
        "max_abs_bps": ordered[-1] if ordered else None,
    }
