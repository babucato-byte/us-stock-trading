"""What happened after each signal (spec sections 12 and 13).

This measures the SCANNER, not the trade
----------------------------------------
Section 14 insists these two never be added together, and this module is
the reason that separation is possible. Every number here is measured
from `signal_price` -- the price at the moment the scanner said "look at
this" -- with no entry rule, no stop, no position size and no exit.

That is what makes the diagnosis in section 14 work. A scanner whose
signals routinely run +10% while the account loses money has an entry
problem. One whose signals show +2% MFE against -10% MAE has a candidate
quality problem, and no entry rule will save it. Blending the two into a
single P&L makes both invisible.

MFE and MAE: what window, exactly
---------------------------------
"Maximum favourable/adverse excursion over 1 day" needs a start and an
end, and the obvious readings are wrong in opposite directions.

Starting at the signal DAY's open would credit the scanner with a move
that happened before it spoke. Starting at the next day's open would
discard the rest of the session an intraday scanner was specifically
built to catch -- the ORB scanner's whole thesis plays out in the hours
after its signal.

So the window runs from the SIGNAL TIMESTAMP to the close of the Nth
session after the signal day, and it is assembled from two pieces:

    [signal timestamp .. signal-day close]   from intraday bars
    [day+1 .. day+N]                         from daily high/low

The intraday piece is only available while the provider still serves
minute bars for that date (about a week). After that the record already
computed for those horizons stands, because performance records are
appended and the newest wins -- so running the tracker daily preserves
the intraday-accurate figures, and a late backfill cannot silently
overwrite them with a coarser measurement. `includes_signal_day_intraday`
records which kind of measurement each row got.

Partial horizons are null, not zero
-----------------------------------
A signal from yesterday has no 5-day return. It is recorded as None, and
`horizons_complete` lists what was actually measurable. Filling it with
0.0 would drag every 5-day average toward zero by exactly the count of
recent signals, which would make the newest week of any report look
worse than it was.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from market_hours import EASTERN
from scanners.base import indicators as ind
from scanners.base import result_store
from scanners.base import session as sess
from scanners.base.market_data_provider import (
    BarMarketDataProvider,
    MarketDataUnavailable,
    default_provider,
)
from scanners.base.models import ScannerSignal

logger = logging.getLogger(__name__)

#: Intraday horizons, in minutes (section 12).
INTRADAY_HORIZONS = (("return_30m", 30), ("return_1h", 60), ("return_2h", 120))

#: Multi-day horizons, in trading sessions (sections 12 and 13).
DAY_HORIZONS = (1, 3, 5)

#: How many calendar days of daily bars to pull when following a signal
#: forward. 5 trading sessions can span a long weekend plus a holiday,
#: so this is deliberately generous -- an under-fetch would silently
#: report a 5-day return as unavailable.
FORWARD_LOOKBACK_DAYS = 30

#: Calendar days after which minute bars are no longer served. Yahoo
#: Finance keeps roughly 8; 7 is used so a horizon is called EXPIRED
#: only once it is genuinely unrecoverable, never merely late.
INTRADAY_RETENTION_DAYS = 7

# --- horizon status vocabulary (spec section 23) ---
#: Measured.
COMPLETE = "complete"
#: Not yet -- the horizon has not elapsed. Will fill in on a later run.
PENDING = "pending"
#: The window has elapsed but the bars needed are gone for good. A
#: signal-day intraday horizon that was never computed inside the
#: minute-bar retention window can never be computed now, and saying so
#: is the difference between "we are still waiting" and "this row will
#: stay empty forever".
EXPIRED = "expired"
#: The bars should exist but the provider did not serve them. Unlike
#: EXPIRED this may succeed on a retry, which is a different operator
#: action.
DATA_UNAVAILABLE = "data_unavailable"

#: Roll-up of the per-horizon statuses onto the record as a whole.
PARTIAL = "partial"


def _percent(from_price, to_price) -> Optional[float]:
    base = ind.to_float(from_price)
    target = ind.to_float(to_price)
    if base is None or target is None or base == 0:
        return None
    return ind.to_float((target / base - 1.0) * 100.0)


def _eastern_index(frame) -> Optional[pd.DataFrame]:
    if frame is None or len(frame) == 0:
        return None
    if not isinstance(frame.index, pd.DatetimeIndex):
        return frame
    return sess.to_eastern(frame)


def _parse_timestamp(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        stamp = value
    else:
        try:
            stamp = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _sessions_after(daily, trading_day: str) -> List[Dict[str, Any]]:
    """Daily bars for the sessions strictly after `trading_day`, in order.

    "Strictly after" matters: the signal day's own daily bar contains
    the whole session including the part BEFORE the signal, and counting
    its high as favourable excursion would credit a scanner with a move
    it did not call.
    """
    if daily is None or len(daily) == 0:
        return []
    if not isinstance(daily.index, pd.DatetimeIndex):
        return []
    frame = sess.to_eastern(daily)
    try:
        cutoff = datetime.fromisoformat(str(trading_day)).date()
    except (TypeError, ValueError):
        return []
    sessions = []
    for stamp, row in frame.iterrows():
        if stamp.date() <= cutoff:
            continue
        sessions.append({
            "date": stamp.date(),
            "high": ind.to_float(row.get("High", row.get("high"))),
            "low": ind.to_float(row.get("Low", row.get("low"))),
            "close": ind.to_float(row.get("Close", row.get("close"))),
        })
    return sessions


def _signal_day_tail(intraday, trading_day: str, signal_time: Optional[datetime]):
    """The signal day's bars at or after the signal timestamp.

    Returns None when minute bars for that date are not (or no longer)
    available, which the caller records rather than papering over.
    """
    frame = _eastern_index(intraday)
    if frame is None or not isinstance(frame.index, pd.DatetimeIndex):
        return None
    try:
        target = datetime.fromisoformat(str(trading_day)).date()
    except (TypeError, ValueError):
        return None
    same_day = frame[[stamp.date() == target for stamp in frame.index]]
    if len(same_day) == 0:
        return None
    if signal_time is None:
        return same_day
    local = signal_time.astimezone(EASTERN)
    tail = same_day[same_day.index >= local]
    # A signal stamped after the last available bar (a scan that ran
    # post-close) leaves nothing at or after it. That is not an error;
    # it means there is no signal-day intraday window to measure.
    return tail if len(tail) else None


def _bar_at_or_after(frame, moment: datetime):
    if frame is None or len(frame) == 0:
        return None
    later = frame[frame.index >= moment]
    if len(later) == 0:
        return None
    closes = ind.close_series(later).dropna()
    return ind.to_float(closes.iloc[0]) if len(closes) else None


def compute_performance(
    signal: ScannerSignal,
    *,
    daily=None,
    intraday=None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Forward returns and excursions for one signal.

    Pure: it reads the frames it is handed and never fetches. Every
    branch -- no forward sessions yet, no intraday bars, a zero signal
    price -- is therefore reachable from a unit test with hand-built
    frames.
    """
    now = now or datetime.now(timezone.utc)
    price = signal.signal_price
    record: Dict[str, Any] = {
        "signal_id": signal.signal_id,
        "trading_day": signal.trading_day,
        "symbol": signal.symbol,
        "scanner_name": signal.scanner_name,
        "scanner_version": signal.scanner_version,
        "signal_price": price,
        "computed_at": now.isoformat(),
        "includes_signal_day_intraday": False,
        "sessions_available": 0,
        "horizons_complete": [],
    }
    for name, _ in INTRADAY_HORIZONS:
        record[name] = None
    record["return_close"] = None
    for days in DAY_HORIZONS:
        record[f"return_{days}d"] = None
        record[f"mfe_{days}d"] = None
        record[f"mae_{days}d"] = None

    if price is None or price <= 0:
        # Without an anchor there is nothing to measure against, and a
        # return computed from a missing price would be a fabricated
        # number in the one dataset this project exists to trust.
        record["error"] = "signal has no usable signal_price"
        return record

    signal_time = _parse_timestamp(signal.timestamp)
    tail = _signal_day_tail(intraday, signal.trading_day, signal_time)
    intraday_expired = _intraday_window_expired(signal.trading_day, now)
    status: Dict[str, str] = {}

    if tail is not None and signal_time is not None:
        record["includes_signal_day_intraday"] = True
        local = signal_time.astimezone(EASTERN)
        for name, minutes in INTRADAY_HORIZONS:
            value = _bar_at_or_after(tail, local + timedelta(minutes=minutes))
            record[name] = _percent(price, value)
            if record[name] is not None:
                record["horizons_complete"].append(name)
                status[name] = COMPLETE
            elif (now - signal_time).total_seconds() < minutes * 60:
                # The horizon has not elapsed yet -- a scan at 15:50 has
                # no +2h return because 17:50 has not happened.
                status[name] = PENDING
            else:
                # Elapsed, and the bar is not in the frame: the session
                # ended before the horizon did.
                status[name] = COMPLETE if value is not None else EXPIRED
    else:
        # No minute bars at all for the signal day. Whether that is
        # recoverable depends entirely on age, and the two cases call
        # for different responses: retry, or accept and move on.
        intraday_status = EXPIRED if intraday_expired else DATA_UNAVAILABLE
        for name, _ in INTRADAY_HORIZONS:
            status[name] = intraday_status

    daily_frame = _eastern_index(daily)
    signal_day_close = _signal_day_close(daily_frame, signal.trading_day)
    if signal_day_close is None and tail is not None:
        closes = ind.close_series(tail).dropna()
        signal_day_close = ind.to_float(closes.iloc[-1]) if len(closes) else None
    record["return_close"] = _percent(price, signal_day_close)
    if record["return_close"] is not None:
        record["horizons_complete"].append("return_close")
        status["return_close"] = COMPLETE
    else:
        status["return_close"] = EXPIRED if intraday_expired else DATA_UNAVAILABLE

    sessions = _sessions_after(daily_frame, signal.trading_day)
    record["sessions_available"] = len(sessions)

    # The excursion window always starts at the signal, so the signal
    # day's post-signal extremes seed the running high/low before any
    # forward session is folded in.
    running_high: Optional[float] = None
    running_low: Optional[float] = None
    if tail is not None:
        highs = ind.high_series(tail).dropna()
        lows = ind.low_series(tail).dropna()
        if len(highs):
            running_high = ind.to_float(highs.max())
        if len(lows):
            running_low = ind.to_float(lows.min())

    for days in DAY_HORIZONS:
        window = sessions[:days]
        if len(window) < days:
            # Not enough sessions have elapsed. Left as None so the
            # averages in sections 15 and 16 are taken over the signals
            # that actually reached the horizon.
            #
            # Always PENDING, never EXPIRED: daily bars do not age out
            # the way minute bars do, so a multi-day horizon that has
            # not filled will fill, on whichever run happens after the
            # sessions elapse.
            status[f"return_{days}d"] = PENDING
            continue
        record[f"return_{days}d"] = _percent(price, window[-1]["close"])
        highs = [bar["high"] for bar in window if bar["high"] is not None]
        lows = [bar["low"] for bar in window if bar["low"] is not None]
        if running_high is not None:
            highs.append(running_high)
        if running_low is not None:
            lows.append(running_low)
        if highs:
            # MFE is clamped at 0: an "excursion in the favourable
            # direction" that never went favourable is zero, not a
            # negative favourable excursion. Same, mirrored, for MAE --
            # which keeps the section 16 MFE/MAE ratio from changing
            # sign and becoming uninterpretable.
            record[f"mfe_{days}d"] = max(0.0, _percent(price, max(highs)) or 0.0)
        if lows:
            record[f"mae_{days}d"] = min(0.0, _percent(price, min(lows)) or 0.0)
        record["horizons_complete"].append(f"return_{days}d")
        status[f"return_{days}d"] = COMPLETE

    record["horizon_status"] = status
    record["status"] = _roll_up_status(status)
    return record


