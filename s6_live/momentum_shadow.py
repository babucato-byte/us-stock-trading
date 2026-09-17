"""HMA20 and the HMA-MACD histogram, observed beside S6 and never acting.

Why this exists
---------------
The question is not "should S6 use momentum" but "what is actually making
S6 late". An earlier attempt to answer that assumed the entry path waits
for a retest and that momentum could bypass it. It does not: the ORB
scanner records `retest_confirmed` and scores it, and says so in its own
docstring -- "recorded but never required". The live gate is
`precision_watch`, an AND over conditions with no retest term at all.

So there is nothing to bypass, and adding momentum as a further required
condition could only make entry later. What is missing is evidence: for
each candidate, WHICH existing condition was the last one to turn true,
and would HMA20 or the MACD histogram have turned first.

This records that, minute by minute, and decides nothing.

What it cannot do
-----------------
It cannot enter, exit, rank, or change a verdict. It reads the
collector's bar store (a pure read), computes two indicators from bars
S6 has already loaded, and appends a JSONL row. It imports nothing from
`execution` and never calls a broker or a provider: NEW_KIS_CALLS = 0,
NEW_PROVIDER_CALLS = 0, NEW_LIMITER_RESERVATIONS = 0.

The same code runs for PREMARKET, REGULAR, AFTER_HOURS and
OVERNIGHT_DAYTIME. There is deliberately no session branch anywhere in
this module -- the whole point is that the four are comparable.

Warm-up is measured, not assumed
--------------------------------
HMA needs `length + sqrt(length) - 1` bars before it has any value at
all: 23 for HMA20, against 97 for the HMA89 the framework already uses,
which is why 89 was rejected for this window. The HMA-MACD needs its
slow HMA (26 -> 30 bars) plus a 9-period signal on top.

Those are arithmetic. What matters operationally is what fraction of
real candidate evaluations actually HAVE enough bars, especially in a
thin DAYTIME session, and that is a measurement -- `availability()`
answers it from the recorded rows rather than from the formula.

An indicator without enough bars is `available=False`. It is never
positive and never negative: unavailable is its own answer, the same
way every other uncertainty in S6 is.
"""

import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

SUBDIR = "momentum_shadow"

#: Approved explicitly for this window. The framework's existing periods
#: are 89 and 200, whose first values land 97 and 213 bars in -- past the
#: early-session window this is meant to observe.
HMA_LENGTH = 20

#: The existing production HMA-MACD. Not a new configuration: the repo
#: has exactly one MACD, `indicators.calculate_hma_macd`, and it is
#: HMA-based rather than EMA-based.
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9

DIRECTION_UP = "UP"
DIRECTION_DOWN = "DOWN"
DIRECTION_FLAT = "FLAT"
DIRECTION_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class MomentumView:
    """Both indicators at one instant, plus why either is missing."""

    symbol: str
    session: Optional[str]
    observed_at: Optional[str] = None
    bar_count: int = 0

    hma_available: bool = False
    hma_value: Optional[float] = None
    hma_previous: Optional[float] = None
    hma_slope: Optional[float] = None
    hma_direction: str = DIRECTION_UNKNOWN

    macd_available: bool = False
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    histogram: Optional[float] = None
    histogram_previous: Optional[float] = None
    histogram_delta: Optional[float] = None

    hma_min_bars: int = 0
    unavailable_reason: Optional[str] = None

    # -- the two derived flags, named for what they would mean ----------
    @property
    def hma_positive(self) -> bool:
        return self.hma_available and self.hma_direction == DIRECTION_UP

    @property
    def hma_negative(self) -> bool:
        return self.hma_available and self.hma_direction == DIRECTION_DOWN

    @property
    def histogram_accelerating(self) -> bool:
        return bool(self.macd_available and self.histogram_delta is not None
                    and self.histogram_delta > 0)

    @property
    def histogram_decelerating(self) -> bool:
        return bool(self.macd_available and self.histogram_delta is not None
                    and self.histogram_delta < 0)

    @property
    def momentum_confirmed(self) -> bool:
        """What a MOMENTUM entry WOULD have required. Observed only."""
        return self.hma_positive and self.histogram_accelerating

    @property
    def momentum_weak(self) -> bool:
        """What a MOMENTUM_STRUCTURE_FAILURE exit WOULD have required.

        BOTH sides, deliberately: neither HMA weakness nor a fading
        histogram is enough on its own.
        """
        return self.hma_negative and self.histogram_decelerating

    def as_record(self) -> Dict[str, Any]:
        row = asdict(self)
        row.update({
            "hma_positive": self.hma_positive,
            "hma_negative": self.hma_negative,
            "histogram_accelerating": self.histogram_accelerating,
            "histogram_decelerating": self.histogram_decelerating,
            "momentum_confirmed": self.momentum_confirmed,
            "momentum_weak": self.momentum_weak,
        })
        return row


