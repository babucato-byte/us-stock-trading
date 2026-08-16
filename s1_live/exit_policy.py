"""S1_EXIT_V0 as one pure decision. Places nothing.

`decide()` takes a position's state and today's observation and returns
exactly ONE action. Purity is what guarantees the spec's "one SELL per
position": there is no path that can emit two exits, because there is one
return value.

Priority, and why the protective floor is not its own exit
-----------------------------------------------------------
    1. emergency        the caller's kill-switch / risk halt
    2. stop             price <= the EFFECTIVE stop
    3. trend breakdown  the structural thesis is gone
    4. time exit        capital released from a trade that never worked
    5. protection       ratchet the floor UP (a state change, not an exit)

The effective stop is `max(hard stop, protective floor)`. Folding
protection into the stop rather than checking it separately is what makes
"several conditions true at once" impossible to turn into two orders --
a raised floor IS a stop, so it is evaluated where stops are evaluated,
and step 5 only ever moves a level.

Exits are never gated by entry risk
-----------------------------------
This module does not import the daily-loss guard, the drawdown guard, the
allocator or the kill switch, and a test asserts that. A drawdown limit
that also blocked liquidation would trap the account in the position the
limit exists to escape. The caller may refuse to ENTER; it may not use
this module's answer to refuse to LEAVE.
"""

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from config import s1_exit_v0 as policy

logger = logging.getLogger(__name__)

HOLD = "HOLD"
SELL = "SELL"
RATCHET = "RATCHET"

REASON_EMERGENCY = "EMERGENCY_LIQUIDATION"
REASON_HARD_STOP = "S1_HARD_STOP"
REASON_PROTECTIVE_STOP = "S1_PROTECTIVE_STOP"
REASON_TREND_BREAKDOWN = "S1_TREND_BREAKDOWN"
REASON_TIME_EXIT = "S1_TIME_EXIT"
REASON_INSUFFICIENT_DATA = "S1_EXIT_DATA_UNAVAILABLE"


@dataclass
class S1PositionState:
    """What must survive a restart for the policy to be stable.

    `protective_floor_r` is persisted rather than recomputed because it
    RATCHETS: a position that touched +2R and fell back keeps the floor
    that move earned it. Recomputing from the current price would hand
    the floor back on every restart, which is precisely the loss this
    axis exists to prevent.
    """

    symbol: str
    entry_price: float
    sessions_held: int = 0
    protective_floor_r: Optional[float] = None
    peak_r: float = 0.0
    exit_submitted: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return dict(vars(self))


@dataclass
class ExitDecision:
    action: str
    reason: Optional[str] = None
    detail: str = ""
    new_protective_floor_r: Optional[float] = None
    effective_stop_price: Optional[float] = None
    unrealised_r: Optional[float] = None

    @property
    def sells(self) -> bool:
        return self.action == SELL

    def as_dict(self) -> Dict[str, Any]:
        return dict(vars(self))


def _finite(value) -> Optional[float]:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def unrealised_r(entry_price, current_price) -> Optional[float]:
    """Move from entry expressed in R (the stop distance)."""
    entry, current = _finite(entry_price), _finite(current_price)
    if entry is None or current is None or entry <= 0:
        return None
    return ((current - entry) / entry) / policy.R_PCT


def protective_floor_for(peak_r: Optional[float]) -> Optional[float]:
    """The highest floor the position's best move has earned. Never falls."""
    best = _finite(peak_r)
    if best is None:
        return None
    floor = None
    for trigger_r, floor_r in policy.PROFIT_PROTECTION_STEPS:
        if best >= trigger_r:
            floor = floor_r if floor is None else max(floor, floor_r)
    return floor


def effective_stop_price(entry_price, protective_floor_r) -> Optional[float]:
    """max(hard stop, protective floor), as a price."""
    entry = _finite(entry_price)
    if entry is None or entry <= 0:
        return None
    hard = entry * (1 + policy.HARD_STOP_PCT)
    floor_r = _finite(protective_floor_r)
    if floor_r is None:
        return hard
    return max(hard, entry * (1 + floor_r * policy.R_PCT))


