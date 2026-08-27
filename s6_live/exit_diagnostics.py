"""What each exit rule answered this tick, and which ones could not answer.

The gap this closes
-------------------
Every HOLD tick logged `action=HELD, reason=None, detail=""`. When the DT
position was examined afterwards, the monitor could say only that it had
held -- not at what price, not against which levels, and crucially not
that three of its seven rules had been unable to evaluate at all for
want of a VWAP. A rule that cannot run and a rule that ran and said "no"
produced identical output, which is precisely why the wiring defect
survived unnoticed.

TRUE / FALSE / UNAVAILABLE
--------------------------
Availability is decided from the INPUTS, before the predicate runs, so a
missing VWAP is reported as UNAVAILABLE rather than as a calm market.
The predicates themselves are imported from `exit_policy` and are not
reimplemented here: a second copy of a trading rule, kept in step by
hand, is a worse failure than the one this file exists to report.

This module decides nothing. `decide()` remains the only thing that
chooses an action; this records what it saw.
"""

import logging
from typing import Any, Dict, Optional

from s6_live import exit_policy
from s6_live import realtime_features as rf

logger = logging.getLogger(__name__)

TRUE = "TRUE"
FALSE = "FALSE"
UNAVAILABLE = "UNAVAILABLE"

#: reason -> the feature inputs its predicate reads. Availability is
#: judged from these; the predicate is only consulted once they exist.
INPUTS_BY_REASON = {
    exit_policy.REASON_HARD_RISK_CAP: ("price",),
    exit_policy.REASON_RANGE_REENTRY: ("price",),
    exit_policy.REASON_VWAP_FAILURE: ("price", "vwap"),
    exit_policy.REASON_EMA_STRUCTURE_FAILURE: ("ema9", "ema21"),
    exit_policy.REASON_VOLUME_DECAY_PRICE_WEAKNESS: ("volume_expansion",),
    exit_policy.REASON_SESSION_EXIT: (),
}

ORDERED_REASONS = (
    exit_policy.REASON_HARD_RISK_CAP,
    exit_policy.REASON_RANGE_REENTRY,
    exit_policy.REASON_VWAP_FAILURE,
    exit_policy.REASON_EMA_STRUCTURE_FAILURE,
    exit_policy.REASON_VOLUME_DECAY_PRICE_WEAKNESS,
    exit_policy.REASON_SESSION_EXIT,
)


def _available(features, names) -> bool:
    """Every named input present on the features object."""
    if features is None:
        return False
    for name in names:
        if isinstance(getattr(features, "unavailable", None), dict) \
                and name in features.unavailable:
            return False
        if getattr(features, name, None) is None:
            return False
    return True


def evaluate(state, *, features=None, price=None, session=None, now=None,
             decision=None) -> Dict[str, Any]:
    """One tick's full picture. Never raises."""
    try:
        return _evaluate(state, features=features, price=price,
                         session=session, now=now, decision=decision)
    except Exception as exc:  # noqa: BLE001 - diagnostics must not stop a tick
        logger.warning("S6 exit diagnostics failed", exc_info=True)
        return {"error": f"{type(exc).__name__}: {exc}"}


def _predicate(reason, state, features, price, session, now) -> Optional[dict]:
    if reason == exit_policy.REASON_HARD_RISK_CAP:
        return exit_policy.hard_risk_breached(state, price)
    if reason == exit_policy.REASON_RANGE_REENTRY:
        return exit_policy.range_reentered(state, price)
    if reason == exit_policy.REASON_VWAP_FAILURE:
        return exit_policy.vwap_failed(features)
    if reason == exit_policy.REASON_EMA_STRUCTURE_FAILURE:
        return exit_policy.ema_structure_failed(features)
    if reason == exit_policy.REASON_VOLUME_DECAY_PRICE_WEAKNESS:
        decayed = exit_policy.volume_decayed(state, features)
        weak = exit_policy.price_weak(state, features) if decayed else None
        return {**decayed, **weak} if (decayed and weak) else None
    if reason == exit_policy.REASON_SESSION_EXIT:
        return exit_policy.session_ending(session, now)
    return None


def _evaluate(state, *, features, price, session, now, decision):
    conditions: Dict[str, str] = {}
    detail: Dict[str, Any] = {}

    for reason in ORDERED_REASONS:
        names = INPUTS_BY_REASON.get(reason, ())
        # price comes from the live quote, not the features object, so it
        # is checked directly; everything else is a feature input.
        if reason in (exit_policy.REASON_HARD_RISK_CAP,
                      exit_policy.REASON_RANGE_REENTRY):
            ok = price is not None
        else:
            ok = _available(features, names)
        if not ok:
            conditions[reason] = UNAVAILABLE
            continue
        hit = _predicate(reason, state, features, price, session, now)
        conditions[reason] = TRUE if hit else FALSE
        if hit:
            detail[reason] = hit

    entry = getattr(state, "entry_price", None)
    pnl_pct = None
    if price is not None and entry:
        try:
            pnl_pct = (float(price) / float(entry) - 1.0) * 100.0
        except (TypeError, ZeroDivisionError):
            pnl_pct = None

    record: Dict[str, Any] = {
        "symbol": getattr(state, "symbol", None),
        "session": session if isinstance(session, str) else getattr(
            session, "name", None),
        "price": price,
        "entry_price": entry,
        "pnl_pct": pnl_pct,
        "range_high": getattr(state, "range_high", None),
        "range_low": getattr(state, "range_low", None),
        "peak_price": getattr(state, "peak_price", None),
        "conditions": conditions,
        "fired": detail,
    }
    if features is not None:
        if isinstance(features, rf.SessionFeatures):
            record["features"] = features.as_record(now)
        else:
            record["features"] = {
                name: getattr(features, name, None)
                for name in ("price", "vwap", "ema9", "ema21",
                             "volume", "volume_expansion")}
    if decision is not None:
        record["action"] = getattr(decision, "action", None)
        record["selected_exit_reason"] = getattr(decision, "reason", None)
    # The headline an operator reads first: a rule that could not run is
    # not the same as a quiet market, and this says which.
    record["unavailable_rules"] = sorted(
        name for name, value in conditions.items() if value == UNAVAILABLE)
    return record
