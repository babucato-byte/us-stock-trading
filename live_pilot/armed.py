"""ARMED-posture dispatch: the real buy cycle and the real exit tick.

This module is imported ONLY after `live_pilot.posture.resolve_posture()`
has returned ARMED -- i.e. only after the operator set all three of
KIS_LIVE_ORDER_ENABLED, LIVE_ROLLOUT_ENABLED and (not) ENTRY_DISABLED in
their own environment file. Nothing here turns a flag on, and nothing
imports this module at package scope; `live_pilot/__init__.py`,
`runner.py`, `preflight.py`, `observe.py` and `recorder.py` all import
cleanly without it, which is what keeps an OBSERVE session structurally
unable to reach an order path.

It adds no rule of its own. Both halves are one call each into code that
already exists, is already gated and is already tested:

    entry  kis_live_trading.run_live_buy_entry_cycle()
             -> per-symbol Order Gate, price re-check, cash, duplicate,
                allow-list, reconciliation, then submit
    exit   kis_position_manager.sync_kis_fills_and_manage_exits()
             -> fill sync, stop/target finalisation from the ACTUAL
                average fill price, then positions.lifecycle.
                check_and_manage() verbatim for every open position

The gates inside those two functions are the real defence. This module
is a caller, not a second gate: adding "extra" checks here would create
a place where the pilot's idea of safety and the live service's idea of
safety could differ.
"""

import logging

logger = logging.getLogger("live_pilot.armed")


def entry_cycle(*, broker, now):
    """One live buy-entry cycle. Returns the tick's `entry` section.

    `run_live_buy_entry_cycle()` raises KISLiveTradingError for a
    structural refusal (rollout disabled, HALT, ENTRY_OFF, commit
    mismatch, unconfigured account) and never for a per-symbol block. A
    refusal is recorded as the tick's entry error and the loop continues:
    the operator turning ENTRY_OFF on mid-session is a normal event, not
    a reason to kill a running pilot.
    """
    import kis_live_trading as klt

    try:
        results = klt.run_live_buy_entry_cycle(broker=broker, now=now)
    except klt.KISLiveTradingError as exc:
        logger.warning("live buy-entry cycle refused this tick: %s", exc)
        return {
            "mode": "ARMED",
            "entrypoint": "kis_live_trading.run_live_buy_entry_cycle",
            "evaluations": 0, "outcomes": [], "submitted": [],
            "error": f"CYCLE_REFUSED: {exc}",
        }

    # The three lists do NOT share a shape: `blocked` and `skipped` hold
    # (symbol, reason) pairs, `submitted` holds bare symbols. Normalised
    # through _split_pair() so the recorded tick has one shape and a
    # reason never ends up in the symbol field.
    outcomes = []
    for item in results.get("blocked") or []:
        symbol, reason = _split_pair(item)
        outcomes.append({"symbol": symbol, "result": "BLOCKED",
                         "reason_code": reason or "BLOCKED", "hypothetical": None})
    for item in results.get("skipped") or []:
        symbol, reason = _split_pair(item)
        outcomes.append({"symbol": symbol, "result": "SKIPPED",
                         "reason_code": reason or "SKIPPED", "hypothetical": None})
    submitted = [_split_pair(item)[0] for item in results.get("submitted") or []]
    for symbol in submitted:
        outcomes.append({"symbol": symbol, "result": "SUBMITTED",
                         "reason_code": None, "hypothetical": None})
    return {
        "mode": "ARMED",
        "entrypoint": "kis_live_trading.run_live_buy_entry_cycle",
        "evaluations": len(outcomes),
        "outcomes": outcomes,
        "submitted": submitted,
        "error": None,
    }


def build_adapter(broker, *, rollout=None, is_regular_session_fn=None):
    """The sell-side adapter `sync_kis_fills_and_manage_exits()` submits
    through. Its allow-list and price-deviation limit come from the SAME
    LiveRolloutConfig the buy path uses -- not from pilot-specific
    settings, which would let the two sides disagree about which symbols
    are tradable."""
    from brokers.kis_broker_adapter import KISBrokerAdapter
    from config.live_rollout_config import LiveRolloutConfig

    config = rollout or LiveRolloutConfig.from_env()
    kwargs = {
        "allowed_symbols": config.allowed_symbols,
        "max_price_deviation_percent": config.max_price_deviation_percent,
    }
    if is_regular_session_fn is not None:
        kwargs["is_regular_session_fn"] = is_regular_session_fn
    return KISBrokerAdapter(broker, **kwargs)


def exit_cycle(*, broker, now, adapter=None, is_regular_session_fn=None):
    """One live exit tick. Returns the tick's `exit` section.

    A KISPositionManagerError means the KIS position read failed, which
    aborts THIS tick only -- the next tick retries. It is recorded, not
    raised, for the same reason as above.
    """
    import kis_position_manager

    broker_adapter = adapter or build_adapter(
        broker, is_regular_session_fn=is_regular_session_fn)
    try:
        summary = kis_position_manager.sync_kis_fills_and_manage_exits(
            kis_broker=broker, broker_adapter=broker_adapter, now=now,
        )
    except kis_position_manager.KISPositionManagerError as exc:
        logger.warning("exit tick aborted: %s", exc)
        return {
            "mode": "ARMED",
            "entrypoint": "kis_position_manager.sync_kis_fills_and_manage_exits",
            "evaluations": 0, "outcomes": [], "error": f"TICK_ABORTED: {exc}",
        }

    # Shapes again differ: `managed`/`synced_fills` are bare symbols,
    # `skipped`/`reconciliation_blocked` are (symbol, reason) pairs.
    #
    # Honest limitation: the summary does NOT carry WHICH exit reason
    # fired -- kis_position_manager appends the symbol alone once
    # check_and_manage() returns. The reason is recorded where it is
    # actually produced (the position record and the sell-side audit
    # events written by brokers/kis_broker_adapter.py), and inventing one
    # here would be a guess. The tick says "managed", not why.
    outcomes = []
    for managed in summary.get("managed") or []:
        symbol, detail = _split_pair(managed)
        outcomes.append({"symbol": symbol, "decision": "MANAGED",
                         "result": "APPROVED", "reason_code": detail or "MANAGED"})
    for blocked in summary.get("reconciliation_blocked") or []:
        symbol, detail = _split_pair(blocked)
        outcomes.append({"symbol": symbol, "decision": None,
                         "result": "BLOCKED", "reason_code": detail or "RECONCILIATION_BLOCKED"})
    for skipped in summary.get("skipped") or []:
        symbol, detail = _split_pair(skipped)
        outcomes.append({"symbol": symbol, "decision": None,
                         "result": "SKIPPED", "reason_code": detail or "SKIPPED"})
    return {
        "mode": "ARMED",
        "entrypoint": "kis_position_manager.sync_kis_fills_and_manage_exits",
        "evaluations": len(outcomes),
        "synced_fills": list(summary.get("synced_fills") or []),
        "outcomes": outcomes,
        "error": None,
    }


def _split_pair(item):
    """The summary lists hold either a bare symbol or a (symbol, reason)
    pair depending on the branch. Normalised here so the recorded tick
    has one shape."""
    if isinstance(item, (tuple, list)) and len(item) == 2:
        return str(item[0]), str(item[1])
    if isinstance(item, dict):
        return item.get("symbol"), item.get("reason")
    return str(item), None
