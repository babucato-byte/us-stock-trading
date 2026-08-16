"""Feature calculation and data-quality gating (Phase 2 instructions,
sections 4/6, hardened per CODEX-010).

Every number — both raw snapshot fields and every derived value — goes
through numeric_guard.require_finite_number() before being used further.
A plain `value < MIN_PRICE`-style comparison is not sufficient: NaN
compares False against every threshold in both directions, so it silently
survives naive range checks. Only require_finite_number()'s explicit
math.isnan()/math.isinf() test can be trusted to catch it.

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

from .models import NOT_AVAILABLE, NOT_EVALUATED
from .numeric_guard import InvalidNumber, require_finite_number

# A gap this large is far more likely a bad print, a split, or a data glitch
# than a real tradable gap; treated as a data-quality rejection, not a
# business-rule rejection (distinct from MIN/MAX_GAP_PERCENT in config).
GAP_SANITY_LIMIT_PERCENT = 500.0

_FEATURE_KEYS = (
    "latest_price", "previous_close", "gap_percent", "current_volume",
    "average_volume", "relative_volume", "average_dollar_volume", "atr",
    "atr_percent", "liquidity_score", "premarket_volume", "premarket_coverage_complete",
)


def compute_features(snapshot):
    """Returns (features: dict, rejection_reasons: list[str]).

    features always has all keys; a key is NOT_AVAILABLE when its own
    validation failed, or when it could not be derived because an input it
    depends on was already invalid. Multiple rejection reasons can apply —
    every check runs regardless of earlier failures, so operators see the
    full picture (Phase 2 instructions section 6).
    """
    reasons = []
    features = {key: NOT_AVAILABLE for key in _FEATURE_KEYS}
    features["premarket_coverage_complete"] = NOT_EVALUATED  # boolean metadata, not a numeric-unavailable field

    if snapshot is None:
        reasons.append("DATA_UNAVAILABLE: provider returned no snapshot")
        return features, reasons

    if snapshot.data_is_stale:
        reasons.append("STALE_DATA: snapshot flagged stale by provider")
        return features, reasons

    price = _validate(snapshot.price, "latest_price", reasons, min_value=0, min_exclusive=True)
    if price is not None:
        features["latest_price"] = price

    previous_close = _validate(snapshot.previous_close, "previous_close", reasons, min_value=0, min_exclusive=True)
    if previous_close is not None:
        features["previous_close"] = previous_close

    if price is not None and previous_close is not None:
        raw_gap = (price - previous_close) / previous_close * 100
        gap_percent = _validate(raw_gap, "gap_percent", reasons)
        if gap_percent is not None:
            if abs(gap_percent) > GAP_SANITY_LIMIT_PERCENT:
                reasons.append(f"DATA_ANOMALY: gap_percent {gap_percent:.1f}% exceeds sanity limit")
            else:
                features["gap_percent"] = gap_percent

    current_volume = _validate(snapshot.current_volume, "current_volume", reasons, min_value=0)
    if current_volume is not None:
        features["current_volume"] = current_volume

    average_volume = _validate(snapshot.average_volume, "average_volume", reasons, min_value=0, allow_zero=False)
    if average_volume is not None:
        features["average_volume"] = average_volume

    if price is not None and average_volume is not None:
        dollar_volume = _validate(price * average_volume, "average_dollar_volume", reasons, min_value=0)
        if dollar_volume is not None:
            features["average_dollar_volume"] = dollar_volume
            liquidity_score = _validate(min(100.0, dollar_volume / 1_000_000), "liquidity_score", reasons, min_value=0)
            if liquidity_score is not None:
                features["liquidity_score"] = liquidity_score

    if current_volume is not None and average_volume is not None:
        relative_volume = _validate(current_volume / average_volume, "relative_volume", reasons, min_value=0)
        if relative_volume is not None:
            features["relative_volume"] = relative_volume

    atr = _validate(snapshot.atr, "atr", reasons, min_value=0)
    if atr is not None:
        features["atr"] = atr
        if price is not None:
            atr_percent = _validate(atr / price * 100, "atr_percent", reasons, min_value=0)
            if atr_percent is not None:
                features["atr_percent"] = atr_percent

    if snapshot.premarket_volume is not None:
        premarket_volume = _validate(snapshot.premarket_volume, "premarket_volume", reasons, min_value=0)
        if premarket_volume is not None:
            features["premarket_volume"] = premarket_volume
            # CODEX-015: never call a partial-session read "the" premarket
            # volume without saying so — this flag distinguishes a full
            # 04:00-09:30 ET read from a provider that only had part of it.
            features["premarket_coverage_complete"] = bool(snapshot.premarket_coverage_complete)

    return features, reasons


def _validate(value, field_name, reasons, **kwargs):
    try:
        return require_finite_number(value, field_name=field_name, **kwargs)
    except InvalidNumber as exc:
        reasons.append(f"{exc.reason_code}: {exc}")
        return None