def min_bars_for_hma(length=HMA_LENGTH) -> int:
    """Delegated to the framework's own formula, not restated here."""
    from scanners.base.indicators import min_bars_for_hma as _minimum

    return int(_minimum(int(length)))


def _finite(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _frame(bars):
    """The bars S6 already holds, as the frame the indicators expect.

    `Close` is the column name both `indicators.get_close_series` and
    `scanners.base.indicators.close_series` look for first, so one frame
    serves the HMA and the MACD without either being adapted.
    """
    import pandas as pd

    rows, stamps = [], []
    for bar in bars or ():
        close = _finite(getattr(bar, "close", None))
        minute = getattr(bar, "minute", None)
        if close is None or minute is None:
            continue
        rows.append({"Close": close})
        stamps.append(minute)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows, index=pd.DatetimeIndex(stamps, name="Datetime"))


def compute(bars, *, symbol, session, now=None) -> MomentumView:
    """Both indicators from bars already loaded. Pure: no I/O at all.

    Never raises. An indicator that cannot be computed is reported
    unavailable with a reason, because a failed observation must not be
    able to disturb the live cycle it is observing.
    """
    moment = (now or datetime.now(timezone.utc))
    frame = _frame(bars)
    count = int(len(frame))
    base = {"symbol": symbol, "session": session,
            "observed_at": moment.isoformat(), "bar_count": count,
            "hma_min_bars": min_bars_for_hma()}

    if count == 0:
        return MomentumView(unavailable_reason="NO_BARS", **base)

    hma_fields, hma_reason = _hma_fields(frame)
    macd_fields, macd_reason = _macd_fields(frame)
    reason = hma_reason or macd_reason
    return MomentumView(unavailable_reason=reason, **base,
                        **hma_fields, **macd_fields)


def _hma_fields(frame):
    """HMA20 value, previous and slope, via the framework's fast HMA."""
    try:
        from scanners.base.indicators import hma_series

        series = hma_series(frame, HMA_LENGTH)
    except Exception:  # noqa: BLE001 - an observation, never a failure
        logger.debug("HMA%d unavailable", HMA_LENGTH, exc_info=True)
        return _empty_hma(), "HMA_COMPUTE_FAILED"

    values = [v for v in (_finite(x) for x in list(series)) if v is not None]
    if len(values) < 2:
        return _empty_hma(), "HMA_INSUFFICIENT_BARS"

    current, previous = values[-1], values[-2]
    slope = current - previous
    if slope > 0:
        direction = DIRECTION_UP
    elif slope < 0:
        direction = DIRECTION_DOWN
    else:
        direction = DIRECTION_FLAT
    return ({"hma_available": True, "hma_value": current,
             "hma_previous": previous, "hma_slope": slope,
             "hma_direction": direction}, None)


def _empty_hma():
    return {"hma_available": False, "hma_value": None, "hma_previous": None,
            "hma_slope": None, "hma_direction": DIRECTION_UNKNOWN}


