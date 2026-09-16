"""1-minute bars aggregated from the raw SIP trade tape, odd lots included.

Why this module exists
----------------------
Phase 1 measured the defect that blocks authority, and it is not the
15-minute entitlement delay. Alpaca's BARS endpoint excludes odd-lot
trades; KIS's HDFSCNT0 tick stream includes them. Premarket in a
high-priced name is almost entirely odd lots, so the two feeds disagree
exactly where S6 looks.

Measured 2026-09-16 premarket:

    TMO ($642)  trades 62 across 27 minutes, 62/62 under 100 shares
                bars endpoint:  1 bar      KIS chart: 16 bars
    AAPL        trades 7,683 across 258 minutes, 95% odd lot
                bars endpoint: 161 bars

TMO's ORB5 opening range was computable from KIS and not from the bars
endpoint, and the parity run recorded it as the single false negative
(KIS 1 positive, broad 0, RECALL 0.0). No subscription upgrade fixes
that -- the odd lots are absent from the aggregate, not withheld by
entitlement.

The raw trades ARE served. `/v2/stocks/trades` is multi-symbol, carries
size and condition per print, and includes the odd lots. So this
aggregates bars from the tape instead of accepting the vendor's.

Which trades count
------------------
All of them, by default -- because the reference this has to match is
KIS's tick stream, which does not filter. That is a deliberate choice
rather than an oversight, so `EXCLUDED_CONDITIONS` exists and is EMPTY,
and the condition histogram is returned alongside the bars. A future
reader who wants last-sale-eligible semantics changes one constant and
can see from the histogram what it costs.

The cost, stated plainly
------------------------
A bar is one row; a trade is one row per print. AAPL alone is 7,683
prints over a premarket window where its bar count is 161. The tape is
roughly two orders of magnitude more data, the 10,000-row page cap binds
almost immediately, and pagination -- not symbol count -- is what sets
the wall clock. `fetch_trades` reports pages and API calls for exactly
that reason.
"""

import logging
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

import requests

from market_data.alpaca_data_client import (
    AlpacaDataError, EntitlementError, RecentDataUnavailable, credentials,
)

logger = logging.getLogger(__name__)

TRADES_URL = "https://data.alpaca.markets/v2/stocks/trades"

FEED = "sip"

#: Alpaca's per-response row cap for the trades endpoint.
MAX_LIMIT = 10000

#: Symbols per request. Smaller than the bars client's 200: a page is
#: capped in ROWS, and a wide symbol list simply means each page covers
#: less time per symbol. Keeping the batch narrow makes pagination
#: progress per symbol rather than interleaving hundreds of them.
DEFAULT_BATCH_SIZE = 50

DEFAULT_TIMEOUT_SECONDS = 60.0

#: Trade conditions to drop before aggregating. EMPTY ON PURPOSE -- see
#: the module docstring. `I` (odd lot) in particular must NOT be added
#: here: excluding it reproduces precisely the gap this module exists to
#: close.
EXCLUDED_CONDITIONS = frozenset()


