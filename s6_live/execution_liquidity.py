"""Is this symbol tradeable, separately from whether the strategy likes it?

The trade this exists for
--------------------------
RIG, 2026-09-10 PREMARKET. Every S6 strategy condition held: price 5.79
above VWAP 5.77, EMA9 above EMA21, ORB5 breakout 5.76/5.71 confirmed,
volume expansion 4.50x over the opening range. Nothing about that reading
was false. What it could not see is that RIG's opening range was built
from 9 non-zero one-minute bars across 39 minutes of premarket -- most of
the session printed nothing at all -- and within two evaluation ticks of
the fill, recent 5-minute volume fell to 3 shares and stayed there for
most of an hour (production shadow-signal log,
2026-09-10T08:39-09:26Z). Volume EXPANSION is a ratio against this
symbol's own thin baseline; it says nothing about whether that baseline
could absorb an exit.

This module asks the second, separate question. `entry_quality.py`
already computes the measurements it needs -- `bar_count`,
`recent_volume_5m/10m/15m/30m`, `dollar_volume_5m` -- as a byproduct of
the strategy's own freshness check, so nothing here costs a new market
data call. It reads that same snapshot and answers a different question
with it: can this be entered and exited, not just entered.

Provisional, on evidence
------------------------
Every numeric floor here is PROVISIONAL, calibrated against exactly two
trading days of production `entry_quality` history (2026-09-09,
2026-09-10 -- the shadow-signal log did not exist before then) and a
handful of symbols. That is not enough to set precise thresholds with
confidence, so every default is set LOOSE deliberately: low enough that
it would not have blocked KVYO (recent_volume_5m as low as 170 shares,
dollar_volume_5m as low as $2,794, both filled and closed without
incident) while still catching genuinely dead readings (RIG's own
post-entry collapse to 3 shares / $17.37 over 5 minutes; COP's early
premarket ticks as low as 1 share / $137.70). See
docs in the release report for the full comparison. Revisit these
constants once more sessions accumulate.

What this does NOT catch
-------------------------
RIG's own BUY decision is not rejected by this gate: at the exact minute
the order was submitted (08:39-08:40 UTC), recent_volume_5m read 3,664
shares and dollar_volume_5m read $21,250 -- a healthy-looking snapshot
that collapsed only after entry. A single point-in-time snapshot cannot
see a collapse that has not happened yet. What it can and does limit is
how much capital is ever put at risk against a thin reading: the sizing
cap (`liquidity_capped_qty`) bounds every order to a fraction of recently
traded volume, which holds regardless of timing.

Spread and quote staleness
---------------------------
KIS's price-detail response (`brokers.kis_broker.get_price_detail`,
PRICE_DETAIL_FIELDS) carries no bid/ask field; no bid-ask source exists
anywhere in this codebase for any session. SPREAD_TOO_WIDE and
QUOTE_STALE stay in the reason vocabulary for when that changes, but
their thresholds are never configured here -- see the module's audit
report for the full per-session data-availability table. Configuring a
gate around data production cannot obtain would fail exactly the way
`entry_quality.spread_bps` already does: permanently UNAVAILABLE,
indistinguishable from thin.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# -- reason codes (stable, English, internal) --------------------------------
LIQUIDITY_OK = "LIQUIDITY_OK"
ABSOLUTE_LIQUIDITY_TOO_LOW = "ABSOLUTE_LIQUIDITY_TOO_LOW"
DOLLAR_VOLUME_TOO_LOW = "DOLLAR_VOLUME_TOO_LOW"
NO_RECENT_TRADES = "NO_RECENT_TRADES"
TOO_MANY_ZERO_VOLUME_BARS = "TOO_MANY_ZERO_VOLUME_BARS"
SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
QUOTE_STALE = "QUOTE_STALE"
ORDER_TOO_LARGE_FOR_LIQUIDITY = "ORDER_TOO_LARGE_FOR_LIQUIDITY"
LIQUIDITY_DATA_UNAVAILABLE = "LIQUIDITY_DATA_UNAVAILABLE"

REASON_CODES = (
    LIQUIDITY_OK, ABSOLUTE_LIQUIDITY_TOO_LOW, DOLLAR_VOLUME_TOO_LOW,
    NO_RECENT_TRADES, TOO_MANY_ZERO_VOLUME_BARS, SPREAD_TOO_WIDE,
    QUOTE_STALE, ORDER_TOO_LARGE_FOR_LIQUIDITY, LIQUIDITY_DATA_UNAVAILABLE,
)

PASS = "PASS"
FAIL = "FAIL"
UNAVAILABLE = "UNAVAILABLE"

#: The window `min_recent_volume`/`min_dollar_volume` are read from.
DEFAULT_VOLUME_WINDOW_MINUTES = 5

#: PROVISIONAL defaults -- see module docstring for the evidence they are
#: calibrated against. `thresholds_for` lets a session override any of
#: these from `scanners/orb/config.json`; these are what applies absent
#: that override.
DEFAULT_THRESHOLDS: Dict[str, Any] = {
    # Below this, there is not enough recorded trading to call the
    # measurement real rather than a single stale print.
    "min_bar_count": 3,
    # Shares in the trailing DEFAULT_VOLUME_WINDOW_MINUTES window.
    "min_recent_volume": 20.0,
    # Dollars in the same window.
    "min_dollar_volume": 100.0,
    # A proposed order may not exceed this fraction of the trailing
    # 15-minute volume. Applied by `liquidity_capped_qty`, not `assess`.
    "max_qty_fraction_of_recent_volume": 0.10,
    # Left unset deliberately -- see "Spread and quote staleness" above.
    "max_spread_bps": None,
}


@dataclass(frozen=True)
class LiquidityDecision:
    verdict: str
    reason_code: Optional[str]
    detail: Dict[str, Any]


def thresholds_for(config, session) -> Dict[str, Any]:
    """The `execution_liquidity` block for one session, falling back to
    `DEFAULT_THRESHOLDS`. Shape in scanners/orb/config.json:

        "execution_liquidity": {"PREMARKET": {"min_bar_count": 5, ...},
                                "default": {}}

    Any key a session (or "default") does not set keeps the module
    default for that key -- a session block only overrides what it
    names.
    """
    merged = dict(DEFAULT_THRESHOLDS)
    try:
        block = config.get("execution_liquidity") if hasattr(config, "get") else None
    except Exception:  # noqa: BLE001
        block = None
    if isinstance(block, dict):
        key = str(session or "").strip().upper()
        chosen = block.get(key)
        if not isinstance(chosen, dict):
            chosen = block.get("default")
        if isinstance(chosen, dict):
            merged.update({k: v for k, v in chosen.items() if k in DEFAULT_THRESHOLDS})
    return merged


def _recent_volume(quality, minutes: int) -> Optional[float]:
    return getattr(quality, f"recent_volume_{int(minutes)}m", None)


def _dollar_volume(quality, minutes: int) -> Optional[float]:
    if minutes == 5:
        return getattr(quality, "dollar_volume_5m", None)
    return None


def assess(quality, thresholds: Optional[Dict[str, Any]] = None) -> Tuple[str, Optional[str], Dict[str, Any]]:
    """(verdict, reason_code, detail) for one candidate's tradeability.

    `quality` is `s6_live.entry_quality.EntryQuality` (or None): the same
    snapshot the strategy's own freshness check already computed for this
    tick, reused rather than recomputed. PASS/FAIL/UNAVAILABLE mirrors
    `entry_quality.assess`'s vocabulary; UNAVAILABLE is the safe reading
    of "we could not measure this", never a silent pass.
    """
    active = dict(thresholds or DEFAULT_THRESHOLDS)
    window = DEFAULT_VOLUME_WINDOW_MINUTES
    detail: Dict[str, Any] = {"thresholds": active, "window_minutes": window}

    if quality is None:
        # Fails open, deliberately: a candidate whose SessionFeatures
        # never carried an entry_quality snapshot at all (an evaluation
        # path that never wired one in, not a market with no trades --
        # see the module docstring's "fails open" note) is not evidence
        # of illiquidity. `entry_quality` measured but a SPECIFIC field
        # missing (below) is a different, real signal and stays
        # UNAVAILABLE.
        detail["missing"] = "entry quality not computed"
        return PASS, None, detail

    bar_count = getattr(quality, "bar_count", 0) or 0
    detail["bar_count"] = bar_count
    min_bar_count = active.get("min_bar_count")
    if min_bar_count is not None and bar_count < min_bar_count:
        detail["failed"] = "min_bar_count"
        detail["limit"] = min_bar_count
        return FAIL, NO_RECENT_TRADES, detail

    recent_volume = _recent_volume(quality, window)
    dollar_volume = _dollar_volume(quality, window)
    detail["recent_volume"] = recent_volume
    detail["dollar_volume"] = dollar_volume

    min_recent_volume = active.get("min_recent_volume")
    if min_recent_volume is not None:
        if recent_volume is None:
            detail["missing"] = f"recent_volume_{window}m"
            return UNAVAILABLE, LIQUIDITY_DATA_UNAVAILABLE, detail
        if recent_volume < min_recent_volume:
            detail["failed"] = "min_recent_volume"
            detail["limit"] = min_recent_volume
            return FAIL, ABSOLUTE_LIQUIDITY_TOO_LOW, detail

    min_dollar_volume = active.get("min_dollar_volume")
    if min_dollar_volume is not None:
        if dollar_volume is None:
            detail["missing"] = "dollar_volume_5m"
            return UNAVAILABLE, LIQUIDITY_DATA_UNAVAILABLE, detail
        if dollar_volume < min_dollar_volume:
            detail["failed"] = "min_dollar_volume"
            detail["limit"] = min_dollar_volume
            return FAIL, DOLLAR_VOLUME_TOO_LOW, detail

    max_spread_bps = active.get("max_spread_bps")
    spread_bps = getattr(quality, "spread_bps", None)
    detail["spread_bps"] = spread_bps
    if max_spread_bps is not None:
        # Never satisfied in production today -- see the module
        # docstring. Kept so a future quote source only has to supply
        # `spread_bps` and set this threshold, not add a new code path.
        if spread_bps is None:
            detail["missing"] = "spread_bps"
            return UNAVAILABLE, LIQUIDITY_DATA_UNAVAILABLE, detail
        if spread_bps > max_spread_bps:
            detail["failed"] = "max_spread_bps"
            detail["limit"] = max_spread_bps
            return FAIL, SPREAD_TOO_WIDE, detail

    return PASS, None, detail


def liquidity_capped_qty(quality, requested_qty: int,
                         thresholds: Optional[Dict[str, Any]] = None
                         ) -> Tuple[int, Optional[str], Dict[str, Any]]:
    """(capped_qty, reason_code_if_zero, detail).

    Caps `requested_qty` to `max_qty_fraction_of_recent_volume` of the
    trailing 15-minute volume -- a standing market-impact bound, applied
    regardless of what `assess` decided, since a candidate can pass every
    liquidity floor and still be oversized for what actually traded.
    Never raises `requested_qty` and never returns a fractional share
    (floored, project-wide no-fractional-shares rule -- see
    `domain.cash_sizing`). A candidate whose recent volume cannot even
    support ONE share is blocked outright, not silently sized to zero
    and forgotten: the caller must treat qty==0 as BUY_INTENT-blocking,
    not as "size later".
    """
    active = dict(thresholds or DEFAULT_THRESHOLDS)
    fraction = active.get("max_qty_fraction_of_recent_volume")
    detail: Dict[str, Any] = {"requested_qty": requested_qty, "fraction": fraction}
    if fraction is None or requested_qty is None or requested_qty <= 0:
        return requested_qty or 0, None, detail
    if quality is None:
        # Same fail-open reasoning as `assess()`: no snapshot at all is
        # not "measured and oversized". The cash/risk/broker checks
        # downstream remain the authority regardless (see
        # s6_live.cash_precheck's module docstring for the same
        # principle applied to the cash side).
        detail["missing"] = "entry quality not computed"
        return int(requested_qty), None, detail
    recent_volume_15m = getattr(quality, "recent_volume_15m", None)
    detail["recent_volume_15m"] = recent_volume_15m
    if recent_volume_15m is None:
        detail["missing"] = "recent_volume_15m"
        return 0, LIQUIDITY_DATA_UNAVAILABLE, detail
    cap = int(recent_volume_15m * fraction)
    detail["cap"] = cap
    if cap < 1:
        detail["failed"] = "max_qty_fraction_of_recent_volume"
        return 0, ORDER_TOO_LARGE_FOR_LIQUIDITY, detail
    capped = min(int(requested_qty), cap)
    if capped < 1:
        detail["failed"] = "max_qty_fraction_of_recent_volume"
        return 0, ORDER_TOO_LARGE_FOR_LIQUIDITY, detail
    return capped, None, detail
