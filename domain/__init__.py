"""Broker-agnostic domain models for the KIS live-broker migration.

Per docs/autonomous/PROJECT_CONSTITUTION.md's 계층 분리 원칙 (extended for
this migration): Strategy/Risk/Sizing code speaks only these dataclasses
-- never an Alpaca or KIS response object directly. `Instrument.alpaca_
symbol`/`kis_symbol` exist so the two adapters can each resolve their own
wire format from one broker-agnostic `Instrument`, but nothing outside
`market_data/alpaca_provider.py` and `brokers/kis_broker.py` should ever
read those two fields -- everyone else uses `symbol`/`normalized_symbol`.
"""
