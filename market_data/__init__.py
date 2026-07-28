"""Market data provider abstraction (spec §6/§15). `base.py` defines the
interface; `alpaca_provider.py` wraps this codebase's EXISTING Alpaca-
data-only code paths (yfinance-based `paper_strategy_order.analyze_stock`
and `daily_candidate_scanner.py`'s scoring, neither of which is touched
or reimplemented here -- this is a thin adapter, not a replacement);
`kis_validation_provider.py` wraps `brokers/kis_broker.py`'s read-only
methods for the pre-order price/account re-check spec §13 requires.

Alpaca is a DATA source only in this package -- nothing here ever calls
an order-submission method.
"""