class TradeTapeClient:
    """Raw SIP trades, batched and paginated."""

    def __init__(self, *, key=None, secret=None, session=None,
                 batch_size=DEFAULT_BATCH_SIZE,
                 timeout_seconds=DEFAULT_TIMEOUT_SECONDS, base_url=TRADES_URL):
        if key is None or secret is None:
            key, secret = credentials()
        self._key = key
        self._secret = secret
        self._http = session or requests.Session()
        self._batch_size = max(1, int(batch_size))
        self._timeout = float(timeout_seconds)
        self._base_url = base_url
        self.api_calls = 0
        self.page_count = 0
        self.fetch_seconds = 0.0
        self.raw_trade_count = 0

    def _get(self, params) -> dict:
        self.api_calls += 1
        try:
            response = self._http.get(
                self._base_url, params=params,
                headers={"APCA-API-KEY-ID": self._key,
                         "APCA-API-SECRET-KEY": self._secret},
                timeout=self._timeout)
        except requests.RequestException as exc:
            raise AlpacaDataError(f"Alpaca trades request failed: {exc}") from exc

        if response.status_code in (401, 403):
            body = (response.text or "")[:200]
            if "recent" in body.lower() or "subscription" in body.lower():
                raise RecentDataUnavailable(
                    f"Alpaca subscription does not serve this window: {body}")
            raise EntitlementError(
                f"Alpaca refused the trades request "
                f"({response.status_code}): {body}")
        if response.status_code != 200:
            raise AlpacaDataError(
                f"Alpaca trades returned {response.status_code}: "
                f"{(response.text or '')[:200]}")
        try:
            return response.json()
        except ValueError as exc:
            raise AlpacaDataError(f"Alpaca returned non-JSON: {exc}") from exc

    def fetch_trades(self, symbols: Sequence[str], *, start: datetime,
                     end: Optional[datetime] = None) -> Dict[str, List[dict]]:
        """Every raw print for `symbols` in `[start, end)`, oldest first.

        Pagination is driven by `next_page_token` and nothing else. The
        cap is on ROWS across the whole response, so a batch of liquid
        names pages many times over a long window -- that is the real
        cost of the trade tape and it is counted, not hidden.
        """
        wanted = [str(s).strip().upper() for s in symbols if str(s).strip()]
        seen = set()
        unique = [s for s in wanted if not (s in seen or seen.add(s))]

        collected: Dict[str, List[dict]] = {}
        began = time.time()
        for index in range(0, len(unique), self._batch_size):
            batch = unique[index:index + self._batch_size]
            self._fetch_batch(batch, start=start, end=end, into=collected)
        self.fetch_seconds += time.time() - began

        for rows in collected.values():
            rows.sort(key=lambda row: row["at"])
        return collected

    def _fetch_batch(self, batch, *, start, end, into) -> None:
        token = None
        while True:
            params = {
                "symbols": ",".join(batch),
                "start": _iso(start),
                "feed": FEED,
                "limit": str(MAX_LIMIT),
            }
            if end is not None:
                params["end"] = _iso(end)
            if token:
                params["page_token"] = token

            payload = self._get(params)
            self.page_count += 1

            for symbol, rows in (payload.get("trades") or {}).items():
                target = into.setdefault(symbol, [])
                for row in rows or []:
                    trade = _normalize_trade(row)
                    if trade is not None:
                        target.append(trade)
                        self.raw_trade_count += 1

            token = payload.get("next_page_token")
            if not token:
                return


    def stream_minute_bars(self, symbols: Sequence[str], *, start: datetime,
                           end: Optional[datetime] = None,
                           excluded_conditions=None) -> dict:
        """Fold each page into bars and never retain the raw prints.

        Why this exists, measured rather than assumed
        ---------------------------------------------
        `fetch_trades` + `to_minute_bars` holds the entire tape in
        memory. On the production host -- 956MB total, 189MB free, 2
        cores, already swapping -- that is what costs the time: the
        aggregation itself runs at 3.1us/trade on an unloaded machine
        and was measured at 81us/trade there, because 743k trade dicts
        push the box into swap.

        Bars are three orders of magnitude fewer than trades (20,450
        bars from 811,251 trades at 100 symbols), so folding as the
        pages arrive makes peak memory O(bars) instead of O(trades) and
        takes the tape off the critical resource on this host.

        Returns the same shape `to_minute_bars` does, per symbol.
        """
        excluded = (EXCLUDED_CONDITIONS if excluded_conditions is None
                    else excluded_conditions)
        wanted = [str(s).strip().upper() for s in symbols if str(s).strip()]
        seen = set()
        unique = [s for s in wanted if not (s in seen or seen.add(s))]

        buckets: Dict[str, dict] = {}
        conditions: Dict[str, Counter] = {}
        odd_lots: Dict[str, int] = {}
        used: Dict[str, int] = {}

        began = time.time()
        for index in range(0, len(unique), self._batch_size):
            batch = unique[index:index + self._batch_size]
            token = None
            while True:
                params = {
                    "symbols": ",".join(batch),
                    "start": _iso(start),
                    "feed": FEED,
                    "limit": str(MAX_LIMIT),
                }
                if end is not None:
                    params["end"] = _iso(end)
                if token:
                    params["page_token"] = token

                payload = self._get(params)
                self.page_count += 1

                for symbol, rows in (payload.get("trades") or {}).items():
                    target = buckets.setdefault(symbol, {})
                    histogram = conditions.setdefault(symbol, Counter())
                    for row in rows or []:
                        trade = _normalize_trade(row)
                        if trade is None:
                            continue
                        self.raw_trade_count += 1
                        for code in trade["conditions"]:
                            histogram[code] += 1
                        if excluded and (set(trade["conditions"])
                                         & set(excluded)):
                            continue
                        used[symbol] = used.get(symbol, 0) + 1
                        if trade["size"] < 100:
                            odd_lots[symbol] = odd_lots.get(symbol, 0) + 1
                        _fold(target, trade)

                token = payload.get("next_page_token")
                if not token:
                    break
        self.fetch_seconds += time.time() - began

        out = {}
        for symbol in unique:
            target = buckets.get(symbol) or {}
            bars = [target[minute] for minute in sorted(target)]
            for bar in bars:
                bar["symbol"] = symbol
            out[symbol] = {
                "bars": bars,
                "conditions": conditions.get(symbol, Counter()),
                "odd_lot_trades": odd_lots.get(symbol, 0),
                "trades_used": used.get(symbol, 0),
            }
        return out


