"""One-minute bars from KIS, including the hours yfinance cannot see.

Why this exists
---------------
S6's premarket scan on 2026-08-31 read: universe 83, DATA_ERROR 77,
evaluated 6, signals 0. Not a universe problem and not a quiet market --
the provider simply has no usable premarket intraday data, so 93% of the
candidates died before any strategy rule was applied.

KIS does have it. Measured against the live account at 05:02 ET:

    inquire-time-itemchartprice (HHDFS76950200) -> rt_cd=0, 120 bars
    {"xymd":"20260831","xhms":"050200","open":"319.4100",
     "high":"319.5300","low":"319.4100","last":"319.4150","evol":"79"}

Real premarket bars with real premarket volume.

Three things the wire format will do to you
-------------------------------------------
NEWEST FIRST. `output2[0]` is the most recent minute. Treating it as
oldest-first silently reverses every series, which an EMA will happily
compute and no assertion will catch.

ONLY MINUTES THAT TRADED. A quiet premarket name yields bars minutes
apart. Those gaps are the market, not missing data, and calling them a
data gap would reject exactly the thin extended-hours names this is for.

IT CROSSES MIDNIGHT. 120 bars from 05:02 ET reach back into the previous
evening -- so the response carries more than one trading day, and
anything that does not filter by day will compute a session VWAP across
two sessions.

`evol` is the bar's own volume, not a cumulative counter. Summed, never
differenced.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")

CHART_PATH = "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice"
TR_ID_CHART = "HHDFS76950200"

#: Bars per call, measured. The endpoint caps here regardless of NREC.
BARS_PER_CALL = 120

#: Measured cost per symbol against the live account: ~2.44s, almost all
#: of it the shared KIS rate limiter's pacing rather than the network
#: (the first call in a burst returned in 246ms). Forty-one symbols is
#: therefore ~100s of limiter time, which is why a warmup backfill runs
#: ONCE when a symbol takes a slot and not on every cycle.
MEASURED_SECONDS_PER_SYMBOL = 2.44

SOURCE = "KIS_REST_CHART"


def _bar_time(row) -> Optional[datetime]:
    """`xymd` + `xhms` as an Eastern-aware datetime.

    The KST pair (`kymd`/`khms`) is deliberately ignored: the session
    boundaries this feeds are Eastern, and converting back and forth adds
    a DST bug for no gain.
    """
    day = str(row.get("xymd") or "").strip()
    clock = str(row.get("xhms") or "").strip().zfill(6)
    if len(day) != 8 or len(clock) != 6:
        return None
    try:
        return datetime(int(day[0:4]), int(day[4:6]), int(day[6:8]),
                        int(clock[0:2]), int(clock[2:4]), int(clock[4:6]),
                        tzinfo=EASTERN)
    except ValueError:
        return None


def _number(value) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_rows(rows, *, trading_day=None) -> List[Dict[str, Any]]:
    """Wire rows to oldest-first bars, optionally for one day only.

    `trading_day` is the Eastern calendar date the bars must belong to.
    Filtering here rather than in the caller is deliberate: the response
    reaches back across midnight, and a session VWAP computed over two
    days is wrong in a way that looks entirely reasonable.
    """
    parsed = []
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        at = _bar_time(row)
        if at is None:
            continue
        if trading_day and at.date().isoformat() != str(trading_day):
            continue
        close = _number(row.get("last"))
        if close is None:
            continue
        parsed.append({
            "at": at,
            "open": _number(row.get("open")) or close,
            "high": _number(row.get("high")) or close,
            "low": _number(row.get("low")) or close,
            "close": close,
            # Per-bar volume, not a running total.
            "volume": _number(row.get("evol")) or 0.0,
            "amount": _number(row.get("eamt")),
            "source": SOURCE,
        })
    # Oldest first. The wire is newest-first and every downstream
    # indicator assumes the opposite.
    parsed.sort(key=lambda b: b["at"])
    return parsed


def fetch(broker, *, symbol, exchange, trading_day=None,
          bars=BARS_PER_CALL) -> List[Dict[str, Any]]:
    """One symbol's recent minute bars. Read-only; never raises.

    Returns [] on anything unusable, because a warmup that cannot be
    filled is a candidate that stays WARMING_UP -- not a scan that dies.
    """
    from brokers.kis_broker import _excd_for

    try:
        broker.config.validate_read_allowed()
        excd = _excd_for(exchange)
        body = broker._get(CHART_PATH, TR_ID_CHART, {
            "AUTH": "", "EXCD": excd, "SYMB": str(symbol).upper(),
            "NMIN": "1", "PINC": "1", "NEXT": "", "NREC": str(int(bars)),
            "FILL": "", "KEYB": "",
        })
    except Exception:  # noqa: BLE001
        logger.warning("KIS minute chart unavailable for %s", symbol,
                       exc_info=True)
        return []
    if str(body.get("rt_cd")) != "0":
        logger.warning("KIS minute chart refused %s: %s", symbol,
                       str(body.get("msg1"))[:120])
        return []
    return parse_rows(body.get("output2"), trading_day=trading_day)


def to_bars(records, *, symbol, session):
    """Chart records as `realtime_bars.Bar`, so the merge and the warmup
    see one shape whatever produced it."""
    from market_data.realtime_bars import Bar

    out = []
    for record in records or ():
        at = record["at"]
        minute = at.astimezone(timezone.utc).replace(second=0, microsecond=0)
        out.append(Bar(
            symbol=str(symbol).upper(), session=session, minute=minute,
            open=record["open"], high=record["high"], low=record["low"],
            close=record["close"], volume=record["volume"],
            trade_count=0, first_trade_at=at, last_trade_at=at,
            source=SOURCE))
    return out