def _macd_fields(frame):
    """Histogram, its previous value and the delta, from the existing
    production HMA-MACD."""
    try:
        table = _hma_macd(frame)
    except Exception:  # noqa: BLE001
        logger.debug("HMA-MACD unavailable", exc_info=True)
        return _empty_macd(), "MACD_COMPUTE_FAILED"

    histogram = [v for v in (_finite(x) for x in
                             list(table.get("hma_macd_histogram", [])))
                 if v is not None]
    if len(histogram) < 2:
        return _empty_macd(), "MACD_INSUFFICIENT_BARS"

    current, previous = histogram[-1], histogram[-2]
    return ({"macd_available": True,
             "macd": _finite(list(table["hma_macd_line"])[-1]),
             "macd_signal": _finite(list(table["hma_macd_signal"])[-1]),
             "histogram": current, "histogram_previous": previous,
             "histogram_delta": current - previous}, None)


def _hma_macd(frame):
    """`indicators.calculate_hma_macd`'s configuration, on the fast HMA.

    Same formula, same 12/26/9, same EWM signal with `min_periods` --
    the ONLY difference is which HMA implementation computes the two
    lines. The reference `indicators.hma` is a per-bar rolling `apply`
    and measured 51.9ms per symbol here; `hma_series` is the convolution
    the scanner framework already uses for exactly this reason ("the
    reference implementation cost 0.90s per symbol ... and this returns
    the same numbers in under a millisecond").

    At roughly 40 watched symbols on a one-minute fast-watch tick, 51.9ms
    each is ~2s of pure arithmetic added to a latency-sensitive path, for
    a feature that is only supposed to observe. Equivalence to the
    reference is pinned by a test rather than assumed.
    """
    import pandas as pd

    from scanners.base.indicators import hma_series

    fast = hma_series(frame, MACD_FAST)
    slow = hma_series(frame, MACD_SLOW)
    macd_line = fast - slow
    signal_line = macd_line.ewm(span=MACD_SIGNAL, adjust=False,
                                min_periods=MACD_SIGNAL).mean()
    return pd.DataFrame({
        "hma_macd_line": macd_line,
        "hma_macd_signal": signal_line,
        "hma_macd_histogram": macd_line - signal_line,
    })


def _empty_macd():
    return {"macd_available": False, "macd": None, "macd_signal": None,
            "histogram": None, "histogram_previous": None,
            "histogram_delta": None}


# -- the log -------------------------------------------------------------

def log_path(trading_day, *, env=None) -> Optional[Path]:
    """Beside the other shadow logs, or None when no root is configured."""
    source = env if env is not None else os.environ
    root = source.get("SCANNER_ANALYTICS_DIR") or source.get("TRADING_LOG_DIR")
    if not root or not str(root).strip():
        return None
    return Path(str(root).strip()) / SUBDIR / f"{trading_day}.jsonl"


def append(record, *, trading_day, env=None) -> bool:
    """One JSONL row. Returns False rather than raising: an observation
    that cannot be written must not disturb the cycle it observes."""
    target = log_path(trading_day, env=env)
    if target is None:
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
        return True
    except Exception:  # noqa: BLE001
        logger.debug("momentum shadow row not written", exc_info=True)
        return False


def observe(evaluation, view, *, trading_day, env=None, extra=None) -> bool:
    """Record one tick: the live verdict, and what momentum said.

    The live `evaluation` is read, never modified -- its conditions are
    copied into the row so the analysis can ask which one was last to
    turn true without re-running anything.
    """
    conditions = dict(getattr(evaluation, "conditions", None) or {})
    record = {
        "kind": "ENTRY_TICK",
        "symbol": getattr(evaluation, "symbol", None) or view.symbol,
        "session": getattr(evaluation, "session", None) or view.session,
        "state": getattr(evaluation, "state", None),
        "evaluated_at": getattr(evaluation, "evaluated_at", None),
        "conditions": conditions,
        "momentum": view.as_record(),
    }
    if extra:
        record.update(extra)
    return append(record, trading_day=trading_day, env=env)


