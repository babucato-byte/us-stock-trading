"""KIS (한국투자증권) Open API adapter package -- the sole live-order
broker in this migration (see docs/autonomous/DECISION_LOG.md's "Alpaca
data-only / KIS live broker" section). `broker/` (singular, existing)
remains the Alpaca market-data adapter; this package is intentionally
separate so nothing can import one and get the other by accident.
"""
