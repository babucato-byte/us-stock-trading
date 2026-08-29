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
from datetime import datetime, timedelta, timezone
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


#: How the two readings classify the same candidate at the same moment.
#:
#: `BOTH` and `NEITHER` are agreement. The two that matter are
#: `LIVE_ONLY` -- a signal the in-progress minute created, which is the
#: shape of a breakout that can un-break before the minute closes -- and
#: `SHADOW_ONLY`, a signal the partial bar is currently suppressing.
CLASS_BOTH = "BOTH"
CLASS_LIVE_ONLY = "LIVE_ONLY"
CLASS_SHADOW_ONLY = "SHADOW_ONLY"
CLASS_NEITHER = "NEITHER"


def classify(*, live_ready, shadow_ready) -> str:
    if live_ready and shadow_ready:
        return CLASS_BOTH
    if live_ready:
        return CLASS_LIVE_ONLY
    if shadow_ready:
        return CLASS_SHADOW_ONLY
    return CLASS_NEITHER


def _evaluate(symbol, features, *, session, now, conn):
    """Readiness for one set of features. Never raises."""
    if features is None:
        return None
    try:
        from s6_live import precision_watch

        return precision_watch.evaluate(symbol, session=session, now=now,
                                        features=features, conn=conn)
    except Exception:  # noqa: BLE001 - research
        logger.warning("could not evaluate readiness for %s", symbol,
                       exc_info=True)
        return None


def compare_readiness(symbol, *, store, session, now=None, conn=None):
    """Would each reading have called this candidate READY?

    The feature deltas say the two readings differ; this says whether
    the difference reaches the decision. A 900bps gap in a field no gate
    consults changes nothing, and a small one that crosses a threshold
    changes everything -- only this distinguishes them.
    """
    from s6_live import kis_bar_features

    moment = now or datetime.now(timezone.utc)
    try:
        live_features = kis_bar_features.build_from_bars(
            symbol, store=store, session=session, now=moment)
        shadow_features = kis_bar_features.build_from_bars(
            symbol, store=_ClosedBarView(store, now=moment), session=session,
            now=moment)
    except Exception:  # noqa: BLE001
        logger.warning("could not build features for %s", symbol, exc_info=True)
        return None
    if live_features is None and shadow_features is None:
        return None

    live = _evaluate(symbol, live_features, session=session, now=moment,
                     conn=conn)
    shadow = _evaluate(symbol, shadow_features, session=session, now=moment,
                       conn=conn)
    live_ready = bool(getattr(live, "ready", False))
    shadow_ready = bool(getattr(shadow, "ready", False))

    return {
        "observed_at": moment.isoformat(),
        "symbol": str(symbol or "").upper(),
        "session": session,
        "live_ready": live_ready,
        "shadow_ready": shadow_ready,
        "classification": classify(live_ready=live_ready,
                                   shadow_ready=shadow_ready),
        "live_state": getattr(live, "state", None),
        "shadow_state": getattr(shadow, "state", None),
        #: What each reading was waiting for. When the classification is
        #: LIVE_ONLY, the shadow's blocking list names the condition the
        #: in-progress minute is what satisfied.
        "live_blocking": list(getattr(live, "blocking", ()) or ()),
        "shadow_blocking": list(getattr(shadow, "blocking", ()) or ()),
        "volume_expansion": {
            "live": _value(live_features, "volume_expansion"),
            "shadow": _value(shadow_features, "volume_expansion")},
        "price": {"live": _value(live_features, "price"),
                  "shadow": _value(shadow_features, "price")},
        "shadow_has_nothing": shadow_features is None,
    }


def classification_counts(trading_day, *, env=None) -> Dict[str, int]:
    """How often each reading would have signalled, over a day.

    Rows carrying no classification are ignored rather than counted as
    agreement: a plain feature comparison has no readiness in it, and
    folding those into NEITHER would report the two readings agreeing
    every time nobody asked the question.
    """
    counts = {CLASS_BOTH: 0, CLASS_LIVE_ONLY: 0, CLASS_SHADOW_ONLY: 0,
              CLASS_NEITHER: 0}
    for row in read(trading_day, env=env):
        name = row.get("classification")
        if name in counts:
            counts[name] += 1
    return counts


#: Marks after a signal at which the two readings are scored against
#: each other. The same set the post-exit tracker uses, so a signal and
#: an exit are answerable on one timeline.
FORWARD_MINUTES = (5, 15, 30, 60)

#: How far from a mark a bar may sit and still answer for it.
#:
#: A bar two minutes either side of T+15 is a fair reading of where the
#: price was. One forty minutes away is a different fact wearing the
#: same label, and these marks are only worth having because they are
#: comparable across signals.
NEAREST_BAR_TOLERANCE_MINUTES = 2.0


def forward_returns(symbol, *, store, session, signal_at, signal_price,
                    minutes=FORWARD_MINUTES, tolerance_minutes=None):
    """What the price did after a signal, at each mark.

    Research, computed from stored bars after the fact -- never in the
    entry path. A mark with no bar near it is None rather than the
    nearest available price: substituting a bar from far away would let
    a quiet symbol look like it held its move.
    """
    limit = (NEAREST_BAR_TOLERANCE_MINUTES if tolerance_minutes is None
             else float(tolerance_minutes))
    out = {}
    try:
        bars = store.bars(symbol, session) or []
        base = float(signal_price) if signal_price else None
    except Exception:  # noqa: BLE001
        return {f"T+{m}": None for m in minutes}
    for offset in minutes:
        key = f"T+{offset}"
        out[key] = None
        if base is None or base <= 0 or not bars:
            continue
        target = signal_at + timedelta(minutes=offset)
        nearest = min(bars, key=lambda b: abs((b.minute - target).total_seconds()))
        drift = abs((nearest.minute - target).total_seconds()) / 60.0
        if drift > limit:
            continue
        out[key] = (float(nearest.close) / base - 1.0) * 100.0
    return out


def score_classifications(trading_day, *, env=None, mark="T+15"):
    """How each classification's signals actually performed.

    Split by classification so the two disagreements can be judged
    separately: `LIVE_ONLY` signals are the ones the in-progress minute
    created, and whether they were worth taking is the entire question.

    Signals with no forward price are counted, not scored. A mark that
    could not be read is not a flat return, and averaging it in as zero
    would drag every group toward "made no difference".
    """
    groups = {}
    for row in read(trading_day, env=env):
        name = row.get("classification")
        if name is None:
            continue
        bucket = groups.setdefault(name, {"signals": 0, "scored": 0,
                                          "returns": []})
        bucket["signals"] += 1
        value = (row.get("forward_returns") or {}).get(mark)
        if isinstance(value, (int, float)):
            bucket["scored"] += 1
            bucket["returns"].append(value)
    summary = {}
    for name, bucket in groups.items():
        values = sorted(bucket["returns"])
        summary[name] = {
            "signals": bucket["signals"],
            "scored": bucket["scored"],
            "unscored": bucket["signals"] - bucket["scored"],
            "median_return_pct": values[len(values) // 2] if values else None,
            "win_rate": (sum(1 for v in values if v > 0) / len(values)
                         if values else None),
        }
    return {"mark": mark, "by_classification": summary}
