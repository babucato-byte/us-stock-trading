"""AlpacaMarketDataProvider -- market-data-only wrapper, per spec §2/§10:
"Alpaca는 데이터 공급자로만 사용한다" / "Alpaca Paper·Live 주문 기능
완전 차단".

Important note on what "Alpaca data" actually means in THIS codebase:
`paper_strategy_order.analyze_stock()` and `daily_candidate_scanner.py`
have always sourced price/volume data from `yfinance`, not a literal
Alpaca Market Data API call -- Alpaca's role in this codebase has always
been the BROKER (account/order endpoints), never the price-data source.
This module wraps that SAME existing yfinance-based data path (spec §5:
재사용, not reimplementation) rather than introducing a new Alpaca Data
API integration that would duplicate an already-working, already-tested
data source for no safety benefit. The module is still named
`alpaca_provider.py` per the migration spec's required file layout, and
this note documents why its implementation is yfinance, not `alpaca-py`.

This module NEVER imports or calls anything from `broker/alpaca_client.py`
(the order-submission adapter) -- there is no code path from here into
an order call, satisfying spec §10's "Alpaca 데이터 모듈 → 주문 실행"
prohibition structurally, not just by convention.
"""

import math
from datetime import datetime, timezone

import yfinance as yf

from market_data.base import MarketDataProvider, MarketDataProviderError, PriceQuote


class AlpacaMarketDataProvider(MarketDataProvider):
    def __init__(self, *, now_fn=None):
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    def get_price_quote(self, symbol: str) -> PriceQuote:
        try:
            history = yf.Ticker(symbol).history(period="5d")
        except Exception as exc:
            raise MarketDataProviderError(f"yfinance lookup failed for {symbol!r}: {exc}") from exc
        if history.empty:
            raise MarketDataProviderError(f"no yfinance price history returned for {symbol!r}")
        try:
            price = float(history["Close"].iloc[-1])
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise MarketDataProviderError(f"yfinance response missing a usable Close price for {symbol!r}: {exc}") from exc
        if not math.isfinite(price) or price <= 0:
            raise MarketDataProviderError(f"yfinance returned a non-positive/non-finite price for {symbol!r}: {price!r}")
        return PriceQuote(symbol=symbol, price_usd=price, as_of=self._now_fn(), source="alpaca_data_yfinance")