def observe_position(view, *, position_id, trading_day, existing_exit=None,
                     env=None) -> bool:
    """Record one tick for an OPEN position -- the exit side.

    `existing_exit` is whatever the live exit path decided this tick, so
    the pairing of a shadow weakness with the real structural exit can be
    done later from the rows alone.
    """
    record = {
        "kind": "EXIT_TICK",
        "position_id": position_id,
        "symbol": view.symbol,
        "session": view.session,
        "existing_exit": existing_exit,
        "momentum": view.as_record(),
    }
    return append(record, trading_day=trading_day, env=env)


#: Below this many seconds left in the tick, stop starting new symbols.
#: Same guard and same number as `range_shadow`: research must never be
#: why a candidate misses its tick.
MIN_OPERATION_BUDGET_SECONDS = 20.0


def record_cycle(source, *, trading_day, now, env=None, store=None,
                 deadline=None, remaining_seconds=None) -> int:
    """After a live cycle: what momentum said for every symbol it judged.

    Returns how many rows were written. Reads the collector's store and
    the evaluations the cycle already produced; computes nothing the
    cycle needs and changes nothing it decided.

    `deadline` / `remaining_seconds` are the caller's tick budget, used
    exactly as `range_shadow.record_cycle` uses them -- a live py-spy
    trace once caught that loop still running ~4s/symbol well past the
    tick's budget because it had no deadline of its own.
    """
    session = getattr(source, "_session", None) or getattr(source, "session", None)
    evaluations = getattr(source, "evaluations", None) or {}
    if not session or not evaluations:
        return 0

    if store is not None:
        bars_store = store
    else:
        from s6_live import kis_bar_features
        from scanners.base import session_range as srange

        bars_store = kis_bar_features.load_store(
            session, trading_day, env=env,
            session_date=srange.current_session_date(session, now))
    if bars_store is None:
        return 0

    written = 0
    for symbol, live in sorted(evaluations.items()):
        remaining = remaining_seconds() if remaining_seconds is not None else None
        if (deadline is not None and deadline()) or (
                remaining is not None and remaining < MIN_OPERATION_BUDGET_SECONDS):
            logger.info("MOMENTUM_SHADOW_BUDGET_STOP written=%d", written)
            break
        try:
            bars = bars_store.bars(symbol, session) or []
        except Exception:  # noqa: BLE001 - one symbol, not the sweep
            continue
        view = compute(bars, symbol=symbol, session=session, now=now)
        if observe(live, view, trading_day=trading_day, env=env):
            written += 1
    return written


def read(trading_day, *, env=None) -> List[Dict[str, Any]]:
    target = log_path(trading_day, env=env)
    if target is None or not target.exists():
        return []
    rows = []
    try:
        for line in target.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return []
    return rows


# -- analysis, derived from the rows rather than from new state ----------

def first_true(rows: Iterable[Dict[str, Any]], symbol) -> Dict[str, Any]:
    """The first moment each existing gate, and each momentum flag,
    became true for one symbol.

    Derived by reading the log rather than by keeping a per-symbol store
    in the live path: the rows already carry every tick, so first-true is
    a fold over them and the cycle stays stateless.
    """
    name = str(symbol).upper()
    gates: Dict[str, str] = {}
    momentum: Dict[str, str] = {}
    ready_at = None
    for row in rows:
        if row.get("kind") != "ENTRY_TICK":
            continue
        if str(row.get("symbol") or "").upper() != name:
            continue
        stamp = row.get("evaluated_at") or (row.get("momentum") or {}).get("observed_at")
        for gate, verdict in (row.get("conditions") or {}).items():
            if verdict == "PASS" and gate not in gates:
                gates[gate] = stamp
        view = row.get("momentum") or {}
        for flag in ("hma_available", "hma_positive", "hma_negative",
                     "macd_available", "histogram_accelerating",
                     "histogram_decelerating", "momentum_confirmed",
                     "momentum_weak"):
            if view.get(flag) and flag not in momentum:
                momentum[flag] = stamp
        if ready_at is None and row.get("state") == "READY_TO_BUY":
            ready_at = stamp
    return {"symbol": name, "gates_first_pass_at": gates,
            "momentum_first_true_at": momentum, "entry_ready_at": ready_at}