def _fold(buckets, trade) -> None:
    """Merge one print into its minute bucket, in place.

    Pages arrive oldest-first WITHIN a symbol, so `open` is whatever
    landed first and `close` is simply the latest seen. The high/low
    comparisons do not depend on order at all, which is what lets this
    run incrementally without buffering the symbol's prints.
    """
    minute = trade["at"].replace(second=0, microsecond=0)
    bucket = buckets.get(minute)
    if bucket is None:
        buckets[minute] = {
            "minute": minute, "open": trade["price"], "high": trade["price"],
            "low": trade["price"], "close": trade["price"],
            "volume": trade["size"],
            "price_volume": trade["price"] * trade["size"],
            "trade_count": 1,
            "first_trade_at": trade["at"], "last_trade_at": trade["at"],
        }
        return
    if trade["price"] > bucket["high"]:
        bucket["high"] = trade["price"]
    if trade["price"] < bucket["low"]:
        bucket["low"] = trade["price"]
    if trade["at"] >= bucket["last_trade_at"]:
        bucket["close"] = trade["price"]
        bucket["last_trade_at"] = trade["at"]
    if trade["at"] < bucket["first_trade_at"]:
        bucket["open"] = trade["price"]
        bucket["first_trade_at"] = trade["at"]
    bucket["volume"] += trade["size"]
    bucket["price_volume"] += trade["price"] * trade["size"]
    bucket["trade_count"] += 1


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_timestamp(raw) -> Optional[datetime]:
    if not raw:
        return None
    text = str(raw)
    # Alpaca stamps trades to nanoseconds; `fromisoformat` on 3.9 takes
    # at most microseconds, so the fractional part is trimmed rather
    # than the whole print being dropped for excess precision.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = "".join(ch for ch in tail if ch.isdigit())[:6]
        zone = "Z" if text.endswith("Z") else ""
        if not zone:
            for marker in ("+", "-"):
                position = tail.find(marker)
                if position != -1:
                    zone = tail[position:]
                    break
        text = f"{head}.{digits}{zone or 'Z'}"
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _normalize_trade(row) -> Optional[dict]:
    at = _parse_timestamp(row.get("t"))
    if at is None:
        return None
    try:
        price = float(row["p"])
        size = float(row.get("s") or 0.0)
    except (KeyError, TypeError, ValueError):
        return None
    if price <= 0 or size <= 0:
        return None
    return {"at": at, "price": price, "size": size,
            "conditions": tuple(row.get("c") or ()),
            "exchange": row.get("x")}


def to_minute_bars(trades, *, symbol, session,
                   excluded_conditions=EXCLUDED_CONDITIONS) -> dict:
    """Aggregate raw prints into 1-minute OHLCV bars.

    Returns `{"bars": [...], "conditions": Counter, "odd_lot_trades": n,
    "trades_used": n}`. A minute with no print produces NO bar -- the
    sparse tape stays sparse, exactly as the bars path and the KIS
    stream both leave it.
    """
    buckets = {}
    conditions = Counter()
    odd_lots = 0
    used = 0
    for trade in trades:
        for code in trade["conditions"]:
            conditions[code] += 1
        if excluded_conditions and (set(trade["conditions"])
                                    & set(excluded_conditions)):
            continue
        used += 1
        if trade["size"] < 100:
            odd_lots += 1
        minute = trade["at"].replace(second=0, microsecond=0)
        bucket = buckets.get(minute)
        if bucket is None:
            buckets[minute] = {
                "minute": minute, "open": trade["price"],
                "high": trade["price"], "low": trade["price"],
                "close": trade["price"], "volume": trade["size"],
                "price_volume": trade["price"] * trade["size"],
                "trade_count": 1,
                "first_trade_at": trade["at"], "last_trade_at": trade["at"],
            }
            continue
        # Prints arrive oldest-first, so open is already set and close is
        # simply the latest one seen.
        if trade["price"] > bucket["high"]:
            bucket["high"] = trade["price"]
        if trade["price"] < bucket["low"]:
            bucket["low"] = trade["price"]
        bucket["close"] = trade["price"]
        bucket["volume"] += trade["size"]
        bucket["price_volume"] += trade["price"] * trade["size"]
        bucket["trade_count"] += 1
        bucket["last_trade_at"] = trade["at"]

    bars = [buckets[minute] for minute in sorted(buckets)]
    for bar in bars:
        bar["symbol"] = symbol
        bar["session"] = session
    return {"bars": bars, "conditions": conditions,
            "odd_lot_trades": odd_lots, "trades_used": used}


def to_frame(bars):
    """The aggregated bars as the frame the scanner already consumes."""
    import pandas as pd

    if not bars:
        return pd.DataFrame()
    return pd.DataFrame([{
        "Open": b["open"], "High": b["high"], "Low": b["low"],
        "Close": b["close"], "Volume": b["volume"],
    } for b in bars], index=pd.DatetimeIndex(
        [b["minute"] for b in bars], name="Datetime"))
