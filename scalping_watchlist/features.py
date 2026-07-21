"""Feature calculation and data-quality gating (Phase 2 instructions,
sections 4/6). Turns a raw SymbolSnapshot into either a dict of computed
features, or a rejection reason — never a fabricated number.

Formulas (all newly written for Phase 2 — no equivalent existed in the
repo per DECISION_LOG.md's reuse-scope entry):

- gap_percent      = (price - previous_close) / previous_close * 100
- relative_volume   = current_volume / average_volume
- average_dollar_volume = price * average_volume
- atr_percent       = atr / price * 100
- liquidity_score   = min(100, average_dollar_volume / 1,000,000), i.e. 100
  at $100M/day average dollar volume or more. A simple, explainable proxy
  for "how easy is this to trade in and out of" — not a spread estimate;
  see DECISION_LOG.md for why no real spread source is used.
"""

from .models import NOT_AVAILABLE

# A gap this large is far more likely a bad print, a split, or a data glitch
# than a real tradable gap; treated as a data-quality rejection, not a
# business-rule rejection (distinct from MIN/MAX_GAP_PERCENT in config).
GAP_SANITY_LIMIT_PERCENT = 500.0


def compute_features(snapshot):
    """Returns (features: dict, rejection_reasons: list[str]).

    features always has all keys; a key is NOT_AVAILABLE when data-quality
    checks failed early enough that a downstream number can't be trusted.
    Multiple rejection reasons can apply — none of them are mutually
    exclusive short-circuits, so operators see the full picture.
    """
    reasons = []
    features = {
        "latest_price": NOT_AVAILABLE,
        "previous_close": NOT_AVAILABLE,
        "gap_percent": NOT_AVAILABLE,
        "current_volume": NOT_AVAILABLE,
        "average_volume": NOT_AVAILABLE,
        "relative_volume": NOT_AVAILABLE,
        "average_dollar_volume": NOT_AVAILABLE,
        "atr": NOT_AVAILABLE,
        "atr_percent": NOT_AVAILABLE,
        "liquidity_score": NOT_AVAILABLE,
        "premarket_volume": NOT_AVAILABLE,
    }

    if snapshot is None:
        reasons.append("DATA_UNAVAILABLE: provider returned no snapshot")
        return features, reasons

    if snapshot.data_is_stale:
        reasons.append("STALE_DATA: snapshot flagged stale by provider")
        return features, reasons

    price = snapshot.price
    previous_close = snapshot.previous_close
    current_volume = snapshot.current_volume
    average_volume = snapshot.average_volume
    atr = snapshot.atr

    if price is None or price <= 0:
        reasons.append("INVALID_PRICE: price is missing, zero, or negative")
        return features, reasons
    features["latest_price"] = price

    if previous_close is None or previous_close <= 0:
        reasons.append("INVALID_PREVIOUS_CLOSE: previous_close is missing, zero, or negative")
    else:
        features["previous_close"] = previous_close
        gap_percent = (price - previous_close) / previous_close * 100
        if abs(gap_percent) > GAP_SANITY_LIMIT_PERCENT:
            reasons.append(f"DATA_ANOMALY: gap_percent {gap_percent:.1f}% exceeds sanity limit")
        else:
            features["gap_percent"] = gap_percent

    if current_volume is None or current_volume < 0:
        reasons.append("CORRUPTED_VOLUME: current_volume is missing or negative")
    else:
        features["current_volume"] = current_volume

    if average_volume is None or average_volume <= 0:
        reasons.append("AVERAGE_VOLUME_UNAVAILABLE: cannot compute a positive average volume")
    else:
        features["average_volume"] = average_volume
        features["average_dollar_volume"] = price * average_volume
        features["liquidity_score"] = min(100.0, (price * average_volume) / 1_000_000)
        if current_volume is not None and current_volume >= 0:
            features["relative_volume"] = current_volume / average_volume

    if atr is None or atr < 0:
        reasons.append("ATR_UNAVAILABLE: cannot compute a valid ATR")
    else:
        features["atr"] = atr
        features["atr_percent"] = atr / price * 100

    if snapshot.premarket_volume is not None:
        features["premarket_volume"] = snapshot.premarket_volume

    return features, reasons
