"""EXIT V2 PHASE 2: a shadow exit decision, computed and persisted
alongside the live one. Decides nothing live; `exit_policy.decide()`
remains the sole trading authority. Nothing in this module is imported
by anything that can submit, cancel, or latch an order.

What the data actually shows (2026-09-09/10, RIG/SCL/KVYO -- see the
Phase 2 report for the full breakdown)
------------------------------------------------------------------------
Every VWAP breach captured in the available history recovered before
the record ends: SCL two episodes (17 ticks/46min, 37 ticks/102min),
RIG one episode (10 ticks/68min), KVYO one single-tick flicker. Zero
observed breaches PERSISTED to a position's close without recovering
first. That is real evidence that patience has value here -- it is
NOT evidence for any particular number of ticks, because there is no
confirmed-failure example in this sample to balance against a
confirmed-recovery one. `VWAP_CONFIRMATION_TICKS` below is therefore a
named, provisional placeholder (2 -- filters the one observed single-
tick flicker, nothing more), not a calibrated threshold. It exists so
shadow data can accumulate toward calibrating it for real in a later
phase, per this phase's own explicit instruction not to invent one.

Hard stop and emergency exits are never delayed
--------------------------------------------------
`NEVER_DELAYED_REASONS` mirrors live's HARD_RISK_CAP / EMERGENCY /
NO_STRUCTURE / SESSION_EXIT / RANGE_REENTRY decisions immediately and
verbatim -- shadow never invents patience for a rule the live policy
did not ask to be softened. VWAP_FAILURE is the one explicit exception
this phase asks for; EMA_STRUCTURE_FAILURE and
VOLUME_DECAY_PRICE_WEAKNESS are mirrored immediately too, since neither
is named in this phase's shadow-state vocabulary.
"""

import json
import logging
from typing import Any, Dict, Optional, Tuple

from s6_live import exit_policy

logger = logging.getLogger(__name__)

# -- VWAP shadow state -------------------------------------------------------
VWAP_HEALTHY = "VWAP_HEALTHY"
VWAP_BREACH = "VWAP_BREACH"
VWAP_RECOVERED = "VWAP_RECOVERED"
VWAP_FAILURE_CONFIRMED = "VWAP_FAILURE_CONFIRMED"
VWAP_UNKNOWN = "UNKNOWN"

#: PROVISIONAL -- see module docstring. Not derived from a confirmed-
#: failure example; none exists yet in the available history.
VWAP_CONFIRMATION_TICKS = 2

# -- structure shadow state ---------------------------------------------
STRUCTURE_HEALTHY = "STRUCTURE_HEALTHY"
STRUCTURE_WEAKENING = "STRUCTURE_WEAKENING"
STRUCTURE_FAILED_BREAKOUT = "FAILED_BREAKOUT"
STRUCTURE_UNKNOWN = "UNKNOWN"

# -- time-stop shadow state (§5) -- never a live gate, see run_s6_runtime ---
TIME_STOP_NO_FOLLOW_THROUGH = "NO_FOLLOW_THROUGH"
TIME_STOP_WARNING = "TIME_STOP_WARNING"
TIME_STOP_EXIT_SHADOW = "TIME_STOP_EXIT_SHADOW"
TIME_STOP_NONE = None

#: PROVISIONAL, for the same reason as VWAP_CONFIRMATION_TICKS: RIG's
#: only observed favorable move was immediate (entry tick itself), and
#: no available closed position shows a genuinely stagnant multi-hour
#: hold to calibrate a real minutes threshold against.
TIME_STOP_NO_PROGRESS_MINUTES = 30.0
TIME_STOP_WARNING_MINUTES = 60.0

# -- shadow decision vocabulary ------------------------------------------
HOLD = "HOLD"
HOLD_FOR_CONFIRMATION = "HOLD_FOR_CONFIRMATION"
WOULD_EXIT = "WOULD_EXIT"
ALREADY_EXITING = "ALREADY_EXITING"

#: Reasons whose live SELL is never delayed or second-guessed by the
#: shadow policy -- see module docstring.
NEVER_DELAYED_REASONS = frozenset({
    exit_policy.REASON_EMERGENCY, exit_policy.REASON_HARD_RISK_CAP,
    exit_policy.REASON_NO_STRUCTURE, exit_policy.REASON_SESSION_EXIT,
    exit_policy.REASON_RANGE_REENTRY, exit_policy.REASON_EMA_STRUCTURE_FAILURE,
    exit_policy.REASON_PROFIT_PROTECTION_EXIT,
    exit_policy.REASON_VOLUME_DECAY_PRICE_WEAKNESS,
})


