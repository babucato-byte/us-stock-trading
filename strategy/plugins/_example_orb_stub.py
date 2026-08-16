"""Minimal extension-pattern example -- NOT a real strategy.

Demonstrates the shape a future plugin (e.g. an Opening Range Breakout
strategy) would have: it implements `TradingStrategy`'s abstract methods
(so it can be constructed and registered at all) but the methods themselves
are left as NotImplementedError, since there is no real ORB logic here --
only the registration/extension pattern is being demonstrated. See
`strategy/plugins/__init__.py` for the step-by-step, and
`strategy/plugins/vwap_micro_pullback_v1.py` for a complete real
implementation to model a genuine new strategy on.

This module is never imported by anything other than tests exercising the
extension pattern itself -- it is not wired into any default registry.
"""

from strategy.interface import EvaluationResult, TradingStrategy
from strategy.status import COLLECTED


class ExampleORBStub(TradingStrategy):
    def __init__(self, strategy_id: str = "EXAMPLE_ORB_STUB_V1", version: str = "0.0.1",
                 status: str = COLLECTED):
        super().__init__(strategy_id, version, status)

    def evaluate_setup(self, bars, *, symbol: str, as_of=None) -> EvaluationResult:
        raise NotImplementedError("ExampleORBStub is a registration-pattern example only")

    def generate_entry(self, bars, *, symbol: str, as_of=None) -> EvaluationResult:
        raise NotImplementedError("ExampleORBStub is a registration-pattern example only")

    def calculate_stop(self, bars, *, entry_price: float) -> float:
        raise NotImplementedError("ExampleORBStub is a registration-pattern example only")

    def calculate_targets(self, *, entry_price: float, stop_price: float) -> dict:
        raise NotImplementedError("ExampleORBStub is a registration-pattern example only")
