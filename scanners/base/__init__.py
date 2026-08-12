"""Shared framework for the independent scanners (spec sections 3, 5, 7-10)."""

from scanners.base.config import ScannerConfig, ScannerConfigError, load_config
from scanners.base.features import SymbolFeatures, build_features
from scanners.base.market_data_provider import (
    AlpacaMarketDataProvider,
    BarMarketDataProvider,
    CachingMarketDataProvider,
    MarketDataUnavailable,
    PremarketSnapshot,
    StaticMarketDataProvider,
    SymbolData,
    YFinanceMarketDataProvider,
    YahooFinanceMarketDataProvider,
    default_provider,
)
from scanners.base.models import ScannerDataError, ScannerSignal
from scanners.base.scanner_base import BaseScanner, Rejected, ScanOutcome, fmt, require

__all__ = [
    "AlpacaMarketDataProvider",
    "BarMarketDataProvider",
    "BaseScanner",
    "CachingMarketDataProvider",
    "MarketDataUnavailable",
    "PremarketSnapshot",
    "Rejected",
    "ScanOutcome",
    "ScannerConfig",
    "ScannerConfigError",
    "ScannerDataError",
    "ScannerSignal",
    "StaticMarketDataProvider",
    "SymbolData",
    "SymbolFeatures",
    "YFinanceMarketDataProvider",
    "YahooFinanceMarketDataProvider",
    "build_features",
    "default_provider",
    "fmt",
    "load_config",
    "require",
]
