"""Bars either side of a session boundary, kept distinguishable.

Why history is not reset at the boundary
---------------------------------------
HMA20 needs 24 bars before it has a slope and the HMA-MACD needs 39, so a
session that starts from nothing has no momentum for its first ~39
minutes -- exactly the window where a breakout happens. Carrying the
previous session's bars removes that cold start.

What it must not do is let yesterday's trend read as today's
confirmation. So the two are counted separately and
`MomentumResult.inherited_context` says out loud when the numbers lean on
history from before the boundary.

Nothing here fabricates a bar. A minute with no print stays absent, in
both sessions: §6 is explicit that sparse DAYTIME minutes must not be
filled in to satisfy anything, and stabilization is measured in elapsed
TIME for that reason.

No network. Both stores are files the collector already wrote, read
through `kis_bar_features.load_store` -- the same pure read the live
cycle does. NEW_KIS_CALLS = 0, NEW_PROVIDER_CALLS = 0.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

#: Which session immediately precedes which, in the order a trading day
#: actually runs: PREMARKET -> REGULAR -> AFTER_HOURS -> OVERNIGHT_DAYTIME
#: -> the next PREMARKET.
PRECEDING_SESSION = {
    "PREMARKET": "OVERNIGHT_DAYTIME",
    "REGULAR": "PREMARKET",
    "AFTER_HOURS": "REGULAR",
    "OVERNIGHT_DAYTIME": "AFTER_HOURS",
}

#: How long a new session must have been open before its momentum may
#: confirm an ENTRY. Elapsed time, never a bar count -- see module
#: docstring and §6.
ENTRY_STABILIZATION_SECONDS = 300.0


@dataclass
class BarContext:
    """Current-session bars, the inherited ones, and the boundary."""

    symbol: str
    session: Optional[str]
    current: List[Any] = field(default_factory=list)
    inherited: List[Any] = field(default_factory=list)
    session_started_at: Optional[datetime] = None

    @property
    def combined(self) -> List[Any]:
        """Inherited first, then current -- one ordered price history."""
        return list(self.inherited) + list(self.current)

    @property
    def inherited_context(self) -> bool:
        return bool(self.inherited)

    def elapsed_seconds(self, now) -> Optional[float]:
        if self.session_started_at is None or now is None:
            return None
        return (now - self.session_started_at).total_seconds()

    def stabilized(self, now, *, seconds=ENTRY_STABILIZATION_SECONDS) -> bool:
        elapsed = self.elapsed_seconds(now)
        return elapsed is not None and elapsed >= float(seconds)

    def boundary_gap(self):
        """`(last_before, first_after, gap_pct)` across the boundary.

        Diagnostic only -- §9 is explicit that no gate is built on it
        here, and no threshold is invented.
        """
        if not self.inherited or not self.current:
            return (None, None, None)
        try:
            last = float(getattr(self.inherited[-1], "close", None))
            first = float(getattr(self.current[0], "close", None))
        except (TypeError, ValueError):
            return (None, None, None)
        if not last:
            return (last, first, None)
        return (last, first, (first / last - 1.0) * 100.0)


def session_started_at(session, *, now=None):
    """The official origin of the session in progress, or None."""
    try:
        from scanners.base import session_range as srange

        moment = now or datetime.now(timezone.utc)
        session_date = srange.current_session_date(session, moment)
        origin = srange.official_origin(session, session_date)
        return origin.astimezone(timezone.utc) if origin is not None else None
    except Exception:  # noqa: BLE001 - no origin is "cannot stabilize yet"
        logger.debug("no session origin for %s", session, exc_info=True)
        return None


def gather(symbol, session, *, now=None, store=None, previous_store=None,
           trading_day=None, env=None) -> BarContext:
    """Bars for `symbol` either side of this session's boundary.

    `store` / `previous_store` may be injected (tests, and a caller that
    already loaded one). Otherwise they are loaded from the collector's
    snapshots, which is a file read.

    A previous session that cannot be loaded is simply no inherited
    history -- the result is a colder start, never an error.
    """
    moment = now or datetime.now(timezone.utc)
    name = str(session or "").upper() or None

    current = _bars_from(store, symbol, name) if store is not None else []
    if store is None and trading_day is not None:
        current = _bars_from(_load(name, trading_day, moment, env), symbol, name)

    previous_session = PRECEDING_SESSION.get(name or "")
    inherited = []
    if previous_store is not None:
        inherited = _bars_from(previous_store, symbol, previous_session)
    elif previous_session and trading_day is not None:
        inherited = _bars_from(_load(previous_session, trading_day, moment, env),
                               symbol, previous_session)

    return BarContext(symbol=symbol, session=name, current=list(current),
                      inherited=list(inherited),
                      session_started_at=session_started_at(name, now=moment))


def _load(session, trading_day, now, env):
    if not session:
        return None
    try:
        from s6_live import kis_bar_features
        from scanners.base import session_range as srange

        return kis_bar_features.load_store(
            session, trading_day, env=env,
            session_date=srange.current_session_date(session, now))
    except Exception:  # noqa: BLE001 - a missing snapshot is no history
        logger.debug("no bar store for %s", session, exc_info=True)
        return None


def _bars_from(store, symbol, session):
    if store is None or not session:
        return []
    try:
        return list(store.bars(symbol, session) or [])
    except Exception:  # noqa: BLE001
        return []
