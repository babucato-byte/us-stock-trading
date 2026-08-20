"""S2_EXIT_V0 as one pure decision. Places nothing.

`decide()` returns exactly ONE action, for the same reason S1's does: one
return value is what makes "two SELLs for one position" unreachable
rather than merely unlikely. That structure is borrowed from S1. Nothing
else is -- S1 sits through noise on a trend thesis with an R-multiple
ladder measured against its own history, and S2's thesis is a volume
event that either carries the price or stops existing.

Priority
--------
    1. emergency                    the caller's kill-switch / risk halt
    2. hard stop                    catastrophic cap, not the strategy
    3. structure failure            below HMA200, or HMA200 flat/falling
    4. decay + price weakness       THE exit: the volume case is dead
    5. VWAP failure                 alone, reported as itself
    6. decay, confirmed             decay that persisted while price stalled
    7. session exit                 no overnight carry during validation

The cap sits second because a ceiling that yields to anything is not a
ceiling -- but it is expected to be a rare reason, and how often it fires
instead of 3-6 is a measurement about whether the structural exits are
fast enough.

Four before five, and six after both, is the whole design. Decay paired
with weakness is the real signal and gets the compound reason. Decay
alone waits: volume fading while price keeps working above VWAP is a
normal winning shape, and exiting it would systematically cut the trades
that worked. Only decay that PERSISTS while the price stops making
progress becomes an exit on its own.

Nothing here reads PnL
----------------------
No condition is expressed in profit, R, or percent-from-entry except the
catastrophic cap. The volume case dying is the exit; whether that books a
gain or a loss is an outcome. A rule that behaved differently above and
below water would be two strategies sharing a name, and the one that runs
in a drawdown would be the untested one.

Exits are never gated by entry risk
-----------------------------------
This module imports no allocator, no daily-loss guard, no kill switch,
and a test asserts it. A risk limit that also blocked liquidation would
trap the account in the position the limit exists to escape.
"""

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Optional

from config import s2_exit_v0 as policy

logger = logging.getLogger(__name__)

HOLD = "HOLD"
SELL = "SELL"

REASON_EMERGENCY = "EMERGENCY_LIQUIDATION"
REASON_HARD_STOP = "HARD_STOP"
REASON_STRUCTURE_FAILURE = "STRUCTURE_FAILURE"
REASON_VWAP_FAILURE = "VWAP_FAILURE"
REASON_VOLUME_DECAY = "VOLUME_DECAY"
REASON_VOLUME_DECAY_PRICE_WEAKNESS = "VOLUME_DECAY_PRICE_WEAKNESS"
REASON_SESSION_EXIT = "SESSION_EXIT"

#: The vocabulary a stored trade must use. Fixed here so the analysis
#: cannot be handed a reason it has no column for.
EXIT_REASONS = (REASON_VOLUME_DECAY, REASON_VOLUME_DECAY_PRICE_WEAKNESS,
                REASON_VWAP_FAILURE, REASON_STRUCTURE_FAILURE,
                REASON_HARD_STOP, REASON_SESSION_EXIT, REASON_EMERGENCY)

#: Sub-reasons for the compound exit: WHICH weakness confirmed the decay.
#: Carried in the detail rather than in the reason, so the analysis can
#: group every volume-death exit together and still tell them apart.
WEAKNESS_VWAP = "VWAP_BELOW"
WEAKNESS_MOMENTUM_REVERSAL = "MOMENTUM_REVERSAL"
WEAKNESS_STALLED = "NO_PROGRESS_SINCE_VOLUME_PEAK"

REASON_INSUFFICIENT_DATA = "S2_EXIT_DATA_UNAVAILABLE"
REASON_ALREADY_SUBMITTED = "S2_EXIT_ALREADY_SUBMITTED"


