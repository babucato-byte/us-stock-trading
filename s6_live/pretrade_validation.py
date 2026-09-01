"""Current evidence for one candidate, fetched when it is needed.

What this replaces
------------------
Live entry used to read its features from the realtime stream alone in
PREMARKET, AFTER_HOURS and OVERNIGHT_DAYTIME -- `realtime_features.build`
takes the stream-only branch when no provider is supplied. The stream
carries at most `MAX_SUBSCRIPTIONS` symbols, chosen before the session
opened from the PRIOR session's candidates, so a name discovered this
morning had no feed and every data gate closed against it.

Measured on 2026-09-01 in PREMARKET: of 32 candidates, 2 were subscribed
and evaluated normally; the other 30 sat at zero open gates. Not one of
them had been judged on its merits. The same universe in REGULAR -- where
the provider fallback already applied -- put 31 of 51 unsubscribed
candidates at every gate open.

So subscription membership was deciding tradeability. It is a data
DELIVERY mechanism and it must not select stocks.

What this does
--------------
Hands the entry path the session's data adapter, so
`realtime_features.build` takes the provider branch and asks KIS for the
one symbol in front of it. No websocket, no watchlist, no universe fetch.

The cost, and why there is a budget
-----------------------------------
`kis_minute_chart` is a measured 2.44s per symbol, rate-limiter bound and
uncached. Fifty-five candidates is ~134s against a 60-second tick, so a
tick validates what it can afford in the strategy's own rank order and
leaves the rest for the next one.

A candidate the budget did not reach is `WAITING_FOR_DATA`. It is NOT a
rejection and must never be reported as one: the strategy was not asked.
That distinction is the whole reason this is a named state rather than an
absence -- an infrastructure limit reading as "no signal" is the failure
this layer exists to make impossible.
"""

import logging
import os
from typing import Any, Callable, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: Asked, and the strategy said no.
STRATEGY_REJECTED = "STRATEGY_REJECTED"
#: Not asked yet -- the tick ran out of budget before reaching it.
WAITING_FOR_DATA = "WAITING_FOR_DATA"
#: Asked, and the data source had nothing to say.
DATA_UNAVAILABLE = "DATA_UNAVAILABLE"

#: How long one tick may spend fetching candidate data. Below the entry
#: cadence on purpose: the tick has sizing, verification and submission
#: to do after this, and a validation pass that consumed the whole minute
#: would starve them.
DEFAULT_BUDGET_SECONDS = 30.0
BUDGET_ENV = "S6_PRETRADE_BUDGET_SECONDS"


def budget_seconds(env=None) -> float:
    env = env if env is not None else os.environ
    raw = env.get(BUDGET_ENV)
    try:
        value = float(raw)
        return value if value > 0 else DEFAULT_BUDGET_SECONDS
    except (TypeError, ValueError):
        return DEFAULT_BUDGET_SECONDS


def provider_for(session, *, broker=None, fallback=None, trading_day=None):
    """The session's data adapter -- the ONLY session-specific thing here.

    Sessions differ in where the bars come from, never in what the
    strategy does with them.
    """
    from market_data.kis_bar_provider import provider_for_session

    return provider_for_session(session, broker=broker, fallback=fallback,
                                trading_day=trading_day)


def ordered(symbols: Sequence[str], *, rank_of: Optional[Callable] = None
            ) -> List[str]:
    """Validation order, taken from the STRATEGY's ranking.

    Execution does not get to choose which candidate is looked at first;
    that is a strategy judgement and it already made it. Symbols with no
    rank sort last rather than first, so a missing rank cannot jump the
    queue ahead of a name the strategy actually favoured.
    """
    names = [str(s).upper() for s in (symbols or []) if s]
    if rank_of is None:
        return names

    def key(symbol):
        try:
            rank = rank_of(symbol)
        except Exception:  # noqa: BLE001 - an unreadable rank is not a
            # reason to drop a candidate, only to stop favouring it.
            return (1, 0, symbol)
        if rank is None:
            return (1, 0, symbol)
        try:
            return (0, int(rank), symbol)
        except (TypeError, ValueError):
            return (1, 0, symbol)

    return sorted(names, key=key)


class Budget:
    """A wall-clock allowance for one tick's validation pass."""

    def __init__(self, seconds=None, *, clock=None):
        import time

        self._clock = clock or time.monotonic
        self._limit = float(seconds if seconds is not None
                            else DEFAULT_BUDGET_SECONDS)
        self._started = self._clock()
        self.reached = 0
        self.skipped: List[str] = []

    @property
    def elapsed(self) -> float:
        return self._clock() - self._started

    def allows(self) -> bool:
        return self.elapsed < self._limit

    def spent_on(self, symbol) -> None:
        self.reached += 1

    def defer(self, symbol) -> None:
        self.skipped.append(str(symbol).upper())

    def report(self) -> dict:
        return {"validated": self.reached,
                "waiting_for_data": len(self.skipped),
                "deferred": list(self.skipped),
                "elapsed_seconds": round(self.elapsed, 2),
                "budget_seconds": self._limit}
