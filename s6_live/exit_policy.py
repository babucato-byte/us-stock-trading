"""S6_EXIT_V0 as one pure decision. Places nothing.

One return value, for the reason S1's and S2's have one: it is what
makes "two SELLs for one position" unreachable rather than merely
unlikely.

Priority
--------
    1. emergency                the caller's kill-switch / risk halt
    2. hard risk                price at or below the range LOW
    3. range re-entry           back inside the range it broke out of
    4. VWAP failure             the session's volume traded above price
    5. EMA structure failure    EMA9 <= EMA21
    6. decay + price weakness   the volume case is dead
    7. session exit             no overnight carry during validation

Two and three are both "back into the structure", separated because they
are different amounts of wrong: re-entry says the breakout is undone,
below the range low says the entire setup is. The stop sits above
re-entry so a position that gaps straight through the range is not
reported as an ordinary re-entry.

Volume decay never exits alone. A breakout continuing on lighter
participation is a normal winning shape, and cutting it would remove
precisely the trades that worked.

Nothing here reads PnL. Whether an exit books a gain is an outcome, not
an input -- a rule that behaved differently above and below water would
be two strategies sharing a name.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from config import s6_exit_v0 as policy

logger = logging.getLogger(__name__)

HOLD = "HOLD"
SELL = "SELL"

REASON_EMERGENCY = "EMERGENCY"
REASON_HARD_RISK_CAP = "HARD_RISK_CAP"
REASON_RANGE_REENTRY = "RANGE_REENTRY"
REASON_VWAP_FAILURE = "VWAP_FAILURE"
REASON_EMA_STRUCTURE_FAILURE = "EMA_STRUCTURE_FAILURE"
REASON_VOLUME_DECAY_PRICE_WEAKNESS = "VOLUME_DECAY_PRICE_WEAKNESS"
REASON_SESSION_EXIT = "SESSION_EXIT"
REASON_NO_STRUCTURE = "NO_STRUCTURE_TO_PROTECT"

EXIT_REASONS = (REASON_EMERGENCY, REASON_HARD_RISK_CAP, REASON_RANGE_REENTRY,
                REASON_VWAP_FAILURE, REASON_EMA_STRUCTURE_FAILURE,
                REASON_VOLUME_DECAY_PRICE_WEAKNESS, REASON_SESSION_EXIT,
                REASON_NO_STRUCTURE)

REASON_INSUFFICIENT_DATA = "S6_EXIT_DATA_UNAVAILABLE"
REASON_ALREADY_SUBMITTED = "S6_EXIT_ALREADY_SUBMITTED"


@dataclass(frozen=True)
class S6PositionState:
    symbol: str
    entry_price: float
    variant: Optional[str] = None
    range_high: Optional[float] = None
    range_low: Optional[float] = None
    entry_volume_expansion: Optional[float] = None
    peak_volume_expansion: Optional[float] = None
    peak_price: Optional[float] = None
    exit_submitted: bool = False


@dataclass
class ExitDecision:
    action: str
    reason: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def sells(self) -> bool:
        return self.action == SELL

    def as_dict(self) -> Dict[str, Any]:
        return {"action": self.action, "reason": self.reason,
                "detail": dict(self.detail)}


def _finite(value) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number or number in (
        float("inf"), float("-inf")) else number


def _price_of(features, current_price=None) -> Optional[float]:
    price = _finite(current_price)
    if price is None and features is not None:
        price = _finite(getattr(features, "price", None))
    return price


def hard_risk_breached(state, price) -> Optional[Dict[str, Any]]:
    """Price at or below the range LOW -- the setup comprehensively undone."""
    stop = policy.structural_stop(state.range_low)
    price = _finite(price)
    if stop is None or price is None or price > stop:
        return None
    return {"price": price, "structural_stop": stop,
            "hard_risk_level": policy.HARD_RISK_LEVEL,
            "range_high": _finite(state.range_high)}


def range_reentered(state, price) -> Optional[Dict[str, Any]]:
    """Back inside the range: the breakout undone, but not the setup."""
    high = _finite(state.range_high)
    price = _finite(price)
    if high is None or price is None or price > high:
        return None
    return {"price": price, "range_high": high,
            "range_low": _finite(state.range_low)}


def vwap_failed(features) -> Optional[Dict[str, Any]]:
    if features is None:
        return None
    price = _finite(getattr(features, "price", None))
    vwap = _finite(getattr(features, "vwap", None))
    if price is None or vwap is None or price >= vwap:
        return None
    return {"price": price, "vwap": vwap}


def ema_structure_failed(features) -> Optional[Dict[str, Any]]:
    if features is None:
        return None
    fast = _finite(getattr(features, "ema9", None))
    slow = _finite(getattr(features, "ema21", None))
    if fast is None or slow is None or fast > slow:
        return None
    return {"ema9": fast, "ema21": slow}


def volume_decayed(state, features) -> Optional[Dict[str, Any]]:
    """Expansion drained past the fraction, measured from the PEAK."""
    peak = _finite(state.peak_volume_expansion) or _finite(
        state.entry_volume_expansion)
    now = _finite(getattr(features, "volume_expansion", None)) if features else None
    if peak is None or now is None:
        return None
    excess = peak - 1.0
    if excess <= 0:
        # Never elevated, so nothing can decay. Reporting it as decayed
        # would exit the quietest positions first.
        return None
    target = 1.0 + excess * (1.0 - policy.VOLUME_DECAY_FRACTION)
    if now > target:
        return None
    return {"peak_expansion": peak, "current_expansion": now,
            "decay_target": target,
            "decay_fraction": policy.VOLUME_DECAY_FRACTION}


def price_weak(state, features) -> Optional[Dict[str, Any]]:
    """The second half of the compound exit. Never an exit alone."""
    lost = vwap_failed(features)
    if lost:
        return {"weakness": "VWAP_BELOW", **lost}
    price = _finite(getattr(features, "price", None)) if features else None
    peak = _finite(state.peak_price)
    if price is not None and peak is not None and price < peak:
        return {"weakness": "GAVE_BACK_PEAK", "price": price,
                "peak_price": peak}
    return None


def session_ending(session=None, now=None) -> Optional[Dict[str, Any]]:
    if not policy.EXIT_ON_SESSION_END or policy.ALLOW_OVERNIGHT_CARRY:
        return None
    try:
        from datetime import timedelta

        from market_hours import EASTERN
        from scanners.base import scan_session

        current = scan_session.normalize(session)
        if current is None or now is None:
            return None
        moment = now if now.tzinfo else now.replace(tzinfo=EASTERN)
        upcoming = scan_session.session_at(
            moment + timedelta(minutes=policy.SESSION_EXIT_LEAD_MINUTES))
        if upcoming != current:
            return {"session": current, "next_session": upcoming,
                    "lead_minutes": policy.SESSION_EXIT_LEAD_MINUTES}
    except Exception:  # noqa: BLE001 - a clock problem must not liquidate
        logger.warning("could not evaluate the S6 session boundary",
                       exc_info=True)
    return None


def decide(state: S6PositionState, *, current_price=None, features=None,
           session=None, now=None, emergency: bool = False) -> ExitDecision:
    """One action for one position. Never places an order."""
    if state.exit_submitted:
        return ExitDecision(HOLD, reason=REASON_ALREADY_SUBMITTED)

    if emergency:
        return ExitDecision(SELL, reason=REASON_EMERGENCY)

    price = _price_of(features, current_price)
    context = _structure_detail(state)

    # A position whose range is unknown has no expressible stop. It
    # cannot normally exist -- qualification refuses a candidate without
    # a range -- so this is a corrupted or hand-inserted row, and it is
    # exited rather than held under protection that does not exist.
    if policy.structural_stop(state.range_low) is None:
        return ExitDecision(SELL, reason=REASON_NO_STRUCTURE, detail=context)

    breached = hard_risk_breached(state, price)
    if breached:
        return ExitDecision(SELL, reason=REASON_HARD_RISK_CAP,
                            detail={**context, **breached})

    if features is None and price is None:
        return ExitDecision(HOLD, reason=REASON_INSUFFICIENT_DATA,
                            detail=context)

    if policy.EXIT_ON_RANGE_REENTRY:
        reentered = range_reentered(state, price)
        if reentered:
            return ExitDecision(SELL, reason=REASON_RANGE_REENTRY,
                                detail={**context, **reentered})

    if policy.EXIT_ON_VWAP_FAILURE:
        lost = vwap_failed(features)
        if lost:
            return ExitDecision(SELL, reason=REASON_VWAP_FAILURE,
                                detail={**context, **lost})

    if policy.EXIT_ON_EMA_STRUCTURE_FAILURE:
        turned = ema_structure_failed(features)
        if turned:
            return ExitDecision(SELL, reason=REASON_EMA_STRUCTURE_FAILURE,
                                detail={**context, **turned})

    if policy.EXIT_ON_VOLUME_DECAY_WITH_WEAKNESS:
        decayed = volume_decayed(state, features)
        if decayed:
            weak = price_weak(state, features)
            if weak:
                return ExitDecision(
                    SELL, reason=REASON_VOLUME_DECAY_PRICE_WEAKNESS,
                    detail={**context, **decayed, **weak})

    ending = session_ending(session, now)
    if ending:
        return ExitDecision(SELL, reason=REASON_SESSION_EXIT,
                            detail={**context, **ending})

    return ExitDecision(HOLD, detail=context)


def _structure_detail(state) -> Dict[str, Any]:
    """The levels in force, on every decision including HOLD."""
    return {
        "variant": state.variant,
        "range_high": _finite(state.range_high),
        "range_low": _finite(state.range_low),
        "structural_stop": policy.structural_stop(state.range_low),
        "hard_risk_level": policy.HARD_RISK_LEVEL,
        "catastrophic_cap_pct": policy.CATASTROPHIC_CAP_PCT,
    }
