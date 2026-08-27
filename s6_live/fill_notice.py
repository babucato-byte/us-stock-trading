"""The BUY-FILLED message, carrying why the symbol was bought.

The message this replaces
-------------------------
A fill notice that says only symbol, quantity and price answers "what
happened" and not "why", and the DT post-mortem needed the second. An
operator reading that a stock filled at 52.75 could not see that the
candidate behind it described a market from three hours earlier, that
the session had no volume to judge, or that the price was 4% above the
range the strategy claimed to be trading.

Every field here already exists somewhere. The point is that they
appear TOGETHER, in the one message a person actually reads, at the
moment the money moves.

`candidate_generated_at` and `market_data_asof` are printed as separate
lines even when they are close, because the entire DT failure lives in
the gap between them and a format that shows only one of them would hide
it again.

Formatting only. This builds a string and never decides anything.
"""

from typing import Any, Dict, Optional

#: Printed when a value genuinely is not known, rather than omitting the
#: line. A missing line reads as "fine"; this reads as "nobody checked".
UNKNOWN = "unknown"


def _fmt(value, suffix=""):
    if value is None or value == "":
        return UNKNOWN
    if isinstance(value, float):
        return f"{value:,.4f}".rstrip("0").rstrip(".") + suffix
    return f"{value}{suffix}"


def build(*, symbol, quantity, fill_price, session=None, strategy="S6 ORB Breakout v1",
          rank=None, score=None, candidate_generated_at=None,
          market_data_asof=None, entry_state=None, gate_results=None,
          broker_order_id=None, position_state="OPEN",
          conditions: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The ordered field map for a live BUY fill notification.

    Returned as a dict rather than a string so `live_notifications.notify`
    can redact it and render it the same way as every other event --
    dicts preserve insertion order, so this IS the printed order.
    """
    fields: Dict[str, Any] = {
        "strategy": strategy,
        "symbol": symbol,
        "entry_session": _fmt(session),
        "quantity": _fmt(quantity),
        "fill_price": _fmt(fill_price),
        "candidate_rank": _fmt(rank),
        "candidate_score": _fmt(score),
        # The two timestamps, always both. See the module docstring.
        "candidate_generated_at": _fmt(candidate_generated_at),
        "market_data_asof": _fmt(market_data_asof),
        "entry_state": _fmt(entry_state),
    }
    if conditions:
        # One line per gate, so a PASS that was actually UNAVAILABLE
        # cannot hide inside a single summary word.
        for name in sorted(conditions):
            fields[f"gate_{name.lower()}"] = conditions[name]
    elif gate_results:
        fields["gates"] = gate_results
    fields["kis_order"] = _fmt(broker_order_id)
    fields["position"] = _fmt(position_state)
    return fields


def from_watch(evaluation, *, symbol, quantity, fill_price, candidate=None,
               broker_order_id=None, position_state="OPEN"):
    """Build the notice from the watch evaluation that authorised the buy.

    Taken from the evaluation rather than re-derived, so the message
    reports the decision that was actually made.
    """
    row = dict(candidate or {})
    features = getattr(evaluation, "features", None)
    asof = None
    if features is not None and getattr(features, "market_data_asof", None):
        asof = features.market_data_asof.isoformat()
    return build(
        symbol=symbol, quantity=quantity, fill_price=fill_price,
        session=getattr(evaluation, "session", None),
        rank=row.get("rank"), score=row.get("score"),
        candidate_generated_at=row.get("generated_at"),
        market_data_asof=asof,
        entry_state=getattr(evaluation, "state", None),
        conditions=dict(getattr(evaluation, "conditions", {}) or {}),
        broker_order_id=broker_order_id, position_state=position_state)
