"""EXIT V2 PHASE 1: one durable row per S6 exit evaluation. Records what
was seen; decides nothing.

The gap this closes
--------------------
`exit_diagnostics.evaluate()` already computes almost this entire record
every tick -- HOLD and SELL alike -- and it was discarded after one
in-process log line. RIG, 2026-08-28: EMA_STRUCTURE_FAILURE latched at
19:52, but the position sold three days later on whatever condition
happened to be true on the tick the order finally went out, and because
`decide()`'s fresh reason overwrote the latched one in the trade record,
the sale was studied under a rule that did not cause it. The tick that
actually mattered left no durable trace once the position closed. This
module is that trace.

Everything here is read from data the position monitor already computed
for THIS tick -- `exit_diagnostics.evaluate()`'s record, the position
row, and (one indexed local read, never a broker call) the most recent
prior snapshot for the same position. No new market-data or KIS call is
added anywhere in this module.

Shadow vocabulary, not a decision
----------------------------------
`vwap_state` / `liquidity_state` / `momentum_state` are OBSERVATIONAL
classifications for the future VWAP-confirmation, liquidity-persistence
and momentum work this phase exists to ground in data. `exit_policy.decide()`
is not called from here, does not read this module, and nothing computed
in this file feeds back into a trading decision -- see
`s6_live/exit_runtime.py`'s wiring, which persists the snapshot strictly
AFTER the decision is already made.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from s6_live import exit_policy

logger = logging.getLogger(__name__)

#: `exit_policy.decide()`'s own priority order, restated as a rank so a
#: replay can sort/compare without re-reading the module docstring.
#: 0 is the pre-priority corrupted-state check; SELL reasons that never
#: fire (a HOLD tick) leave this None, not 0 -- "nothing fired" and "the
#: lowest-priority rule fired" must not look the same.
PRIORITY_BY_REASON = {
    exit_policy.REASON_NO_STRUCTURE: 0,
    exit_policy.REASON_EMERGENCY: 1,
    exit_policy.REASON_HARD_RISK_CAP: 2,
    exit_policy.REASON_RANGE_REENTRY: 3,
    exit_policy.REASON_VWAP_FAILURE: 4,
    exit_policy.REASON_EMA_STRUCTURE_FAILURE: 5,
    exit_policy.REASON_VOLUME_DECAY_PRICE_WEAKNESS: 6,
    exit_policy.REASON_SESSION_EXIT: 7,
}

# -- vwap_state ---------------------------------------------------------
VWAP_ABOVE = "ABOVE"
VWAP_BREACH = "BREACH"
VWAP_BELOW = "BELOW"
VWAP_RECOVERED = "RECOVERED"
VWAP_UNKNOWN = "UNKNOWN"

# -- liquidity_state ------------------------------------------------------
LIQUIDITY_NORMAL = "NORMAL"
LIQUIDITY_WARNING = "WARNING"
LIQUIDITY_CRITICAL = "CRITICAL"
LIQUIDITY_UNKNOWN = "UNKNOWN"

#: Purely observational bucketing, separate from
#: `s6_live.execution_liquidity`'s own entry-side gate thresholds -- this
#: classifies an already-open position's exit liquidity, not a BUY
#: decision, and reusing the entry gate's exact floors would conflate
#: two different questions asked of the same numbers.
LIQUIDITY_CRITICAL_DOLLAR_VOLUME_5M = 100.0
LIQUIDITY_WARNING_DOLLAR_VOLUME_5M = 500.0

# -- momentum_state -------------------------------------------------------
MOMENTUM_HEALTHY = "HEALTHY"
MOMENTUM_WEAKENING = "WEAKENING"
MOMENTUM_FAILED = "FAILED"
MOMENTUM_UNKNOWN = "UNKNOWN"


def _finite(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number or number in (float("inf"), float("-inf")) else number


def _parse_at(stamp) -> Optional[datetime]:
    if stamp is None:
        return None
    if isinstance(stamp, datetime):
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _last_snapshot(conn, position_id) -> Optional[Dict[str, Any]]:
    """The most recently persisted row for this position, or None.

    One indexed local read (idx_s6_exit_snapshots_position), never a
    network call -- used only to classify a state TRANSITION (a breach
    vs. a continuing below), never to alter this tick's own measurements.
    """
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT vwap_state FROM s6_exit_snapshots WHERE position_id = ? "
            "ORDER BY evaluated_at DESC, snapshot_id DESC LIMIT 1",
            (position_id,),
        ).fetchone()
        return dict(row) if row else None
    except Exception:  # noqa: BLE001 - a missing table/read fault is "no history"
        logger.debug("exit_snapshot: could not read prior snapshot for %s",
                     position_id, exc_info=True)
        return None


def _vwap_state(price, vwap, previous_state) -> str:
    if price is None or vwap is None:
        return VWAP_UNKNOWN
    above_now = price >= vwap
    was_below = previous_state in (VWAP_BELOW, VWAP_BREACH)
    was_above = previous_state in (VWAP_ABOVE, VWAP_RECOVERED)
    if above_now:
        return VWAP_RECOVERED if was_below else VWAP_ABOVE
    return VWAP_BREACH if (was_above or previous_state in (None, VWAP_UNKNOWN)) else VWAP_BELOW


def _liquidity_state(quality) -> str:
    if quality is None:
        return LIQUIDITY_UNKNOWN
    dollar_volume = _finite(getattr(quality, "dollar_volume_5m", None))
    if dollar_volume is None:
        return LIQUIDITY_UNKNOWN
    if dollar_volume < LIQUIDITY_CRITICAL_DOLLAR_VOLUME_5M:
        return LIQUIDITY_CRITICAL
    if dollar_volume < LIQUIDITY_WARNING_DOLLAR_VOLUME_5M:
        return LIQUIDITY_WARNING
    return LIQUIDITY_NORMAL


def _momentum_state(conditions, would_sell_reason) -> str:
    if not conditions:
        return MOMENTUM_UNKNOWN
    vwap_c = conditions.get(exit_policy.REASON_VWAP_FAILURE)
    ema_c = conditions.get(exit_policy.REASON_EMA_STRUCTURE_FAILURE)
    if vwap_c == "TRUE" or ema_c == "TRUE":
        return MOMENTUM_FAILED
    if would_sell_reason == exit_policy.REASON_VOLUME_DECAY_PRICE_WEAKNESS:
        return MOMENTUM_WEAKENING
    if vwap_c == "UNAVAILABLE" and ema_c == "UNAVAILABLE":
        return MOMENTUM_UNKNOWN
    return MOMENTUM_HEALTHY


def build(*, conn, position_id, row, features, diagnostics, decision,
         now=None) -> Dict[str, Any]:
    """The full persisted record for one tick. Never raises -- a failure
    to build a shadow record must not touch the trading decision that
    already happened; the caller wraps this in its own try/except too,
    but a partial record here is still better than none."""
    moment = now or datetime.now(timezone.utc)
    quality = getattr(features, "entry_quality", None)
    price = diagnostics.get("price")
    vwap = _finite(getattr(features, "vwap", None))
    entry_price = _finite(row.get("entry_price"))
    peak = diagnostics.get("peak") or {}
    peak_price = _finite(peak.get("peak_price"))
    conditions = diagnostics.get("conditions") or {}

    previous = _last_snapshot(conn, position_id)
    previous_vwap_state = (previous or {}).get("vwap_state")
    vwap_state = _vwap_state(price, vwap, previous_vwap_state)

    time_in_trade_seconds = None
    entered_at = _parse_at(row.get("entry_time"))
    if entered_at is not None:
        time_in_trade_seconds = (moment - entered_at).total_seconds()

    price_minus_vwap = (price - vwap) if (price is not None and vwap is not None) else None
    price_vs_vwap_pct = (
        (price / vwap - 1.0) * 100.0
        if price is not None and vwap not in (None, 0) else None)
    peak_gain_pct = (
        (peak_price / entry_price - 1.0) * 100.0
        if peak_price is not None and entry_price not in (None, 0) else None)

    session_for_bar_count = diagnostics.get("session")
    nonzero_bar_count = None
    if quality is not None:
        try:
            from s6_live.realtime_features import KIS_AUTHORITATIVE_SESSIONS

            if str(session_for_bar_count or "").upper() in KIS_AUTHORITATIVE_SESSIONS:
                # Only the KIS collector store's sparse-by-construction
                # bars (no print, no bar) make bar_count an EXACT
                # nonzero count; the yfinance-backed REGULAR frame is
                # zero-padded per minute and bar_count there counts
                # every minute, traded or not -- reporting it as
                # "nonzero" would fabricate a number this data cannot
                # support.
                nonzero_bar_count = getattr(quality, "bar_count", None)
        except Exception:  # noqa: BLE001
            nonzero_bar_count = None

    active_intent = None
    if conn is not None:
        try:
            from state_store import exit_intent_ledger

            active_intent = exit_intent_ledger.get_active_intent(conn, position_id)
        except Exception:  # noqa: BLE001
            logger.debug("exit_snapshot: could not read active exit intent for %s",
                         position_id, exc_info=True)

    reason = getattr(decision, "reason", None)

    record: Dict[str, Any] = {
        "position_id": position_id,
        "symbol": row.get("symbol") or diagnostics.get("symbol"),
        "session": diagnostics.get("session"),
        "evaluated_at": moment.isoformat(),
        "entry_price": entry_price,
        "current_price": price,
        "position_qty": _finite(row.get("quantity")),
        "time_in_trade_seconds": time_in_trade_seconds,
        "range_high": _finite(row.get("range_high")),
        "range_low": _finite(row.get("range_low")),
        "vwap": vwap,
        "price_minus_vwap": price_minus_vwap,
        "price_vs_vwap_pct": price_vs_vwap_pct,
        "ema9": _finite(getattr(features, "ema9", None)),
        "ema21": _finite(getattr(features, "ema21", None)),
        "current_exit_reason": reason,
        "current_exit_priority": PRIORITY_BY_REASON.get(reason),
        "range_reentry": conditions.get(exit_policy.REASON_RANGE_REENTRY),
        "hard_risk_cap": conditions.get(exit_policy.REASON_HARD_RISK_CAP),
        "vwap_breach": conditions.get(exit_policy.REASON_VWAP_FAILURE),
        "ema_structure_failure": conditions.get(exit_policy.REASON_EMA_STRUCTURE_FAILURE),
        "recent_volume_5m": _finite(getattr(quality, "recent_volume_5m", None)),
        "recent_volume_10m": _finite(getattr(quality, "recent_volume_10m", None)),
        "recent_volume_15m": _finite(getattr(quality, "recent_volume_15m", None)),
        "dollar_volume_5m": _finite(getattr(quality, "dollar_volume_5m", None)),
        "nonzero_bar_count": nonzero_bar_count,
        "data_age_seconds": _finite(getattr(quality, "data_age_seconds", None)),
        "peak_price": peak_price,
        "peak_gain_pct": peak_gain_pct,
        "drawdown_from_peak_pct": _finite(peak.get("peak_drawdown_pct")),
        "exit_submitted": bool(row.get("exit_submitted")),
        "active_exit_intent_id": (active_intent or {}).get("intent_id"),
        "active_broker_order_id": (active_intent or {}).get("broker_order_id"),
        "vwap_state": vwap_state,
        "liquidity_state": _liquidity_state(quality),
        "momentum_state": _momentum_state(conditions, diagnostics.get("would_sell_reason")),
    }
    record["diagnostics_json"] = diagnostics
    return record


_COLUMNS = (
    "position_id", "symbol", "session", "evaluated_at", "entry_price",
    "current_price", "position_qty", "time_in_trade_seconds", "range_high",
    "range_low", "vwap", "price_minus_vwap", "price_vs_vwap_pct", "ema9",
    "ema21", "current_exit_reason", "current_exit_priority", "range_reentry",
    "hard_risk_cap", "vwap_breach", "ema_structure_failure",
    "recent_volume_5m", "recent_volume_10m", "recent_volume_15m",
    "dollar_volume_5m", "nonzero_bar_count", "data_age_seconds",
    "peak_price", "peak_gain_pct", "drawdown_from_peak_pct", "exit_submitted",
    "active_exit_intent_id", "active_broker_order_id", "vwap_state",
    "liquidity_state", "momentum_state",
)


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def persist(conn, record: Dict[str, Any], *, now=None) -> None:
    """Append one row and commit immediately -- the same per-write commit
    convention `position_store.observe`/`latch_pending_exit` already use,
    so this row is durable even if a later step in the same tick raises.
    Never raises itself: a failed write costs one missing instrumentation
    row, never the tick that computed it."""
    try:
        moment = now or datetime.now(timezone.utc)
        values = [record.get(name) for name in _COLUMNS]
        values[_COLUMNS.index("exit_submitted")] = (
            1 if record.get("exit_submitted") else 0)
        placeholders = ", ".join("?" for _ in _COLUMNS)
        conn.execute(
            f"INSERT INTO s6_exit_snapshots ({', '.join(_COLUMNS)}, "
            "diagnostics_json, created_at) VALUES "
            f"({placeholders}, ?, ?)",
            (*values, json.dumps(record.get("diagnostics_json") or {},
                                 default=_json_default), moment.isoformat()),
        )
        conn.commit()
    except Exception:  # noqa: BLE001
        logger.warning("S6 exit snapshot persist failed for %s",
                       record.get("position_id"), exc_info=True)
