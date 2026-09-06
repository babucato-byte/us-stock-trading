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

Nor does one tick below the peak make a position weak. `price_weak`
once said GAVE_BACK_PEAK whenever `price < peak_price`, and the peak
ratchets up on every tick, so the compound exit fired on the first
ordinary pullback of any breakout whose opening volume burst had faded.
Weakness now means a meaningful fraction of the gain above the range
given back, from a peak that is no longer fresh -- see
`peak_given_back`. The order of the rules above is unchanged.

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
    #: When `peak_price` was last raised, ISO-8601. None on rows opened
    #: before the peak was dated; the give-back test then judges on the
    #: fraction alone rather than going silent.
    peak_price_at: Optional[str] = None


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


def _minutes_since(stamp, now=None) -> Optional[float]:
    """Minutes from an ISO stamp (or datetime) to `now`; None if unknown.

    `now=None` reads the wall clock: the peak's age is a fact about the
    position, not about the caller having a moment to hand over. A naive
    `now` is Eastern, as `session_ending` treats it; a naive stamp is
    UTC, as the position store writes them.
    """
    if stamp is None:
        return None
    from datetime import datetime, timezone

    try:
        if isinstance(stamp, datetime):
            when = stamp
        else:
            when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        try:
            from market_hours import EASTERN

            moment = moment.replace(tzinfo=EASTERN)
        except Exception:  # noqa: BLE001 - an unreadable zone is unknown age
            return None
    return (moment - when).total_seconds() / 60.0


WEAKNESS_VWAP_BELOW = "VWAP_BELOW"
WEAKNESS_GAVE_BACK_PEAK = "GAVE_BACK_PEAK"


def peak_observation(state, features, now=None, price=None) -> Dict[str, Any]:
    """Where price stands against the position's own peak, every tick.

    Raw figures, recorded whether or not any threshold is met: the
    give-back fraction and the peak's age are the two distributions the
    compound exit's provisional numbers have to be measured against, and
    every value a different threshold would need is kept as a number so
    an alternative can be replayed from the record rather than
    reconstructed from a boolean.
    """
    price = _price_of(features, price)
    peak = _finite(state.peak_price)
    high = _finite(state.range_high)
    drawdown_pct = None
    giveback_amount = None
    if price is not None and peak is not None:
        giveback_amount = peak - price
        if peak > 0:
            drawdown_pct = giveback_amount / peak * 100.0
    gain = None
    giveback = None
    if peak is not None and high is not None and peak > high:
        gain = peak - high
        if giveback_amount is not None:
            giveback = giveback_amount / gain
    peak_expansion = _finite(state.peak_volume_expansion) or _finite(
        state.entry_volume_expansion)
    return {
        "current_price": price,
        "peak_price": peak,
        "peak_price_at": state.peak_price_at,
        "peak_age_minutes": _minutes_since(state.peak_price_at, now),
        "range_high": high,
        "breakout_gain_at_peak": gain,
        "giveback_amount": giveback_amount,
        "giveback_fraction": giveback,
        "peak_drawdown_pct": drawdown_pct,
        "peak_volume_expansion": peak_expansion,
        "current_volume_expansion": (
            _finite(getattr(features, "volume_expansion", None))
            if features is not None else None),
        "giveback_fraction_threshold": policy.PEAK_GIVEBACK_FRACTION,
        "stale_minutes_threshold": policy.PEAK_STALE_MINUTES,
    }