def _finite(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number or number in (float("inf"), float("-inf")) else number


def vwap_shadow_state(price, vwap, *, prior_state=None, prior_streak=0
                      ) -> Tuple[str, int]:
    """(state, breach_streak). `prior_state`/`prior_streak` come from the
    last persisted snapshot row for this position -- one indexed local
    read, the same pattern `s6_live.exit_snapshot`'s own vwap_state
    transition already uses."""
    price, vwap = _finite(price), _finite(vwap)
    if price is None or vwap is None:
        return VWAP_UNKNOWN, 0
    if price >= vwap:
        if prior_state in (VWAP_BREACH, VWAP_FAILURE_CONFIRMED):
            return VWAP_RECOVERED, 0
        return VWAP_HEALTHY, 0
    streak = (prior_streak or 0) + 1 if prior_state in (
        VWAP_BREACH, VWAP_FAILURE_CONFIRMED) else 1
    if streak >= VWAP_CONFIRMATION_TICKS:
        return VWAP_FAILURE_CONFIRMED, streak
    return VWAP_BREACH, streak


def structure_shadow_state(*, price, range_high, giveback_fraction
                           ) -> str:
    """Reuses `config.s6_exit_v0.PEAK_GIVEBACK_FRACTION` -- an existing,
    already-tuned system constant -- rather than inventing a new
    threshold for "weakening"."""
    from config import s6_exit_v0 as policy

    price, range_high = _finite(price), _finite(range_high)
    if price is not None and range_high is not None and price <= range_high:
        return STRUCTURE_FAILED_BREAKOUT
    fraction = _finite(giveback_fraction)
    if fraction is None:
        return STRUCTURE_UNKNOWN if price is None or range_high is None else STRUCTURE_HEALTHY
    if fraction >= 1.0:
        return STRUCTURE_FAILED_BREAKOUT
    if fraction >= policy.PEAK_GIVEBACK_FRACTION:
        return STRUCTURE_WEAKENING
    return STRUCTURE_HEALTHY


def time_stop_shadow_state(*, time_in_trade_seconds, peak_gain_pct,
                           current_gain_pct) -> Optional[str]:
    """Never a live gate (§5/§11) -- purely descriptive, and PROVISIONAL
    (see module docstring)."""
    minutes = _finite(time_in_trade_seconds)
    if minutes is None:
        return TIME_STOP_NONE
    minutes = minutes / 60.0
    peak = _finite(peak_gain_pct)
    current = _finite(current_gain_pct)
    ever_favorable = (peak is not None and peak > 0) or (current is not None and current > 0)
    if minutes >= TIME_STOP_WARNING_MINUTES and not ever_favorable:
        return TIME_STOP_EXIT_SHADOW
    if minutes >= TIME_STOP_NO_PROGRESS_MINUTES and not ever_favorable:
        return TIME_STOP_WARNING
    if minutes >= TIME_STOP_NO_PROGRESS_MINUTES and current is not None and current <= 0:
        return TIME_STOP_NO_FOLLOW_THROUGH
    return TIME_STOP_NONE


def decide_shadow(*, live_reason, live_action, vwap_state, liquidity_state,
                  structure_state, momentum_state) -> Dict[str, Any]:
    """One shadow verdict for this tick. `live_reason`/`live_action` are
    exactly `exit_policy.decide()`'s own output -- this reads the
    decision, it never recomputes or second-guesses the rules that made
    it, except the one explicit VWAP confirmation gate this phase asks
    for."""
    evidence = []

    if live_reason == exit_policy.REASON_ALREADY_SUBMITTED:
        return {
            "shadow_v2_decision": ALREADY_EXITING,
            "shadow_v2_reason": live_reason,
            "shadow_confidence": 1.0,
            "shadow_evidence": ["live:ALREADY_SUBMITTED"],
            "would_exit_now": True, "would_hold_now": False,
        }

    if live_action == exit_policy.SELL:
        if live_reason in NEVER_DELAYED_REASONS or live_reason is None:
            evidence.append(f"live:{live_reason}")
            return {
                "shadow_v2_decision": WOULD_EXIT, "shadow_v2_reason": live_reason,
                "shadow_confidence": 1.0, "shadow_evidence": evidence,
                "would_exit_now": True, "would_hold_now": False,
            }
        if live_reason == exit_policy.REASON_VWAP_FAILURE:
            if vwap_state == VWAP_FAILURE_CONFIRMED:
                evidence.append(f"vwap:{vwap_state}")
                return {
                    "shadow_v2_decision": WOULD_EXIT,
                    "shadow_v2_reason": VWAP_FAILURE_CONFIRMED,
                    "shadow_confidence": 0.8, "shadow_evidence": evidence,
                    "would_exit_now": True, "would_hold_now": False,
                }
            evidence.append(f"vwap:{vwap_state}")
            return {
                "shadow_v2_decision": HOLD_FOR_CONFIRMATION,
                "shadow_v2_reason": VWAP_BREACH,
                "shadow_confidence": 0.4, "shadow_evidence": evidence,
                "would_exit_now": False, "would_hold_now": True,
            }
        # An unnamed future SELL reason: mirror, never invent patience
        # for a rule this phase did not ask to be softened.
        evidence.append(f"live:{live_reason}")
        return {
            "shadow_v2_decision": WOULD_EXIT, "shadow_v2_reason": live_reason,
            "shadow_confidence": 1.0, "shadow_evidence": evidence,
            "would_exit_now": True, "would_hold_now": False,
        }

    # live_action is HOLD and nothing is latched: the only place a
    # shadow-only WOULD_EXIT (never triggered live) can originate --
    # §6's second example, §3's "liquidity alone must not produce a
    # SELL, only amplify".
    if liquidity_state == "CRITICAL":
        if vwap_state == VWAP_FAILURE_CONFIRMED:
            evidence.append("vwap:VWAP_FAILURE_CONFIRMED")
            evidence.append("liquidity:CRITICAL")
            return {
                "shadow_v2_decision": WOULD_EXIT,
                "shadow_v2_reason": "VWAP_FAILURE_CONFIRMED+LIQUIDITY_CRITICAL",
                "shadow_confidence": 0.7, "shadow_evidence": evidence,
                "would_exit_now": True, "would_hold_now": False,
            }
        if structure_state == STRUCTURE_FAILED_BREAKOUT:
            evidence.append("structure:FAILED_BREAKOUT")
            evidence.append("liquidity:CRITICAL")
            return {
                "shadow_v2_decision": WOULD_EXIT,
                "shadow_v2_reason": "FAILED_BREAKOUT+LIQUIDITY_CRITICAL",
                "shadow_confidence": 0.6, "shadow_evidence": evidence,
                "would_exit_now": True, "would_hold_now": False,
            }

    if vwap_state == VWAP_BREACH:
        evidence.append(f"vwap:{vwap_state}")
        return {
            "shadow_v2_decision": HOLD_FOR_CONFIRMATION,
            "shadow_v2_reason": VWAP_BREACH,
            "shadow_confidence": 0.3, "shadow_evidence": evidence,
            "would_exit_now": False, "would_hold_now": True,
        }

    return {
        "shadow_v2_decision": HOLD, "shadow_v2_reason": None,
        "shadow_confidence": 0.0, "shadow_evidence": [],
        "would_exit_now": False, "would_hold_now": True,
    }


def build(*, prior_row, snapshot: Dict[str, Any], live_action) -> Dict[str, Any]:
    """The full Phase 2 record for one tick, built from the SAME
    Phase 1 snapshot dict (no new market-data read), the real
    `exit_policy.decide()` action for this tick (never re-derived --
    reconstructing HOLD-vs-SELL from the reason string alone is exactly
    wrong for REASON_INSUFFICIENT_DATA, a HOLD with a non-None reason),
    and one prior persisted row (position_id-scoped, the same lookback
    `exit_snapshot.build`'s own vwap_state transition already reads,
    reused rather than queried a second time).
    """
    prior = prior_row or {}
    vwap_state, streak = vwap_shadow_state(
        snapshot.get("current_price"), snapshot.get("vwap"),
        prior_state=prior.get("shadow_vwap_state"),
        prior_streak=prior.get("shadow_vwap_breach_streak") or 0)

    diagnostics = snapshot.get("diagnostics_json") or {}
    peak = diagnostics.get("peak") or {}
    structure_state = structure_shadow_state(
        price=snapshot.get("current_price"), range_high=snapshot.get("range_high"),
        giveback_fraction=peak.get("giveback_fraction"))

    current_gain_pct = None
    entry_price = _finite(snapshot.get("entry_price"))
    price = _finite(snapshot.get("current_price"))
    if entry_price and price is not None:
        current_gain_pct = (price / entry_price - 1.0) * 100.0
    time_stop_state = time_stop_shadow_state(
        time_in_trade_seconds=snapshot.get("time_in_trade_seconds"),
        peak_gain_pct=snapshot.get("peak_gain_pct"),
        current_gain_pct=current_gain_pct)

    decision = decide_shadow(
        live_reason=snapshot.get("current_exit_reason"), live_action=live_action,
        vwap_state=vwap_state, liquidity_state=snapshot.get("liquidity_state"),
        structure_state=structure_state, momentum_state=snapshot.get("momentum_state"))

    record = dict(snapshot)
    record["live_exit_reason"] = snapshot.get("current_exit_reason")
    record["shadow_vwap_state"] = vwap_state
    record["shadow_vwap_breach_streak"] = streak
    record["shadow_liquidity_state"] = snapshot.get("liquidity_state")
    record["shadow_structure_state"] = structure_state
    record["shadow_momentum_state"] = snapshot.get("momentum_state")
    record["shadow_time_stop_state"] = time_stop_state
    record.update(decision)
    record["shadow_evidence"] = json.dumps(decision.get("shadow_evidence") or [])
    return record
