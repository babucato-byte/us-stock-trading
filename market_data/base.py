"""MarketDataProvider -- the interface both alpaca_provider.py and
kis_validation_provider.py implement, so callers (the future strategy-
wiring layer) can depend on this shape rather than either concrete
adapter. Deliberately minimal: candidate discovery and price re-
validation are the only two capabilities the rest of this migration
actually needs from a market-data source.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class PriceQuote:
    symbol: str
    price_usd: float
    as_of: datetime
    source: str


class MarketDataProviderError(Exception):
    """Raised on any market-data read failure. Callers must treat this
    as "no data available right now", never fabricate a price."""


class MarketDataProvider(ABC):
    @abstractmethod
    def get_price_quote(self, symbol: str) -> PriceQuote:
        """Returns the current price for `symbol`, or raises
        MarketDataProviderError."""
