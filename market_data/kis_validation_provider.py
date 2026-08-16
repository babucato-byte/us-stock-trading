"""KISValidationProvider -- the pre-order price re-check (spec §13):
"Alpaca 신호 -> KIS 종목 매핑 확인 -> KIS 현재가·호가 재조회 -> 가격 차이
계산". Wraps `brokers/kis_broker.py`'s read-only `get_current_price()` --
never a write call. `compute_price_deviation_percent()` is the single
place the deviation formula `order_gate.py` consumes lives, so both this
module's own callers and any future reporting code compute it
identically.
"""

import math
from datetime import datetime, timezone

from brokers.kis_broker import KISBrokerError
from domain.instrument import Instrument
from market_data.base import MarketDataProvider, MarketDataProviderError, PriceQuote


class KISValidationProvider(MarketDataProvider):
    def __init__(self, broker, *, instrument_lookup):
        """`instrument_lookup` is a callable `symbol -> Instrument`,
        supplied by the caller (this module never constructs an
        Instrument itself -- that mapping/normalization decision belongs
        to whichever layer owns the Instrument universe, per spec §8's
        "별도 계층에서 처리")."""
        self._broker = broker
        self._instrument_lookup = instrument_lookup

    def get_price_quote(self, symbol: str) -> PriceQuote:
        instrument = self._instrument_lookup(symbol)
        try:
            price = self._broker.get_current_price(instrument)
        except KISBrokerError as exc:
            raise MarketDataProviderError(f"KIS price re-check failed for {symbol!r}: {exc}") from exc
        return PriceQuote(
            symbol=symbol, price_usd=price, as_of=datetime.now(timezone.utc), source="kis_price_quote",
        )


def compute_price_deviation_percent(signal_price_usd: float, kis_price_usd: float) -> float:
    """`abs(kis - signal) / signal * 100` -- the exact formula spec §13
    describes, and the same one execution/order_gate.py's
    evaluate_buy_gate() applies. Centralized here so a future caller that
    wants to REPORT deviation (e.g. Shadow Mode logging, spec §26) never
    silently drifts from the enforcement formula."""
    if not isinstance(signal_price_usd, (int, float)) or isinstance(signal_price_usd, bool) \
            or not math.isfinite(signal_price_usd) or signal_price_usd <= 0:
        raise MarketDataProviderError(f"signal_price_usd must be a positive finite number, got {signal_price_usd!r}")
    if not isinstance(kis_price_usd, (int, float)) or isinstance(kis_price_usd, bool) \
            or not math.isfinite(kis_price_usd) or kis_price_usd <= 0:
        raise MarketDataProviderError(f"kis_price_usd must be a positive finite number, got {kis_price_usd!r}")
    return abs(kis_price_usd - signal_price_usd) / signal_price_usd * 100.0
