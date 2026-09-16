"""The SIP consolidated tape, in bulk.

Why this exists at all
----------------------
The KIS chart serves ONE symbol per call behind a 3.0s global rate
limiter shared by every process on the host. Measured 2026-09-16 on the
live universe, that is 5.93s per symbol at the median -- so a 594-symbol
extended-session scan costs ~59 minutes against a 15-minute cadence, and
the whole universe is unreachable. The limiter is not the bug; asking a
per-symbol endpoint for hundreds of symbols is.

Alpaca's bars endpoint takes a symbol LIST. Measured the same day, the
full 13,409-symbol universe over a 5.5-hour premarket window returned
106,505 bar points in 104.7s across 68 pages -- about 760x the KIS path
for the same work.

What this client refuses to do
------------------------------
It asks for `feed=sip` and nothing else. IEX is available on the same
account and is NOT an acceptable substitute: on the identical window
where SIP gave AAPL 14,834 shares in its 04:00 ET bar, IEX returned no
AAPL bar at all and single-trade bars elsewhere. A quiet-looking premarket
built from IEX is the exact failure this whole provider exists to end, so
the feed is a constant here rather than a parameter -- see `FEED`.

The entitlement boundary is explicit
------------------------------------
This account holds SIP HISTORICAL but not recent SIP. Alpaca says so
directly: `GET /v2/stocks/AAPL/quotes/latest?feed=sip` answers 403
`"subscription does not permit querying recent SIP data"`, and the bars
endpoint silently truncates instead -- measured lag 15.3-17.3 minutes
across AAPL/MSFT/NVDA/TSLA on 2026-09-16.

A silent truncation is the dangerous shape: it looks like a market that
stopped trading. `RecentDataUnavailable` and `newest_bar_lag_seconds()`
exist so a caller can TELL, and so an authority path can refuse rather
than infer a quiet tape.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://data.alpaca.markets/v2/stocks/bars"

#: The consolidated tape. Not a parameter -- see the module docstring.
FEED = "sip"

#: Symbols per request. Alpaca accepts a long list; 200 keeps the query
#: string well inside any gateway limit while making the request count
#: negligible (68 pages for 13,409 symbols).
DEFAULT_BATCH_SIZE = 200

#: Alpaca's own per-response cap. Pagination is driven by the returned
#: `next_page_token`, never by this number -- the cap is the reason
#: pagination exists, not a page size to compute against.
MAX_LIMIT = 10000

DEFAULT_TIMEOUT_SECONDS = 60.0

#: How stale the newest bar may be before a caller should treat the feed
#: as delayed rather than live. The measured entitlement delay is ~15-17
#: minutes; 300s is comfortably below it and comfortably above normal
#: publication jitter, so it separates the two without straddling either.
FRESH_WITHIN_SECONDS = 300.0


class AlpacaDataError(Exception):
    """The request did not produce usable bars."""


class CredentialsMissing(AlpacaDataError):
    """No API key pair configured."""


class EntitlementError(AlpacaDataError):
    """The account is not entitled to what was asked for.

    Carried as its own type because the correct response differs from
    every other failure: a retry cannot fix it and a fallback feed must
    not silently paper over it.
    """


class RecentDataUnavailable(EntitlementError):
    """The window asked for data newer than the subscription serves."""


def credentials(env=None) -> tuple:
    """The key pair, from the environment.

    Returns the pair; never logs it. The two names are the ones already
    present in `shared/env/kis-readonly.env`, so this introduces no new
    secret and no new place to put one.
    """
    import os

    source = env if env is not None else os.environ
    key = source.get("ALPACA_API_KEY") or source.get("APCA_API_KEY_ID")
    secret = source.get("ALPACA_SECRET_KEY") or source.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise CredentialsMissing(
            "ALPACA_API_KEY / ALPACA_SECRET_KEY are not both set")
    return key, secret


def _headers(key, secret) -> Dict[str, str]:
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def _parse_timestamp(raw) -> Optional[datetime]:
    """Alpaca's RFC-3339 stamp as a tz-aware UTC datetime.

    Always aware. A naive timestamp downstream would be compared against
    aware session boundaries and raise, or worse, be coerced to local
    time by something that does not raise.
    """
    if not raw:
        return None
    try:
        text = str(raw).replace("Z", "+00:00")
        moment = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


class AlpacaDataClient:
    """Bulk 1-minute SIP bars, batched and paginated."""

    def __init__(self, *, key=None, secret=None, session=None,
                 batch_size=DEFAULT_BATCH_SIZE,
                 timeout_seconds=DEFAULT_TIMEOUT_SECONDS, base_url=BASE_URL):
        if key is None or secret is None:
            key, secret = credentials()
        self._key = key
        self._secret = secret
        self._http = session or requests.Session()
        self._batch_size = max(1, int(batch_size))
        self._timeout = float(timeout_seconds)
        self._base_url = base_url
        #: Observability for the performance report (§12). Counted here
        #: because this is the only place that knows about a page.
        self.request_count = 0
        self.page_count = 0
        self.fetch_seconds = 0.0

    # -- the wire --------------------------------------------------------

    def _get(self, params) -> dict:
        self.request_count += 1
        try:
            response = self._http.get(
                self._base_url, params=params,
                headers=_headers(self._key, self._secret),
                timeout=self._timeout)
        except requests.RequestException as exc:
            raise AlpacaDataError(f"Alpaca request failed: {exc}") from exc

        if response.status_code in (401, 403):
            body = (response.text or "")[:200]
            if "recent" in body.lower() or "subscription" in body.lower():
                raise RecentDataUnavailable(
                    f"Alpaca subscription does not serve this window: {body}")
            raise EntitlementError(
                f"Alpaca refused the request ({response.status_code}): {body}")
        if response.status_code != 200:
            raise AlpacaDataError(
                f"Alpaca returned {response.status_code}: "
                f"{(response.text or '')[:200]}")
        try:
            return response.json()
        except ValueError as exc:
            raise AlpacaDataError(f"Alpaca returned non-JSON: {exc}") from exc

    # -- the bulk fetch --------------------------------------------------

    def fetch_bars(self, symbols: Sequence[str], *, start: datetime,
                   end: Optional[datetime] = None,
                   timeframe: str = "1Min") -> Dict[str, List[dict]]:
        """Every 1-minute bar for `symbols` in `[start, end)`.

        Returns symbol -> bars, oldest first. A symbol with no prints in
        the window is ABSENT from the result rather than present-and-empty
        -- 45 of a 200-symbol sample had no premarket prints on
        2026-09-15, and 32 of those traded normally in regular hours. They
        were quiet, not missing, and the two must stay distinguishable.

        The universe size is whatever is passed. There is no cap, no
        truncation and no padding here; batching is an implementation
        detail of the wire, not a limit on the caller.
        """
        wanted = [str(s).strip().upper() for s in symbols if str(s).strip()]
        # Deduplicate while preserving order: a repeated symbol would
        # otherwise be requested twice and merged into itself.
        seen = set()
        unique = [s for s in wanted if not (s in seen or seen.add(s))]

        collected: Dict[str, List[dict]] = {}
        began = time.time()
        for index in range(0, len(unique), self._batch_size):
            batch = unique[index:index + self._batch_size]
            self._fetch_batch(batch, start=start, end=end,
                              timeframe=timeframe, into=collected)
        self.fetch_seconds += time.time() - began

        for rows in collected.values():
            rows.sort(key=lambda row: row["at"])
        return collected

    def _fetch_batch(self, batch, *, start, end, timeframe, into) -> None:
        token = None
        while True:
            params = {
                "symbols": ",".join(batch),
                "timeframe": timeframe,
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

            for symbol, rows in (payload.get("bars") or {}).items():
                target = into.setdefault(symbol, [])
                for row in rows or []:
                    normalized = _normalize_row(symbol, row)
                    if normalized is not None:
                        target.append(normalized)

            token = payload.get("next_page_token")
            if not token:
                return

    def newest_bar_lag_seconds(self, symbol="AAPL", *, now=None,
                               lookback_minutes=90) -> Optional[float]:
        """How far behind the newest available bar is.

        The entitlement probe, expressed as a number a caller can gate
        on. `None` means no bars at all in the lookback, which is not the
        same as "fresh" and must never be read as it.
        """
        moment = now or datetime.now(timezone.utc)
        bars = self.fetch_bars(
            [symbol], start=moment - timedelta(minutes=lookback_minutes))
        rows = bars.get(str(symbol).upper()) or []
        if not rows:
            return None
        return (moment - rows[-1]["at"]).total_seconds()


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_row(symbol, row) -> Optional[dict]:
    """One Alpaca bar as this codebase's row shape.

    Returns None for a row that cannot be trusted rather than a row with
    a zero in it. A bar with no timestamp or no price is not a quiet
    minute -- it is an unparseable one, and the difference is the whole
    reason `kis_bar_features` can refuse a range.
    """
    at = _parse_timestamp(row.get("t"))
    if at is None:
        return None
    try:
        open_ = float(row["o"])
        high = float(row["h"])
        low = float(row["l"])
        close = float(row["c"])
        volume = float(row.get("v") or 0.0)
    except (KeyError, TypeError, ValueError):
        return None
    if not all(value > 0 for value in (open_, high, low, close)):
        return None
    return {
        "symbol": str(symbol).upper(),
        "at": at,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        # Alpaca publishes a trade count and a volume-weighted price per
        # bar. Both are carried because the KIS stream also produces
        # them, and parity comparison needs the same fields on each side.
        "trade_count": int(row.get("n") or 0),
        "vwap": float(row["vw"]) if row.get("vw") is not None else None,
    }
