"""Shared framework for the independent scanners (spec sections 3, 5, 7-10).

Lazy on purpose (PEP 562 module __getattr__)
---------------------------------------------
Every name below used to be imported eagerly here, which means ANY
import of ANY submodule of this package -- including ones that need
nothing from this list, like scanners.base.session_range's plain date
arithmetic -- paid the full cost of the whole scanner framework: pandas,
yfinance, pandas_market_calendars, transitively through features.py and
market_data_provider.py. Measured cold: 11 seconds, once per process.

S6's active-watch fast path is a fresh process every entry tick (cron,
not a daemon), and its hot path calls scanners.base.session_range for
session windows -- it never touches a market-data provider or a scanner
base class. Paying 11s of that for a helper that only reads a static
dict was most of one observed 73.7-second tick, which is over the
60-second cron interval and causes the NEXT tick to be skipped (see
s6_live/active_watch.py's docstring and the runtime evidence in
docs/review/).

__getattr__ defers each name to its owning submodule until the FIRST
time something actually asks for it, exactly once (then caches in this
module's own namespace, so every access after the first is a plain
attribute lookup, not a re-import). Every existing caller -- `from
scanners.base import BaseScanner`, `scanners.base.ScannerConfig`, star
imports via __all__ -- keeps working identically; only WHEN the
underlying heavy import happens changes.
"""

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

#: name -> (submodule, attribute-on-that-submodule). The attribute name
#: differs from the exported name nowhere today, but is kept separate so
#: a future re-export under a different local name stays possible.
_LAZY = {
    "ScannerConfig": ("scanners.base.config", "ScannerConfig"),
    "ScannerConfigError": ("scanners.base.config", "ScannerConfigError"),
    "load_config": ("scanners.base.config", "load_config"),
    "SymbolFeatures": ("scanners.base.features", "SymbolFeatures"),
    "build_features": ("scanners.base.features", "build_features"),
    "AlpacaMarketDataProvider": ("scanners.base.market_data_provider", "AlpacaMarketDataProvider"),
    "BarMarketDataProvider": ("scanners.base.market_data_provider", "BarMarketDataProvider"),
    "CachingMarketDataProvider": ("scanners.base.market_data_provider", "CachingMarketDataProvider"),
    "MarketDataUnavailable": ("scanners.base.market_data_provider", "MarketDataUnavailable"),
    "PremarketSnapshot": ("scanners.base.market_data_provider", "PremarketSnapshot"),
    "StaticMarketDataProvider": ("scanners.base.market_data_provider", "StaticMarketDataProvider"),
    "SymbolData": ("scanners.base.market_data_provider", "SymbolData"),
    "YFinanceMarketDataProvider": ("scanners.base.market_data_provider", "YFinanceMarketDataProvider"),
    "YahooFinanceMarketDataProvider": ("scanners.base.market_data_provider", "YahooFinanceMarketDataProvider"),
    "default_provider": ("scanners.base.market_data_provider", "default_provider"),
    "ScannerDataError": ("scanners.base.models", "ScannerDataError"),
    "ScannerSignal": ("scanners.base.models", "ScannerSignal"),
    "BaseScanner": ("scanners.base.scanner_base", "BaseScanner"),
    "Rejected": ("scanners.base.scanner_base", "Rejected"),
    "ScanOutcome": ("scanners.base.scanner_base", "ScanOutcome"),
    "fmt": ("scanners.base.scanner_base", "fmt"),
    "require": ("scanners.base.scanner_base", "require"),
}


def __getattr__(name):
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module_name, attr_name = target
    value = getattr(importlib.import_module(module_name), attr_name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