def last_blocking_gate(summary) -> Dict[str, Any]:
    """Which existing gate turned true LAST -- the one actually delaying
    entry. `None` when a gate never passed at all, which is a different
    answer from "it passed late"."""
    gates = summary.get("gates_first_pass_at") or {}
    dated = {g: s for g, s in gates.items() if s}
    if not dated:
        return {"gate": None, "at": None, "reason": "NO_GATE_EVER_PASSED"}
    gate = max(dated, key=lambda g: dated[g])
    return {"gate": gate, "at": dated[gate], "reason": None}


def lead_seconds(summary, flag="momentum_confirmed") -> Optional[float]:
    """How far ahead of the live ENTRY_READY the momentum flag turned.

    Positive means momentum was EARLIER. None when either side is
    missing -- never zero, which would read as "simultaneous".
    """
    ready = summary.get("entry_ready_at")
    when = (summary.get("momentum_first_true_at") or {}).get(flag)
    if not ready or not when:
        return None
    try:
        a = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(ready).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (b - a).total_seconds()


def availability(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Per session: how often each indicator actually had enough bars.

    The DAYTIME question (§5) answered from observations rather than
    from the warm-up formula.
    """
    per: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        view = row.get("momentum") or {}
        session = str(row.get("session") or view.get("session") or "UNKNOWN")
        bucket = per.setdefault(session, {
            "evaluations": 0, "hma_available": 0, "macd_available": 0,
            "both_available": 0, "first_hma_at": None, "first_macd_at": None,
            "min_bars_seen": None, "max_bars_seen": None})
        bucket["evaluations"] += 1
        stamp = view.get("observed_at")
        count = view.get("bar_count")
        if isinstance(count, int):
            lo, hi = bucket["min_bars_seen"], bucket["max_bars_seen"]
            bucket["min_bars_seen"] = count if lo is None else min(lo, count)
            bucket["max_bars_seen"] = count if hi is None else max(hi, count)
        if view.get("hma_available"):
            bucket["hma_available"] += 1
            if bucket["first_hma_at"] is None:
                bucket["first_hma_at"] = stamp
        if view.get("macd_available"):
            bucket["macd_available"] += 1
            if bucket["first_macd_at"] is None:
                bucket["first_macd_at"] = stamp
        if view.get("hma_available") and view.get("macd_available"):
            bucket["both_available"] += 1
    for bucket in per.values():
        total = bucket["evaluations"] or 1
        bucket["hma_available_pct"] = round(100.0 * bucket["hma_available"] / total, 1)
        bucket["macd_available_pct"] = round(100.0 * bucket["macd_available"] / total, 1)
        bucket["both_available_pct"] = round(100.0 * bucket["both_available"] / total, 1)
    return per


def compute_timed(bars, *, symbol, session, now=None):
    """`(view, timings_ms)` -- the §6 measurement, without a benchmark."""
    frame_started = time.perf_counter()
    frame = _frame(bars)
    frame_ms = (time.perf_counter() - frame_started) * 1000.0

    hma_started = time.perf_counter()
    hma_fields, hma_reason = _hma_fields(frame) if len(frame) else (_empty_hma(), "NO_BARS")
    hma_ms = (time.perf_counter() - hma_started) * 1000.0

    macd_started = time.perf_counter()
    macd_fields, macd_reason = _macd_fields(frame) if len(frame) else (_empty_macd(), "NO_BARS")
    macd_ms = (time.perf_counter() - macd_started) * 1000.0

    moment = now or datetime.now(timezone.utc)
    view = MomentumView(
        symbol=symbol, session=session, observed_at=moment.isoformat(),
        bar_count=int(len(frame)), hma_min_bars=min_bars_for_hma(),
        unavailable_reason=hma_reason or macd_reason,
        **hma_fields, **macd_fields)
    return view, {"HMA_COMPUTE_MS": round(hma_ms, 3),
                  "MACD_COMPUTE_MS": round(macd_ms, 3),
                  "FRAME_BUILD_MS": round(frame_ms, 3),
                  "TOTAL_AUX_COMPUTE_MS": round(frame_ms + hma_ms + macd_ms, 3)}
