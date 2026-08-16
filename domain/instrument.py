"""`Instrument` -- the single broker-agnostic identity for a tradable
symbol. Ticker/exchange normalization (e.g. mapping Alpaca's plain
`AAPL` to whatever exchange-qualified form KIS's overseas-order API
requires) happens in `market_data/` and `brokers/kis_broker.py`
respectively; this dataclass is just the resulting, already-normalized
record both sides agree on.

`leveraged`/`inverse`/`otc` are explicit booleans (not inferred from the
symbol string) so `execution/order_gate.py` can reject them outright per
spec: leveraged/inverse/OTC instruments are never eligible for a live
order, regardless of any other signal/risk/sizing result.
"""

from dataclasses import dataclass
from typing import Optional


class InstrumentError(Exception):
    """Raised when an Instrument cannot be safely constructed. Callers
    must treat this as a hard block -- there is no fallback identity."""


@dataclass(frozen=True)
class Instrument:
    symbol: str
    normalized_symbol: str
    alpaca_symbol: str
    kis_symbol: str
    exchange: str
    currency: str
    asset_type: str
    tradable: bool
    fractionable: bool
    leveraged: bool
    inverse: bool
    otc: bool

    def __post_init__(self):
        for field_name in ("symbol", "normalized_symbol", "alpaca_symbol",
                           "kis_symbol", "exchange", "currency", "asset_type"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise InstrumentError(f"{field_name} must be a non-empty string, got {value!r}")
        for field_name in ("tradable", "fractionable", "leveraged", "inverse", "otc"):
            value = getattr(self, field_name)
            if not isinstance(value, bool):
                raise InstrumentError(f"{field_name} must be a bool, got {value!r}")

    @property
    def is_order_eligible(self):
        """A pure identity-level eligibility check -- NOT the full
        order_gate.py safety gate (which also checks live balance,
        reconciliation state, allow-list, etc). This only encodes facts
        about the instrument itself: never leveraged/inverse/OTC, and
        the broker must consider it tradable at all."""
        return self.tradable and not self.leveraged and not self.inverse and not self.otc


def normalized_symbol(raw_symbol: str) -> str:
    """The one place symbol normalization happens (uppercase, stripped)
    -- both `alpaca_symbol` and `kis_symbol` are derived from this before
    each adapter applies its own wire-format quirks."""
    if not isinstance(raw_symbol, str) or not raw_symbol.strip():
        raise InstrumentError(f"raw symbol must be a non-empty string, got {raw_symbol!r}")
    return raw_symbol.strip().upper()


def build_instrument(
    raw_symbol: str, *, exchange: str, currency: str = "USD", asset_type: str = "us_equity",
    tradable: bool = True, fractionable: bool = False, leveraged: bool = False,
    inverse: bool = False, otc: bool = False, kis_symbol: Optional[str] = None,
) -> Instrument:
    """Builds an `Instrument` from a raw ticker. `kis_symbol` defaults to
    the same normalized symbol as `alpaca_symbol` -- KIS's overseas-order
    API uses plain US tickers too, but the parameter exists so a future
    exchange-suffix quirk can be handled without changing every caller."""
    norm = normalized_symbol(raw_symbol)
    return Instrument(
        symbol=raw_symbol, normalized_symbol=norm, alpaca_symbol=norm,
        kis_symbol=kis_symbol or norm, exchange=exchange, currency=currency,
        asset_type=asset_type, tradable=tradable, fractionable=fractionable,
        leveraged=leveraged, inverse=inverse, otc=otc,
    )