def trend_broken(features) -> Optional[Dict[str, Any]]:
    """Has S1's structural thesis failed? None when it cannot be judged.

    Reads only what the scanner already computes. ADX is deliberately not
    consulted -- see `config/s1_exit_v0.py`.
    """
    price = _finite(getattr(features, "price", None))
    hma200 = _finite(getattr(features, "hma200", None))
    hma89 = _finite(getattr(features, "hma89", None))
    slope = _finite(getattr(features, "hma200_slope", None))
    if price is None or hma200 is None:
        return None

    failures = []
    if policy.TREND_REQUIRE_PRICE_ABOVE_HMA200 and price <= hma200:
        failures.append(f"price {price:.4f} at/below HMA200 {hma200:.4f}")
    if policy.TREND_REQUIRE_HMA89_ABOVE_HMA200:
        if hma89 is None:
            return None
        if hma89 <= hma200:
            failures.append(f"HMA89 {hma89:.4f} at/below HMA200 {hma200:.4f}")
    if policy.TREND_REQUIRE_HMA200_RISING:
        if slope is None:
            return None
        if slope < policy.TREND_SLOPE_EXIT_BELOW_PCT:
            failures.append(f"HMA200 slope {slope:.3f}% turned negative")
    return {"broken": bool(failures), "failures": failures}


def decide(state: S1PositionState, *, current_price, features=None,
           emergency: bool = False) -> ExitDecision:
    """The single exit decision for one position, this tick."""
    if state is None:
        return ExitDecision(HOLD, REASON_INSUFFICIENT_DATA, "no position state")
    if state.exit_submitted:
        # Idempotency at the policy layer as well as the ledger's: a
        # position whose exit is already in flight is never re-sold.
        return ExitDecision(HOLD, detail="an exit is already in flight")

    # 1. emergency -- the caller's own halt. Always liquidates.
    if emergency:
        return ExitDecision(SELL, REASON_EMERGENCY, "emergency liquidation requested")

    price = _finite(current_price)
    entry = _finite(state.entry_price)
    # `price <= 0` is a DATA failure, not a crash to zero. Treating a
    # zero tick as a price would fire the stop on a bad feed and sell the
    # position at whatever the market actually was.
    if price is None or price <= 0 or entry is None or entry <= 0:
        return ExitDecision(HOLD, REASON_INSUFFICIENT_DATA,
                            "no usable current or entry price")

    move_r = unrealised_r(entry, price)
    peak_r = max(_finite(state.peak_r) or 0.0, move_r or 0.0)
    floor_r = protective_floor_for(peak_r)
    stored_floor = _finite(state.protective_floor_r)
    if stored_floor is not None:
        floor_r = stored_floor if floor_r is None else max(floor_r, stored_floor)
    stop_price = effective_stop_price(entry, floor_r)

    # 2. stop -- hard stop, or the raised protective floor. One check.
    if stop_price is not None and price <= stop_price:
        protective = floor_r is not None and stop_price > entry * (1 + policy.HARD_STOP_PCT)
        return ExitDecision(
            SELL, REASON_PROTECTIVE_STOP if protective else REASON_HARD_STOP,
            f"price {price:.4f} at/below stop {stop_price:.4f}",
            effective_stop_price=round(stop_price, 6),
            unrealised_r=round(move_r, 4) if move_r is not None else None)

    # 3. trend breakdown -- the thesis is gone.
    verdict = trend_broken(features) if features is not None else None
    if verdict and verdict["broken"]:
        return ExitDecision(SELL, REASON_TREND_BREAKDOWN,
                            "; ".join(verdict["failures"]),
                            effective_stop_price=round(stop_price, 6),
                            unrealised_r=round(move_r, 4) if move_r is not None else None)

    # 4. time exit -- capital released only from a trade that never worked.
    if state.sessions_held >= policy.TIME_EXIT_SESSIONS \
            and peak_r < policy.TIME_EXIT_EXEMPT_ABOVE_R:
        return ExitDecision(
            SELL, REASON_TIME_EXIT,
            f"held {state.sessions_held} sessions and never reached "
            f"+{policy.TIME_EXIT_EXEMPT_ABOVE_R:g}R (peak {peak_r:.2f}R)",
            effective_stop_price=round(stop_price, 6),
            unrealised_r=round(move_r, 4) if move_r is not None else None)

    # 5. protection -- move the floor UP. Not an exit.
    if floor_r is not None and (stored_floor is None or floor_r > stored_floor):
        return ExitDecision(
            RATCHET, None,
            f"peak {peak_r:.2f}R earns a protective floor at +{floor_r:g}R",
            new_protective_floor_r=floor_r,
            effective_stop_price=round(stop_price, 6),
            unrealised_r=round(move_r, 4) if move_r is not None else None)

    return ExitDecision(HOLD, effective_stop_price=round(stop_price, 6),
                        unrealised_r=round(move_r, 4) if move_r is not None else None)
