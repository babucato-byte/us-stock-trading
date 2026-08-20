"""S2_EXIT_V0 as one pure decision. Places nothing.

Shape borrowed from S1, content not
-----------------------------------
`decide()` returns exactly ONE action, for the same reason S1's does:
one return value is what makes "two SELLs for one position" unreachable
rather than merely unlikely. That structure is worth copying.

The conditions are not. S1 exits on an R-multiple ladder measured
against S1's history; S2 has about four trading days, so every condition
here is either S2's own entry condition re-checked or a level with no
free parameter. See `config/s2_exit_v0` for why, and for the price stop
that is deliberately absent.

Priority
--------
    1. emergency          the caller's kill-switch / risk halt
    2. thesis invalidated  price below HMA200, or HMA200 no longer rising
    3. VWAP loss          the day's volume traded above the current price
    4. volume decay       the volume that justified the entry has drained

Thesis invalidation outranks VWAP because it is the slower, more
structural failure: a symbol can lose VWAP intraday and recover, but a
symbol that has dropped through its 200-period HMA is no longer the setup
the scanner found. Ordering them the other way would close positions on
noise and hold them through the real breakdown.

Exits are never gated by entry risk
-----------------------------------
This module imports no allocator, no daily-loss guard, no kill switch,
and a test asserts it. A risk limit that also blocked liquidation would
trap the account in the position the limit exists to escape. A caller may
refuse to ENTER on risk; it may not use this module's answer to refuse to
LEAVE.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from config import s2_exit_v0 as policy

logger = logging.getLogger(__name__)

HOLD = "HOLD"
SELL = "SELL"

REASON_EMERGENCY = "EMERGENCY_LIQUIDATION"
REASON_BELOW_HMA200 = "S2_THESIS_BELOW_HMA200"
REASON_HMA200_NOT_RISING = "S2_THESIS_HMA200_NOT_RISING"
REASON_VWAP_LOSS = "S2_VWAP_LOSS"
REASON_VOLUME_DECAY = "S2_VOLUME_DECAY"
REASON_INSUFFICIENT_DATA = "S2_EXIT_DATA_UNAVAILABLE"


@dataclass(frozen=True)
class S2PositionState:
    symbol: str
    entry_price: float
    #: The volume multiple that made this a candidate. Decay is measured
    #: against THIS, not against a shared level.
    signal_volume_multiple: Optional[float] = None
    #: The average volume the multiple was computed from. Without it
    #: there is nothing for a current volume to be a multiple OF.
    baseline_volume: Optional[float] = None
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


def thesis_broken(features) -> Optional[Dict[str, Any]]:
    """S2's own entry conditions, asked again.

    Returns the reason and the numbers behind it, or None if the thesis
    still holds. Returns None -- not "broken" -- when the features cannot
    answer: an unmeasurable HMA is missing data, and selling on missing
    data would make a provider hiccup into a liquidation.
    """
    if features is None:
        return None
    price = _finite(getattr(features, "price", None))
    hma200 = _finite(getattr(features, "hma200", None))
    slope = _finite(getattr(features, "hma200_slope", None))

    if price is not None and hma200 is not None and price < hma200:
        return {"reason": REASON_BELOW_HMA200, "price": price, "hma200": hma200}
    if slope is not None and slope <= 0:
        return {"reason": REASON_HMA200_NOT_RISING, "hma200_slope": slope}
    return None


def vwap_lost(features) -> Optional[Dict[str, Any]]:
    """Price below VWAP: the day's volume traded higher than it is now.

    Strictly below. A price sitting exactly at VWAP has not lost it, and
    treating equality as a loss would exit on a rounding artefact.
    """
    if features is None:
        return None
    price = _finite(getattr(features, "price", None))
    vwap = _finite(getattr(features, "vwap", None))
    if price is None or vwap is None:
        return None
    if price < vwap:
        return {"reason": REASON_VWAP_LOSS, "price": price, "vwap": vwap}
    return None


def volume_decayed(state: S2PositionState, features) -> Optional[Dict[str, Any]]:
    """The excess volume that justified the entry has halved.

    Relative to the candidate's own trigger. A 6x that has fallen to 3.5x
    has lost half its excess over baseline; so has a 1.6x that fell to
    1.3x. A shared absolute threshold would call only the first decayed
    and would hold every quiet candidate forever.

    Unmeasurable inputs return None rather than a decay: no baseline
    means there is nothing to be a multiple of, and a candidate that was
    never elevated cannot decay -- reporting it as decayed would exit the
    quietest positions first, which inverts the finding.
    """
    multiple = _finite(state.signal_volume_multiple)
    baseline = _finite(state.baseline_volume)
    current = _finite(getattr(features, "volume", None)) if features else None
    if multiple is None or baseline is None or baseline <= 0 or current is None:
        return None
    excess = multiple - 1.0
    if excess <= 0:
        return None
    target = 1.0 + excess * (1.0 - policy.VOLUME_DECAY_FRACTION)
    now_multiple = current / baseline
    if now_multiple <= target:
        return {"reason": REASON_VOLUME_DECAY, "signal_multiple": multiple,
                "current_multiple": now_multiple, "decay_target": target,
                "decay_fraction": policy.VOLUME_DECAY_FRACTION}
    return None


def decide(state: S2PositionState, *, features=None,
           emergency: bool = False) -> ExitDecision:
    """One action for one position. Never places an order.

    `features` is the current observation. When it is absent the answer
    is HOLD with an explicit reason, never SELL: this module must not be
    able to turn a data outage into a liquidation.
    """
    if state.exit_submitted:
        # An exit is already live at the broker. Deciding again could
        # only ever produce a second SELL for one position.
        return ExitDecision(HOLD, reason="S2_EXIT_ALREADY_SUBMITTED")

    if emergency:
        return ExitDecision(SELL, reason=REASON_EMERGENCY)

    if features is None:
        return ExitDecision(HOLD, reason=REASON_INSUFFICIENT_DATA)

    if policy.EXIT_ON_THESIS_INVALIDATION:
        broken = thesis_broken(features)
        if broken:
            return ExitDecision(SELL, reason=broken.pop("reason"), detail=broken)

    if policy.EXIT_ON_VWAP_LOSS:
        lost = vwap_lost(features)
        if lost:
            return ExitDecision(SELL, reason=lost.pop("reason"), detail=lost)

    if policy.EXIT_ON_VOLUME_DECAY:
        decayed = volume_decayed(state, features)
        if decayed:
            return ExitDecision(SELL, reason=decayed.pop("reason"),
                                detail=decayed)

    return ExitDecision(HOLD, detail={"stop_status": policy.NO_STOP_REASON}
                        if policy.HARD_STOP_PCT is None else {})
