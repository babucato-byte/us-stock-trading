"""The KIS session provider serves collected trades before it asks the chart.

Why: every daytime scan on 2026-08-31..09-04 rejected 100% of symbols
with DATA_ERROR because no overnight bar reached the scanner, while the
per-symbol chart reads cost 3 s each and made a 593-symbol scan take an
hour. Bars the collector has ALREADY accumulated for the session are
the cheapest and freshest source there is; the chart is the fallback
for symbols the collector is not subscribed to.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_data import kis_bar_provider as kbp  # noqa: E402
from market_data import realtime_bars as rb  # noqa: E402
from scanners.base.market_data_provider import MarketDataUnavailable  # noqa: E402

SESSION = "OVERNIGHT_DAYTIME"
T0 = datetime(2026, 9, 9, 1, 30, tzinfo=timezone.utc)


def _bar(symbol, minute, close, volume=100.0):
    return rb.Bar(symbol=symbol, session=SESSION, minute=minute, open=close,
                  high=close + 0.1, low=close - 0.1, close=close, volume=volume,
                  trade_count=3, first_trade_at=minute, last_trade_at=minute)


class _Store:
    def __init__(self, bars):
        self._bars = bars

    def bars(self, symbol, session):
        return [b for b in self._bars if b.symbol == symbol and b.session == session]


class _ChartBroker:
    """A broker whose chart read is counted; the probe must not reach it
    for symbols the store already covers."""

    def __init__(self):
        self.reads = 0

    class config:
        @staticmethod
        def validate_read_allowed():
            return True

    def _get(self, *a, **k):
        self.reads += 1
        return {"rt_cd": "0", "output2": []}


def _provider(store, broker=None):
    return kbp.KISBarMarketDataProvider(
        broker=broker or _ChartBroker(), fallback=None, session=SESSION,
        store_loader=lambda: store, exchange_for=lambda s: "NASDAQ")


class TestRealtimeFirst:
    def test_collected_bars_are_served_without_a_chart_read(self):
        store = _Store([_bar("AAPL", T0, 320.0), _bar("AAPL", T0 + timedelta(minutes=1), 320.5)])
        broker = _ChartBroker()
        provider = _provider(store, broker)
        frame = provider.get_intraday_bars("AAPL", interval="1m")
        assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert len(frame) == 2 and frame.index.tz is not None
        assert list(frame["Close"]) == [320.0, 320.5]
        assert broker.reads == 0
        assert provider.realtime_hits == 1 and provider.chart_reads == 0

    def test_bars_come_back_oldest_first(self):
        later, earlier = T0 + timedelta(minutes=5), T0
        store = _Store([_bar("NVDA", later, 231.0), _bar("NVDA", earlier, 230.0)])
        # The store orders its own bars; a provider must not reorder them.
        store._bars.sort(key=lambda b: b.minute)
        frame = _provider(store).get_intraday_bars("NVDA")
        assert frame.index[0] < frame.index[-1]

    def test_a_symbol_the_collector_lacks_falls_through_to_the_chart(self, monkeypatch):
        store = _Store([_bar("AAPL", T0, 320.0)])
        broker = _ChartBroker()
        provider = _provider(store, broker)
        from market_data import kis_minute_chart

        monkeypatch.setattr(kis_minute_chart, "fetch", lambda *a, **k: [
            {"at": T0, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}])
        frame = provider.get_intraday_bars("F")
        assert len(frame) == 1
        assert provider.chart_reads == 1 and provider.realtime_hits == 0

    def test_no_store_means_the_chart_as_before(self, monkeypatch):
        provider = kbp.KISBarMarketDataProvider(
            broker=_ChartBroker(), fallback=None, session=SESSION,
            store_loader=lambda: None, exchange_for=lambda s: "NASDAQ")
        from market_data import kis_minute_chart

        monkeypatch.setattr(kis_minute_chart, "fetch", lambda *a, **k: [])
        with pytest.raises(MarketDataUnavailable):
            provider.get_intraday_bars("AAPL")
        assert provider.chart_reads == 1

    def test_a_broken_store_loader_is_no_store(self, monkeypatch):
        def boom():
            raise RuntimeError("disk")

        provider = kbp.KISBarMarketDataProvider(
            broker=_ChartBroker(), fallback=None, session=SESSION,
            store_loader=boom, exchange_for=lambda s: "NASDAQ")
        from market_data import kis_minute_chart

        monkeypatch.setattr(kis_minute_chart, "fetch", lambda *a, **k: [
            {"at": T0, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}])
        assert len(provider.get_intraday_bars("AAPL")) == 1

    def test_the_store_is_loaded_once_per_provider(self):
        calls = []

        def loader():
            calls.append(1)
            return _Store([_bar("AAPL", T0, 1.0)])

        provider = kbp.KISBarMarketDataProvider(
            broker=_ChartBroker(), fallback=None, session=SESSION,
            store_loader=loader, exchange_for=lambda s: "NASDAQ")
        provider.get_intraday_bars("AAPL")
        provider.get_intraday_bars("AAPL")
        assert calls == [1]

    def test_the_session_provider_is_realtime_first_for_kis_sessions(self):
        class _B:
            pass

        provider = kbp.provider_for_session(SESSION, broker=_B(), fallback=object())
        assert isinstance(provider, kbp.KISBarMarketDataProvider)
        assert provider._session == SESSION
        assert provider._store_loader is not None

    def test_regular_keeps_the_fallback(self):
        sentinel = object()
        assert kbp.provider_for_session("REGULAR", broker=object(), fallback=sentinel) is sentinel
