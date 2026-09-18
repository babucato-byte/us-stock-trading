"""What a profit-protection exit WOULD have done. Never an order.

Why
---
Measured on 40 canonical ORB5 positions closed 2026-09-08..09-17: the
median position that ever showed a profit captured 17.9% of its peak, and
12 of them turned a positive peak into a realised loss. The system holds a
median of 40 minutes past the peak.

Making the existing rules fire earlier does NOT fix that. Twelve variants
of RANGE_REENTRY / VWAP_FAILURE / EMA_STRUCTURE_FAILURE were replayed and
every one of them moved the median by roughly nothing while INCREASING the
number of positive-peak positions that ended negative -- those rules also
fire before a position is ever profitable, so making them faster just
confirms losses sooner.

What changed the sign was gating on profit already earned. The same
weakness test, applied only once a position has shown >= +1%, replayed at
+0.704% median against a realised median of +0.023%.

So the question worth measuring live is narrow: for a position that has
ALREADY earned a peak, does `price < EMA9` with a non-rising EMA9 mark the
end of the move? This records the answer. It does not act on it.

What this cannot do
-------------------
It cannot sell. It receives the decision `exit_policy.decide()` already
made and writes it down beside its own hypothetical; it returns nothing
the caller uses. It imports no broker, no execution, no order path, and it
never writes to the position store -- `tests/test_s6_profit_protection_
shadow.py` pins all of that.

EMA9 slope without a new indicator
----------------------------------
The slope is this tick's `ema9` minus the PREVIOUS TICK's, and the
previous value is kept here, in the observation area -- not on the
position row. A column on `s6_positions` would put a research quantity
inside the record that reconciliation compares against the broker, and
`peak_volume_expansion` already shows what that costs: it is written from
a feature computed on a different opening range than the one the position
was entered on.

No production default for the root
----------------------------------
`shadow_signal_log` wrote nothing for weeks because a default root put its
files inside the release directory, where the next deploy erased them. The
root comes from the environment or the observation is skipped and says so.
"""
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SUBDIR = "profit_protection_shadow"

#: The two candidates under observation. Peak gates in PERCENT.
MODEL_1P0 = "PROFIT_PROTECTION_EMA_1P0_SHADOW"
MODEL_1P5 = "PROFIT_PROTECTION_EMA_1P5_SHADOW"
GATE_1P0 = 1.0
GATE_1P5 = 1.5

EVENT = "PROFIT_PROTECTION_SHADOW"


def _root(env=None) -> Optional[str]:
    env = env if env is not None else os.environ
    return (env.get("PROFIT_PROTECTION_SHADOW_DIR")
            or env.get("SCANNER_DATA_ROOT") or None)


def log_path(trading_day, *, env=None) -> Optional[Path]:
    root = _root(env)
    if not root:
        return None
    return Path(root) / SUBDIR / f"{trading_day}.jsonl"


def state_path(*, env=None) -> Optional[Path]:
    """Where the previous tick's EMA9 and the first-trigger latch live.

    Keyed by position id. Small, and deliberately outside the trading
    database: losing this file costs one slope reading, never a trade.
    """
    root = _root(env)
    if not root:
        return None
    return Path(root) / SUBDIR / "state.json"


def _load_state(env=None) -> Dict[str, Any]:
    path = state_path(env=env)
    if path is None:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 - a corrupt observation file is not a fault
        logger.warning("profit-protection shadow state unreadable", exc_info=True)
        return {}


def _save_state(state, env=None) -> bool:
    path = state_path(env=env)
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, default=str)
        os.replace(temp, path)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("profit-protection shadow state not written", exc_info=True)
        return False


