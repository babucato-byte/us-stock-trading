"""Data-freshness gate (CODEX-011).

The provider (data_provider.py) only reports `data_as_of` (when the bar
actually happened) and `provider_fetched_at` (when the request ran) —
this module is where the pipeline decides whether the gap between
`data_as_of` and "now" (the pipeline's own evaluation time, not the
provider's fetch time — see the module docstring in data_provider.py for
why conflating the two is exactly the bug this closes) is acceptable for
the current trading session.
"""

from datetime import timedelta

STALE_REASON = "STALE_MARKET_DATA"
NAIVE_REASON = "NAIVE_TIMESTAMP"
MISSING_REASON = "MISSING_DATA_TIMESTAMP"
FUTURE_REASON = "FUTURE_TIMESTAMP"

# Tolerance for clock skew between this process and the data provider
# before a data_as_of "in the future" is treated as a hard error rather
# than rounding noise.
FUTURE_TOLERANCE_SECONDS = 60

_SESSION_TO_MAX_AGE_ATTR = {
    "premarket": "MAX_PREMARKET_DATA_AGE_MINUTES",
    "regular": "MAX_REGULAR_DATA_AGE_MINUTES",
    "aftermarket": "MAX_AFTER_HOURS_DATA_AGE_MINUTES",
}


def check_data_freshness(data_as_of, evaluated_at, session, cfg):
    """Returns a list of rejection reasons (empty = fresh enough).

    Both `data_as_of` and `evaluated_at` must be timezone-aware; a naive
    datetime is rejected outright rather than assumed to be any particular
    zone (Phase 2 instructions, section 11: "naive datetime은 허용하지
    않습니다").
    """
    if data_as_of is None:
        return [MISSING_REASON]

    if data_as_of.tzinfo is None or evaluated_at.tzinfo is None:
        return [NAIVE_REASON]

    if data_as_of > evaluated_at + timedelta(seconds=FUTURE_TOLERANCE_SECONDS):
        return [FUTURE_REASON]

    max_age_attr = _SESSION_TO_MAX_AGE_ATTR.get(session, "MAX_REGULAR_DATA_AGE_MINUTES")
    max_age_minutes = getattr(cfg, max_age_attr)

    age_minutes = (evaluated_at - data_as_of).total_seconds() / 60.0
    if age_minutes > max_age_minutes:
        return [f"{STALE_REASON}: data is {age_minutes:.1f} minutes old, max allowed is {max_age_minutes}"]

    return []
