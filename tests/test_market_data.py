from datetime import datetime, timezone

import pandas as pd
import pytest

from brokers.kis_broker import KISBrokerError
from domain.instrument import build_instrument
from market_data.alpaca_provider import AlpacaMarketDataProvider
from market_data.base import MarketDataProviderError
from market_data.kis_validation_provider import (
    KISValidationProvider,
    compute_price_deviation_percent,
)

NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)


class _FakeTicker:
    def __init__(self, history_df):
        self._history_df = history_df

    def history(self, period="5d"):
        return self._history_df


class TestAlpacaMarketDataProvider:
    def test_success(self, monkeypatch):
        df = pd.DataFrame({"Close": [100.0, 101.0, 102.5]})
        monkeypatch.setattr("yfinance.Ticker", lambda symbol: _FakeTicker(df))
        provider = AlpacaMarketDataProvider(now_fn=lambda: NOW)
        quote = provider.get_price_quote("AAPL")
        assert quote.price_usd == pytest.approx(102.5)
        assert quote.source == "alpaca_data_yfinance"

    def test_empty_history_raises(self, monkeypatch):
        monkeypatch.setattr("yfinance.Ticker", lambda symbol: _FakeTicker(pd.DataFrame()))
        provider = AlpacaMarketDataProvider(now_fn=lambda: NOW)
        with pytest.raises(MarketDataProviderError):
            provider.get_price_quote("AAPL")

    def test_non_positive_price_raises(self, monkeypatch):
        df = pd.DataFrame({"Close": [0.0]})
        monkeypatch.setattr("yfinance.Ticker", lambda symbol: _FakeTicker(df))
        provider = AlpacaMarketDataProvider(now_fn=lambda: NOW)
        with pytest.raises(MarketDataProviderError):
            provider.get_price_quote("AAPL")

    def test_ticker_exception_raises_market_data_error(self, monkeypatch):
        def _raise(symbol):
            raise RuntimeError("network down")
        monkeypatch.setattr("yfinance.Ticker", _raise)
        provider = AlpacaMarketDataProvider(now_fn=lambda: NOW)
        with pytest.raises(MarketDataProviderError):
            provider.get_price_quote("AAPL")


class _FakeKISBroker:
    def __init__(self, price=None, raise_exc=None):
        self.price = price
        self.raise_exc = raise_exc
        self.calls = []

    def get_current_price(self, instrument):
        self.calls.append(instrument)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.price


class TestKISValidationProvider:
    def test_success(self):
        broker = _FakeKISBroker(price=101.5)
        provider = KISValidationProvider(broker, instrument_lookup=lambda s: build_instrument(s, exchange="NASDAQ"))
        quote = provider.get_price_quote("AAPL")
        assert quote.price_usd == 101.5
        assert quote.source == "kis_price_quote"
        assert len(broker.calls) == 1

    def test_broker_error_wrapped_as_market_data_error(self):
        broker = _FakeKISBroker(raise_exc=KISBrokerError("boom"))
        provider = KISValidationProvider(broker, instrument_lookup=lambda s: build_instrument(s, exchange="NASDAQ"))
        with pytest.raises(MarketDataProviderError):
            provider.get_price_quote("AAPL")


class TestComputePriceDeviationPercent:
    def test_zero_deviation(self):
        assert compute_price_deviation_percent(100.0, 100.0) == 0.0

    def test_positive_deviation(self):
        assert compute_price_deviation_percent(100.0, 100.30) == pytest.approx(0.30)

    def test_negative_direction_deviation_is_absolute(self):
        assert compute_price_deviation_percent(100.0, 99.70) == pytest.approx(0.30)

    @pytest.mark.parametrize("signal_price", [0, -1.0, float("nan"), float("inf")])
    def test_invalid_signal_price_raises(self, signal_price):
        with pytest.raises(MarketDataProviderError):
            compute_price_deviation_percent(signal_price, 100.0)

    @pytest.mark.parametrize("kis_price", [0, -1.0, float("nan"), float("inf")])
    def test_invalid_kis_price_raises(self, kis_price):
        with pytest.raises(MarketDataProviderError):
            compute_price_deviation_percent(100.0, kis_price)