def _finite(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (
        float("inf"), float("-inf")) else None


def triggered(*, peak_pnl_pct, price, ema9, ema9_slope, gate) -> bool:
    """The candidate rule, in one place so the tests and the recorder
    cannot drift.

    Every input must be present. An absent EMA9 or an unknown slope is
    NOT a trigger: the whole point of the measurement is what the rule
    would have done on evidence it actually had.
    """
    for value in (peak_pnl_pct, price, ema9, ema9_slope):
        if value is None:
            return False
    return (peak_pnl_pct >= gate and price < ema9 and ema9_slope <= 0)


def append(record, *, trading_day, env=None) -> bool:
    try:
        path = log_path(trading_day, env=env)
        if path is None:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
        return True
    except Exception:  # noqa: BLE001 - losing an observation costs no trade
        logger.warning("profit-protection shadow append failed", exc_info=True)
        return False


def observe(*, position_id, symbol, session, trading_day, now,
            entry_price, price, peak_price, ema9,
            decision_action, decision_reason, env=None) -> Optional[Dict[str, Any]]:
    """Record one tick. Returns the record, or None when nothing was
    recorded.

    The caller uses the return value for nothing. It exists so the tests
    can assert on what would be written without reading a file.
    """
    entry = _finite(entry_price)
    now_price = _finite(price)
    peak = _finite(peak_price)
    fast = _finite(ema9)

    state = _load_state(env)
    mine = state.get(str(position_id)) or {}
    previous = _finite(mine.get("previous_ema9"))
    slope = (fast - previous) if (fast is not None and previous is not None) else None

    peak_pnl = ((peak / entry - 1.0) * 100.0) if (entry and entry > 0 and peak) else None
    now_pnl = ((now_price / entry - 1.0) * 100.0) if (entry and entry > 0 and now_price) else None

    hit_1p0 = triggered(peak_pnl_pct=peak_pnl, price=now_price, ema9=fast,
                        ema9_slope=slope, gate=GATE_1P0)
    hit_1p5 = triggered(peak_pnl_pct=peak_pnl, price=now_price, ema9=fast,
                        ema9_slope=slope, gate=GATE_1P5)

    # The FIRST trigger is latched per model, so the eventual comparison
    # against the real exit does not depend on finding the right row in a
    # day of ticks. A later tick never relabels it.
    for key, hit in (("1p0", hit_1p0), ("1p5", hit_1p5)):
        if hit and not mine.get(f"shadow_{key}_triggered_at"):
            mine[f"shadow_{key}_triggered_at"] = str(now)
            mine[f"shadow_{key}_trigger_price"] = now_price
            mine[f"shadow_{key}_trigger_pnl_pct"] = now_pnl
    # Highest price seen AFTER the first trigger -- the missed-upside
    # evidence, kept incrementally so it survives sparse ticks.
    for key in ("1p0", "1p5"):
        if mine.get(f"shadow_{key}_triggered_at") and now_price is not None:
            seen = _finite(mine.get(f"post_shadow_{key}_max_price"))
            if seen is None or now_price > seen:
                mine[f"post_shadow_{key}_max_price"] = now_price

    if fast is not None:
        mine["previous_ema9"] = fast
    mine["symbol"] = symbol
    state[str(position_id)] = mine
    _save_state(state, env)

    record = {
        "event": EVENT,
        "symbol": symbol,
        "position_id": position_id,
        "session": session,
        "timestamp": str(now),
        "entry_price": entry,
        "price": now_price,
        "running_peak_price": peak,
        "running_peak_pnl_pct": peak_pnl,
        "pnl_pct": now_pnl,
        "ema9": fast,
        "previous_ema9": previous,
        "ema9_slope": slope,
        "model_1p0": MODEL_1P0,
        "model_1p5": MODEL_1P5,
        "model_1p0_triggered": hit_1p0,
        "model_1p5_triggered": hit_1p5,
        # What production ACTUALLY did on this same tick, so the pair is
        # never assembled from two sources that might disagree.
        "actual_exit_decision": decision_action,
        "actual_exit_reason": decision_reason,
        "shadow_triggered_at": mine.get("shadow_1p0_triggered_at"),
        "shadow_trigger_price": mine.get("shadow_1p0_trigger_price"),
        "shadow_trigger_pnl_pct": mine.get("shadow_1p0_trigger_pnl_pct"),
        "shadow_1p5_triggered_at": mine.get("shadow_1p5_triggered_at"),
        "shadow_1p5_trigger_price": mine.get("shadow_1p5_trigger_price"),
        "shadow_1p5_trigger_pnl_pct": mine.get("shadow_1p5_trigger_pnl_pct"),
        "post_shadow_1p0_max_price": mine.get("post_shadow_1p0_max_price"),
        "post_shadow_1p5_max_price": mine.get("post_shadow_1p5_max_price"),
    }
    append(record, trading_day=trading_day, env=env)
    logger.info(
        "%s symbol=%s position_id=%s session=%s price=%s running_peak_pnl_pct=%s "
        "ema9=%s previous_ema9=%s ema9_slope=%s model_1p0_triggered=%s "
        "model_1p5_triggered=%s actual_exit_decision=%s actual_exit_reason=%s",
        EVENT, symbol, position_id, session, now_price, peak_pnl, fast,
        previous, slope, hit_1p0, hit_1p5, decision_action, decision_reason)
    return record