def _intraday_window_expired(trading_day: str, now: datetime) -> bool:
    """Are this day's minute bars past the provider's retention window?"""
    try:
        day = datetime.fromisoformat(str(trading_day)).date()
    except (TypeError, ValueError):
        return False
    return (now.date() - day).days > INTRADAY_RETENTION_DAYS


def _roll_up_status(status: Dict[str, str]) -> str:
    """One word for the record as a whole.

    `complete` only when nothing is still owed -- i.e. no horizon is
    PENDING. A record with expired intraday horizons but every daily
    horizon measured is still `complete`, because nothing further will
    ever arrive for it and calling it `partial` forever would make the
    label useless for spotting rows that genuinely still need a run.
    """
    if not status:
        return DATA_UNAVAILABLE
    values = set(status.values())
    if values == {COMPLETE}:
        return COMPLETE
    if PENDING in values:
        return PARTIAL if COMPLETE in values else PENDING
    if COMPLETE in values:
        return COMPLETE
    if EXPIRED in values:
        return EXPIRED
    return DATA_UNAVAILABLE


def track_signals(
    signals: Iterable[ScannerSignal],
    *,
    provider: Optional[BarMarketDataProvider] = None,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Compute performance for each signal, fetching bars once per symbol.

    Section 6 keeps the same symbol under several scanners on purpose,
    so the same ticker commonly appears three or four times in a day's
    signals. The caching provider makes that one fetch, not four.
    """
    provider = provider or default_provider()
    records: List[Dict[str, Any]] = []
    for signal in signals:
        try:
            daily = provider.get_daily_bars(signal.symbol, lookback_days=FORWARD_LOOKBACK_DAYS)
        except MarketDataUnavailable as exc:
            logger.debug("no daily bars for %s: %s", signal.symbol, exc)
            daily = None
        intraday = None
        try:
            intraday = provider.get_intraday_bars(
                signal.symbol, interval="1m", lookback_days=7, include_prepost=False)
        except MarketDataUnavailable as exc:
            logger.debug("no intraday bars for %s: %s", signal.symbol, exc)
        try:
            records.append(compute_performance(
                signal, daily=daily, intraday=intraday, now=now))
        except Exception as exc:  # noqa: BLE001 - one signal must not end the run
            logger.exception("performance computation failed for %s", signal.signal_id)
            records.append({
                "signal_id": signal.signal_id,
                "trading_day": signal.trading_day,
                "symbol": signal.symbol,
                "scanner_name": signal.scanner_name,
                "scanner_version": signal.scanner_version,
                "error": f"{type(exc).__name__}: {exc}",
            })
    return records


def track_day(
    trading_day: str,
    *,
    provider: Optional[BarMarketDataProvider] = None,
    store: bool = True,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Compute and store performance for every signal of one trading day."""
    signals = result_store.read_signals(trading_day)
    if not signals:
        logger.info("no signals recorded for %s", trading_day)
        return []
    records = track_signals(signals, provider=provider, now=now)
    if store and records:
        result_store.write_performance(records, trading_day=trading_day)
    logger.info("computed performance for %s signals on %s", len(records), trading_day)
    return records


def track_recent(
    *,
    days: int = 10,
    provider: Optional[BarMarketDataProvider] = None,
    store: bool = True,
    now: Optional[datetime] = None,
) -> Dict[str, int]:
    """Re-track the last `days` recorded trading days.

    Re-tracking rather than only tracking the newest day is the point:
    a signal's 5-day return does not exist until five sessions later, so
    every day's records have to be revisited until their horizons
    mature. Records are appended and the newest per `signal_id` wins on
    read, so repeated runs converge rather than conflict.
    """
    processed: Dict[str, int] = {}
    for day in result_store.available_trading_days()[-int(days):]:
        try:
            records = track_day(day, provider=provider, store=store, now=now)
            processed[day] = len(records)
        except Exception:  # noqa: BLE001 - one bad day must not stop the backfill
            logger.exception("performance tracking failed for %s", day)
            processed[day] = 0
    return processed


def _signal_day_close(daily_frame, trading_day: str) -> Optional[float]:
    if daily_frame is None or len(daily_frame) == 0:
        return None
    if not isinstance(daily_frame.index, pd.DatetimeIndex):
        return None
    try:
        target = datetime.fromisoformat(str(trading_day)).date()
    except (TypeError, ValueError):
        return None
    for stamp, row in daily_frame.iterrows():
        if stamp.date() == target:
            return ind.to_float(row.get("Close", row.get("close")))
    return None
