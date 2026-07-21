"""Trading-day and allowed-session gating for the pipeline entry point
(CODEX-012).

Reuses market_guard.is_us_trading_day(now=...) (the same NYSE calendar
via pandas_market_calendars the existing scanner uses) rather than a
parallel holiday calendar — market_guard.py gained an optional `now`
parameter for this purpose, backward compatible with all its existing
zero-arg callers (daily_candidate_scanner.py, run_premarket.py, etc.).

Session policy (documented in DECISION_LOG.md): Phase 2's charter
(instructions section 1) is explicitly premarket + early regular session
scanning, not the full trading day — the low-frequency full-day scan is
daily_candidate_scanner.py's job already. Only PREMARKET and a narrow
REGULAR_OPEN_WINDOW_MINUTES-wide window at the regular-session open are
allowed; after-hours and the rest of the regular session are not.
"""

from datetime import timedelta

from market_guard import is_us_trading_day

MARKET_CLOSED = "MARKET_CLOSED"
SESSION_NOT_ALLOWED = "SESSION_NOT_ALLOWED"


def check_pipeline_allowed(now_et, session, cfg):
    """Returns None if the pipeline may run, or a reason string
    (MARKET_CLOSED / SESSION_NOT_ALLOWED) if it must not."""
    if not is_us_trading_day(now_et):
        return MARKET_CLOSED

    if session not in cfg.ALLOWED_SESSIONS:
        return SESSION_NOT_ALLOWED

    if session == "regular":
        market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        window_end = market_open + timedelta(minutes=cfg.REGULAR_OPEN_WINDOW_MINUTES)
        if not (market_open <= now_et < window_end):
            return SESSION_NOT_ALLOWED

    return None