@dataclass(frozen=True)
class S2PositionState:
    """What must persist between ticks, and nothing that can be recomputed.

    The three tracked fields exist because they are history: the peak
    multiple and the price at that peak cannot be recovered from a later
    observation, and the moment decay began is what the confirmation
    window is measured from.
    """

    symbol: str
    entry_price: float
    entry_volume_multiple: Optional[float] = None
    #: The average volume the multiples are computed against. Without it
    #: there is nothing for a current volume to be a multiple OF.
    baseline_volume: Optional[float] = None
    #: Highest volume multiple seen since entry. Ratchets up only.
    peak_volume_multiple: Optional[float] = None
    #: Price at the moment that peak was set -- the level the volume
    #: event actually produced.
    price_at_volume_peak: Optional[float] = None
    #: When decay was first observed, for the confirmation window. Reset
    #: to None the moment volume recovers.
    decay_since: Optional[Any] = None
    #: Structural invalidation price when the caller can compute one.
    structural_stop_price: Optional[float] = None
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


def current_volume_multiple(state: S2PositionState, features
                            ) -> Optional[float]:
    """Current volume as a multiple of the position's baseline."""
    baseline = _finite(state.baseline_volume)
    volume = _finite(getattr(features, "volume", None)) if features else None
    if baseline is None or baseline <= 0 or volume is None:
        return None
    return volume / baseline


def volume_decay_ratio(state: S2PositionState, features) -> Optional[float]:
    """How much of the peak EXCESS over baseline remains, in [0, 1]-ish.

    1.0 means the volume is still at its peak; 0.0 means it is back to
    baseline. Measured from the peak rather than from entry because
    momentum that built to 8x and fell to 4x has decayed, even though 4x
    still dwarfs the 1.5x that triggered the scan.

    Returns None when unmeasurable, and None when the position was never
    elevated -- nothing that did not rise can fall, and reporting it as
    fully decayed would exit the quietest positions first.
    """
    peak = _finite(state.peak_volume_multiple)
    now = current_volume_multiple(state, features)
    if peak is None or now is None:
        return None
    peak_excess = peak - 1.0
    if peak_excess <= 0:
        return None
    return max(0.0, (now - 1.0) / peak_excess)


def volume_has_decayed(state: S2PositionState, features) -> Optional[bool]:
    """Has the peak excess drained past the configured fraction?"""
    ratio = volume_decay_ratio(state, features)
    if ratio is None:
        return None
    return ratio <= (1.0 - policy.VOLUME_DECAY_FRACTION)


def observe(state: S2PositionState, features, *, now=None) -> S2PositionState:
    """Update the tracked history from one observation. Decides nothing.

    Separated from `decide()` so the decision stays pure: this is the
    only function that changes what the position remembers, and it is
    called once per tick before the decision is asked for.

    The peak ratchets UP only. A lower reading is decay, not a new peak,
    and letting the peak follow the volume down would make decay
    impossible to detect -- the ratio would sit at 1.0 forever.
    """
    multiple = current_volume_multiple(state, features)
    price = _price_of(features)
    updated = state

    if multiple is not None:
        peak = _finite(state.peak_volume_multiple)
        if peak is None or multiple > peak:
            updated = replace(updated, peak_volume_multiple=multiple,
                              price_at_volume_peak=price
                              if price is not None else state.price_at_volume_peak)

    decayed = volume_has_decayed(updated, features)
    if decayed is True and updated.decay_since is None:
        updated = replace(updated, decay_since=now)
    elif decayed is False and updated.decay_since is not None:
        # Volume recovered. The window restarts rather than continuing,
        # because the condition it was timing stopped being true.
        updated = replace(updated, decay_since=None)
    return updated


def effective_stop_price(entry_price, structural_stop_price=None
                         ) -> Optional[float]:
    """The catastrophic stop in force: whichever is more conservative.

    More conservative means the HIGHER price -- the smaller loss. A
    structural level above the cap governs; one below it is overridden,
    because the cap is the agreed maximum and a structural level is not a
    licence to exceed it. With no structural stop the cap answers alone.
    """
    cap = policy.max_loss_stop_price(entry_price)
    if cap is None:
        return None
    structural = _finite(structural_stop_price)
    return cap if structural is None else max(structural, cap)


