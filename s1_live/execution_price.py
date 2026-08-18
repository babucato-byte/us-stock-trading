"""Is this KIS execution price usable right now?

The question this replaces
-------------------------
The order gate asked "has the price moved more than 0.30% since the
signal?". For the scalping path that is the right question: its signal is
seconds old, so a large gap means a stale or wrong quote.

S1's signal price is the PREVIOUS COMPLETED CLOSE. An overnight gap and a
morning move are not anomalies there, they are the strategy. On
2026-08-18 every one of the nine ranked candidates was refused, the
tightest by 0.46% -- the guard had become a permanent block rather than a
check.

So the check is reframed, not removed: instead of comparing against
yesterday, ask whether the price KIS is quoting is consistent with KIS's
own trading day.

Why the band is the market's own range
--------------------------------------
No percentage is invented here. The tolerance is today's own high and
low, which the market sets:

    low <= price <= high

A stale quote, a wrong-exchange answer or a garbled field lands outside
that range or makes it degenerate; a legitimate intraday price cannot.
That is the property a fixed percentage was reaching for, obtained from
data rather than from a chosen number.

Why not a second vendor
-----------------------
A cross-feed check was tried first and the diagnostic ruled it out. The
account has no SIP entitlement (HTTP 403), leaving IEX -- one venue with a
few percent of consolidated volume. Measured on these candidates its
median quoted spread was 25.6%, so "KIS is inside Alpaca's bid/ask"
accepted a symbol whose reference trade was 21 days old (BYFC), while the
single name with a tight, fresh book was the only one it REFUSED (TAL, KIS
half a cent through the ask). It fails on good data and passes on bad, so
it is recorded as diagnostic provenance and never gates an order.

Freshness without a timestamp
-----------------------------
`price-detail` carries no quote timestamp. The response is fetched at
order time, so it is contemporaneous by construction; what has to be
established is that the DATA describes today. Today's traded volume does
that -- a session with no prints has no range to check against.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

PASS = "PASS"
BLOCK = "BLOCK"

REASON_OK = "EXECUTION_PRICE_OK"
REASON_NO_DETAIL = "PRICE_DETAIL_UNAVAILABLE"
REASON_PRICE_MISSING = "PRICE_MISSING"
REASON_RANGE_MISSING = "DAY_RANGE_MISSING"
REASON_RANGE_DEGENERATE = "DAY_RANGE_DEGENERATE"
REASON_ABOVE_HIGH = "PRICE_ABOVE_DAY_HIGH"
REASON_BELOW_LOW = "PRICE_BELOW_DAY_LOW"
REASON_NO_VOLUME = "NO_TRADES_TODAY"
REASON_CURRENCY = "CURRENCY_NOT_USD"
REASON_NOT_ORDERABLE = "INSTRUMENT_NOT_ORDERABLE_AT_BROKER"

#: KIS reports tradability as Korean text on `e_ordyn`. The observed
#: affirmative value is recorded rather than guessed at; anything else is
#: refused, so a new value KIS starts sending blocks instead of passing.
ORDERABLE_AFFIRMATIVE = ("매매 가능",)

EXPECTED_CURRENCY = "USD"

#: What the cross-feed check would have been, kept as provenance so a
#: report can state why it is absent instead of implying it ran.
EXTERNAL_VALIDATION_STATUS = "UNAVAILABLE_FOR_HARD_GATE"
ALPACA_FEED = "IEX"
SIP_ENTITLEMENT = False


@dataclass(frozen=True)
class ExecutionPriceVerdict:
    symbol: str
    status: str
    reason_code: str
    detail: str = ""
    price: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    tick_size: Optional[float] = None
    prev_close: Optional[float] = None
    today_volume: Optional[float] = None
    provenance: Dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == PASS

    def as_dict(self) -> Dict[str, Any]:
        return dict(vars(self), passed=self.passed)


def _provenance(detail: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    # `detail` may be anything a malformed response produced -- a list, a
    # number, None. Only a dict is read from, so building provenance for a
    # refusal cannot itself raise: this function runs on the BLOCK path,
    # and an exception here would turn a clean refusal into a crash.
    fields = detail if isinstance(detail, dict) else {}
    return {
        "source": "KIS_PRICE_DETAIL",
        "kis_exchange_code": fields.get("kis_exchange_code"),
        "fetched_at": fields.get("fetched_at"),
        # Stated explicitly so no report can imply a cross-feed gate ran.
        "external_validation_status": EXTERNAL_VALIDATION_STATUS,
        "alpaca_feed": ALPACA_FEED,
        "sip_entitlement": SIP_ENTITLEMENT,
    }


def _finite(value) -> Optional[float]:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def evaluate(symbol, detail: Optional[Dict[str, Any]]) -> ExecutionPriceVerdict:
    """The verdict for one symbol. Every unclear case is a BLOCK."""
    prov = _provenance(detail)

    def block(reason, message, **extra):
        return ExecutionPriceVerdict(symbol=symbol, status=BLOCK, reason_code=reason,
                                     detail=message, provenance=prov, **extra)

    if not isinstance(detail, dict):
        return block(REASON_NO_DETAIL, "no KIS price-detail response")

    price = _finite(detail.get("last"))
    high = _finite(detail.get("high"))
    low = _finite(detail.get("low"))
    volume = _finite(detail.get("today_volume"))
    tick = _finite(detail.get("tick_size"))
    prev_close = _finite(detail.get("prev_close"))
    common = dict(price=price, day_high=high, day_low=low, tick_size=tick,
                  prev_close=prev_close, today_volume=volume)

    currency = str(detail.get("currency") or "").upper()
    if currency != EXPECTED_CURRENCY:
        return block(REASON_CURRENCY, f"currency is {currency or '<empty>'!r}", **common)

    orderable = str(detail.get("orderable_text") or "").strip()
    if orderable not in ORDERABLE_AFFIRMATIVE:
        return block(REASON_NOT_ORDERABLE,
                     f"KIS reports tradability as {orderable or '<empty>'!r}", **common)

    if price is None or price <= 0:
        return block(REASON_PRICE_MISSING, f"unusable last price {detail.get('last')!r}",
                     **common)
    if high is None or low is None:
        return block(REASON_RANGE_MISSING,
                     f"day range incomplete (high={detail.get('high')!r} "
                     f"low={detail.get('low')!r})", **common)
    if low <= 0 or high < low:
        return block(REASON_RANGE_DEGENERATE, f"day range [{low}, {high}] is not usable",
                     **common)
    if volume is None or volume <= 0:
        # No prints today means the range describes some other session.
        return block(REASON_NO_VOLUME, f"today's volume is {detail.get('today_volume')!r}",
                     **common)
    if price > high:
        return block(REASON_ABOVE_HIGH, f"price {price} above today's high {high}", **common)
    if price < low:
        return block(REASON_BELOW_LOW, f"price {price} below today's low {low}", **common)

    return ExecutionPriceVerdict(
        symbol=symbol, status=PASS, reason_code=REASON_OK,
        detail=f"price {price} within today's range [{low}, {high}]",
        provenance=prov, **common)


def evaluate_symbol(symbol, *, broker, instrument=None) -> ExecutionPriceVerdict:
    """Fetch and evaluate. A read failure is a BLOCK, never a pass."""
    from market_data.exchange_registry import build_kis_instrument

    if instrument is None:
        instrument, _record = build_kis_instrument(symbol)
    try:
        detail = broker.get_price_detail(instrument)
    except Exception as exc:
        logger.warning("S1 execution-price detail unavailable for %s: %s", symbol, exc)
        return ExecutionPriceVerdict(
            symbol=symbol, status=BLOCK, reason_code=REASON_NO_DETAIL,
            detail=str(exc)[:200], provenance=_provenance(None))
    return evaluate(symbol, detail)
