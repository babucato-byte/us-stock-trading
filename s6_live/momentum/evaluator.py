"""The two questions S6 asks. Everything indicator-specific is below this.

    evaluate_entry_momentum(...)     -> may momentum confirm a NEW entry
    evaluate_position_momentum(...)  -> is an OPEN position weakening

They differ in exactly one rule, and it is the point of the module:

  * an ENTRY must wait `ENTRY_STABILIZATION_SECONDS` into the new session
    before momentum may confirm anything, so a breakout is judged against
    this session's own price discovery rather than the previous one's
    trend;
  * a POSITION is not gated at all. It was opened under rules that
    already passed, it is exposed right now, and making its weakness
    invisible for the first five minutes of a new session would be a gap
    in monitoring, not a safety margin.

Neither function decides anything. Both return a `MomentumResult`; what a
direction MEANS to S6 stays in S6.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from s6_live.momentum import indicators, state
from s6_live.momentum.model import MomentumResult, NOT_STABILIZED

logger = logging.getLogger(__name__)


def evaluate_entry_momentum(symbol, *, session, now=None, context=None,
                            store=None, previous_store=None, trading_day=None,
                            env=None) -> MomentumResult:
    """Momentum for a NEW entry. Not eligible until the session settles."""
    return _evaluate(symbol, session=session, now=now, context=context,
                     store=store, previous_store=previous_store,
                     trading_day=trading_day, env=env, for_entry=True)


def evaluate_position_momentum(symbol, *, session, now=None, context=None,
                               store=None, previous_store=None,
                               trading_day=None, env=None) -> MomentumResult:
    """Momentum for an OPEN position. No stabilization hold, ever."""
    return _evaluate(symbol, session=session, now=now, context=context,
                     store=store, previous_store=previous_store,
                     trading_day=trading_day, env=env, for_entry=False)


def _evaluate(symbol, *, session, now, context, store, previous_store,
              trading_day, env, for_entry) -> MomentumResult:
    started = time.perf_counter()
    moment = now or datetime.now(timezone.utc)
    try:
        bars = context if context is not None else state.gather(
            symbol, session, now=moment, store=store,
            previous_store=previous_store, trading_day=trading_day, env=env)
        frame = indicators.frame_from(bars.combined)
        fields, reason, timings = indicators.measure(frame)

        stabilized = bars.stabilized(moment)
        last, first, gap = bars.boundary_gap()
        available = bool(fields["hma_available"] and fields["macd_available"])

        # The one rule that differs. An ENTRY that is measured but not yet
        # settled is reported UNAVAILABLE with that reason, so it can
        # never be mistaken for "measured and flat".
        if for_entry and available and not stabilized:
            available = False
            reason = NOT_STABILIZED

        timings["MOMENTUM_EVALUATION_MS"] = round(
            (time.perf_counter() - started) * 1000.0, 3)
        return MomentumResult(
            symbol=symbol, session=bars.session, observed_at=moment.isoformat(),
            available=available, reason=reason,
            session_started_at=(bars.session_started_at.isoformat()
                                if bars.session_started_at else None),
            current_session_bar_count=len(bars.current),
            inherited_bar_count=len(bars.inherited),
            inherited_context=bars.inherited_context,
            entry_stabilized=stabilized,
            session_elapsed_seconds=bars.elapsed_seconds(moment),
            previous_session_last_price=last, new_session_first_price=first,
            boundary_gap_pct=gap, timings_ms=timings, **fields)
    except Exception:  # noqa: BLE001 - an observation, never a failure
        logger.warning("momentum evaluation failed for %s", symbol,
                       exc_info=True)
        return MomentumResult(symbol=symbol, session=session,
                              observed_at=moment.isoformat(),
                              reason="COMPUTE_FAILED")