def peak_given_back(state, features, now=None) -> Optional[Dict[str, Any]]:
    """Has the position surrendered a meaningful share of its breakout gain?

    Two conditions, both from the position's own geometry:

      give-back   (peak - price) / (peak - range_high) at or past
                  PEAK_GIVEBACK_FRACTION. A peak at or below the range
                  high has no gain to give back, and price below the
                  range high is the range re-entry rule's finding, not
                  this one's.
      staleness   the peak is at least PEAK_STALE_MINUTES old. A dip
                  minutes after a fresh high is a shake, not a stall.
                  An undated peak (a row from before the date existed)
                  is judged on the give-back alone.

    Answers the rule as DESIGNED. Whether the answer may sell is
    `ENFORCE_PEAK_GIVEBACK_EXIT`'s question, asked by `compound_decay_exit`.
    """
    observed = peak_observation(state, features, now)
    price, peak = observed["current_price"], observed["peak_price"]
    if price is None or peak is None or price >= peak:
        return None
    fraction = observed["giveback_fraction"]
    if fraction is None or fraction < policy.PEAK_GIVEBACK_FRACTION:
        return None
    age = observed["peak_age_minutes"]
    if age is not None and age < policy.PEAK_STALE_MINUTES:
        return None
    return {"weakness": WEAKNESS_GAVE_BACK_PEAK, **observed}


def price_weak(state, features, now=None) -> Optional[Dict[str, Any]]:
    """The second half of the compound exit. Never an exit alone."""
    lost = vwap_failed(features)
    if lost:
        return {"weakness": WEAKNESS_VWAP_BELOW, **lost}
    return peak_given_back(state, features, now)


def compound_decay_exit(state, features, now=None) -> Dict[str, Any]:
    """Rule 6 in full: decay AND weakness, and whether it may sell.

    Returns {"sell": detail-or-None, "shadow": detail-or-None}. `sell`
    is set when the rule fires AND is enforced; `shadow` when the rule
    fired as designed but the give-back half is not yet enforced, so
    the tick is recorded as one the rule WOULD have sold on. The two are
    never both set. Volume decay with no weakness sets neither.
    """
    decayed = volume_decayed(state, features)
    if not decayed:
        return {"sell": None, "shadow": None}
    weak = price_weak(state, features, now)
    if not weak:
        return {"sell": None, "shadow": None}
    detail = {**decayed, **weak}
    if (weak.get("weakness") == WEAKNESS_GAVE_BACK_PEAK
            and not policy.ENFORCE_PEAK_GIVEBACK_EXIT):
        return {"sell": None,
                "shadow": {**detail, "peak_exit_candidate": True,
                           "peak_exit_enforced": False,
                           "would_sell_reason":
                               REASON_VOLUME_DECAY_PRICE_WEAKNESS}}
    return {"sell": {**detail, "peak_exit_enforced": True}, "shadow": None}


def peak_exit_assessment(state, features, now=None, price=None
                         ) -> Dict[str, Any]:
    """The full compound-rule picture for one tick, for the diagnostics.

    The raw observation plus what each half answered and what the rule
    would do about it. Recorded on HOLD ticks as much as on SELL ticks:
    a threshold can only be re-chosen from ticks it did NOT fire on.
    """
    observed = peak_observation(state, features, now, price=price)
    decayed = volume_decayed(state, features)
    weak = peak_given_back(state, features, now)
    candidate = bool(decayed and weak)
    return {
        **observed,
        "volume_decay_triggered": bool(decayed),
        "volume_decay_target": decayed.get("decay_target") if decayed else None,
        "peak_weakness_triggered": bool(weak),
        "peak_exit_candidate": candidate,
        "peak_exit_enforced": bool(policy.ENFORCE_PEAK_GIVEBACK_EXIT),
        "would_sell_reason": (REASON_VOLUME_DECAY_PRICE_WEAKNESS
                              if candidate else None),
    }


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

    shadow: Dict[str, Any] = {}
    if policy.EXIT_ON_VOLUME_DECAY_WITH_WEAKNESS:
        compound = compound_decay_exit(state, features, now)
        if compound["sell"]:
            return ExitDecision(
                SELL, reason=REASON_VOLUME_DECAY_PRICE_WEAKNESS,
                detail={**context, **compound["sell"]})
        # The rule fired as designed but is not yet enforced. Nothing
        # sells on it; the tick is recorded as one it WOULD have sold
        # on, and the later rules are still asked.
        shadow = compound["shadow"] or {}

    ending = session_ending(session, now)
    if ending:
        return ExitDecision(SELL, reason=REASON_SESSION_EXIT,
                            detail={**context, **ending})

    return ExitDecision(HOLD, detail={**context, **shadow})


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