def stop_breached(state: S2PositionState, current_price
                  ) -> Optional[Dict[str, Any]]:
    price = _finite(current_price)
    stop = effective_stop_price(state.entry_price, state.structural_stop_price)
    if price is None or stop is None or price > stop:
        return None
    return {"price": price, "effective_stop": stop,
            "structural_stop": _finite(state.structural_stop_price),
            "hard_stop": policy.max_loss_stop_price(state.entry_price),
            "max_loss_pct": policy.S2_LIMITED_LIVE_MAX_LOSS_PCT}


def structure_failed(features) -> Optional[Dict[str, Any]]:
    """S2's own entry conditions, asked again.

    None -- not "failed" -- when the features cannot answer. An
    unmeasurable HMA is missing data, and selling on missing data turns a
    provider hiccup into a liquidation.
    """
    if features is None:
        return None
    price = _finite(getattr(features, "price", None))
    hma200 = _finite(getattr(features, "hma200", None))
    slope = _finite(getattr(features, "hma200_slope", None))
    if price is not None and hma200 is not None and price < hma200:
        return {"failure": "BELOW_HMA200", "price": price, "hma200": hma200}
    if slope is not None and slope <= 0:
        return {"failure": "HMA200_NOT_RISING", "hma200_slope": slope}
    return None


def vwap_failed(features) -> Optional[Dict[str, Any]]:
    """Price strictly below VWAP.

    Strictly: a price sitting exactly at VWAP has not lost it, and
    treating equality as a failure would exit on a rounding artefact.
    """
    if features is None:
        return None
    price = _finite(getattr(features, "price", None))
    vwap = _finite(getattr(features, "vwap", None))
    if price is None or vwap is None or price >= vwap:
        return None
    return {"price": price, "vwap": vwap}


def price_weakness(state: S2PositionState, features) -> Optional[Dict[str, Any]]:
    """Is the price failing to carry, in any of the ways that count?

    Checked only to CONFIRM decay -- this is the second half of the
    compound exit, never an exit by itself. Returns the specific weakness
    so the stored trade can distinguish a VWAP break from a move that was
    simply given back.
    """
    if features is None:
        return None
    price = _finite(getattr(features, "price", None))
    if price is None:
        return None

    lost = vwap_failed(features)
    if lost:
        return {"weakness": WEAKNESS_VWAP, **lost}

    if policy.EXIT_ON_MOMENTUM_REVERSAL:
        peak_price = _finite(state.price_at_volume_peak)
        if peak_price is not None and price < peak_price:
            # The move the volume produced has been handed back. A
            # comparison against the position's own history -- there is
            # no level here to choose.
            return {"weakness": WEAKNESS_MOMENTUM_REVERSAL, "price": price,
                    "price_at_volume_peak": peak_price}
    return None


def decay_confirmed(state: S2PositionState, now=None) -> Optional[Dict[str, Any]]:
    """Has decay persisted long enough to stand on its own?

    The window is a debounce: one quiet bar is a reading, a sustained
    drain while the price makes no progress is a condition. Returns None
    when the elapsed time cannot be computed, because a clock problem
    must not liquidate a healthy position.
    """
    started = state.decay_since
    if started is None or now is None:
        return None
    try:
        elapsed = (now - started).total_seconds() / 60.0
    except Exception:  # noqa: BLE001
        return None
    if elapsed < 0 or elapsed < policy.VOLUME_DECAY_CONFIRMATION_MINUTES:
        return None
    return {"weakness": WEAKNESS_STALLED, "decayed_for_minutes": elapsed,
            "confirmation_minutes": policy.VOLUME_DECAY_CONFIRMATION_MINUTES}


def session_ending(session=None, now=None) -> Optional[Dict[str, Any]]:
    """Is this position's session about to end?

    None when unknowable -- an unrecognised session or an unreadable
    clock. None means HOLD, the right direction for a validation
    constraint: the catastrophic cap protects the position, and closing
    on an unreadable clock would exit healthy trades on a timezone bug.
    """
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
        logger.warning("could not evaluate the S2 session boundary",
                       exc_info=True)
    return None


def decide(state: S2PositionState, *, current_price=None, features=None,
           session=None, now=None, emergency: bool = False) -> ExitDecision:
    """One action for one position. Never places an order.

    Missing observations produce HOLD with an explicit reason, never
    SELL. The one exception is `emergency`, where the caller is asserting
    a fact rather than reporting a measurement.
    """
    if state.exit_submitted:
        # An exit is already live at the broker. Deciding again could
        # only ever produce a second SELL for one position.
        return ExitDecision(HOLD, reason=REASON_ALREADY_SUBMITTED)

    if emergency:
        return ExitDecision(SELL, reason=REASON_EMERGENCY)

    price = _price_of(features, current_price)
    context = _volume_detail(state, features)

    breached = stop_breached(state, price)
    if breached:
        return ExitDecision(SELL, reason=REASON_HARD_STOP,
                            detail={**context, **breached})

    if features is None:
        return ExitDecision(HOLD, reason=REASON_INSUFFICIENT_DATA,
                            detail={**context, **_stop_detail(state)})

    if policy.EXIT_ON_STRUCTURE_FAILURE:
        failed = structure_failed(features)
        if failed:
            return ExitDecision(SELL, reason=REASON_STRUCTURE_FAILURE,
                                detail={**context, **failed})

    decayed = volume_has_decayed(state, features)

    # The exit this strategy is actually built on: the volume case is
    # dead AND the price has stopped being carried by it.
    if policy.EXIT_ON_VOLUME_DECAY and decayed is True:
        weak = price_weakness(state, features)
        if weak:
            return ExitDecision(SELL,
                                reason=REASON_VOLUME_DECAY_PRICE_WEAKNESS,
                                detail={**context, **weak})

    if policy.EXIT_ON_VWAP_FAILURE:
        lost = vwap_failed(features)
        if lost:
            return ExitDecision(SELL, reason=REASON_VWAP_FAILURE,
                                detail={**context, **lost})

    # Decay that persisted while the price made no progress. Reached only
    # when the price is NOT weak by the checks above -- so this is the
    # slow-fade case, not the give-back one.
    if policy.EXIT_ON_VOLUME_DECAY and decayed is True:
        confirmed = decay_confirmed(state, now)
        if confirmed:
            return ExitDecision(SELL, reason=REASON_VOLUME_DECAY,
                                detail={**context, **confirmed})

    ending = session_ending(session, now)
    if ending:
        return ExitDecision(SELL, reason=REASON_SESSION_EXIT,
                            detail={**context, **ending})

    return ExitDecision(HOLD, detail={**context, **_stop_detail(state)})


def _volume_detail(state: S2PositionState, features) -> Dict[str, Any]:
    """The volume picture, carried on every decision.

    On HOLDs as well as SELLs: §6 asks for the decay history of trades
    that were never exited on volume too, and a field only written at
    exit cannot answer what the position looked like on the way there.
    """
    return {
        "entry_volume_multiple": _finite(state.entry_volume_multiple),
        "peak_volume_multiple": _finite(state.peak_volume_multiple),
        "current_volume_multiple": current_volume_multiple(state, features),
        "volume_decay_ratio": volume_decay_ratio(state, features),
        "price_at_volume_peak": _finite(state.price_at_volume_peak),
        "current_price": _price_of(features),
        "vwap": _finite(getattr(features, "vwap", None)) if features else None,
        "decay_fraction": policy.VOLUME_DECAY_FRACTION,
    }


def _stop_detail(state: S2PositionState) -> Dict[str, Any]:
    """The stop levels in force, so the log does not need the config."""
    return {
        "effective_stop": effective_stop_price(state.entry_price,
                                               state.structural_stop_price),
        "structural_stop": _finite(state.structural_stop_price),
        "hard_stop": policy.max_loss_stop_price(state.entry_price),
        "max_loss_pct": policy.S2_LIMITED_LIVE_MAX_LOSS_PCT,
    }
