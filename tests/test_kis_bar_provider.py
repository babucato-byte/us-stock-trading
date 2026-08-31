"""The scanner's market data, from KIS instead of yfinance.

S6's premarket scan on 2026-08-31: universe 83, DATA_ERROR 77, evaluated
6, signals 0. Ninety-three percent of candidates died before any strategy
rule ran, and it read as a quiet market for weeks. KIS has the data --
measured live, RIG 12 premarket bars / 1,708 shares, AAPL 66 / 43,442.
"""

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_data.kis_bar_provider import (  # noqa: E402
    KIS_AUTHORITATIVE_SESSIONS, KISBarMarketDataProvider, provider_for_session,
)
from scanners.base.market_data_provider import (  # noqa: E402
    BarMarketDataProvider, MarketDataUnavailable,
)

ET = ZoneInfo("America/New_York")


class _Broker:
    """Answers the chart endpoint with premarket-shaped rows."""

    def __init__(self, rows=None, fail=False):
        self._rows = rows
        self._fail = fail

    class config:
        @staticmethod
        def validate_read_allowed():
            return True

    def _get(self, *a, **k):
        if self._fail:
            raise RuntimeError("KIS unreachable")
        return {"rt_cd": "0", "output2": self._rows or []}


def _row(hms, close, volume="100"):
    return {"xymd": "20260831", "xhms": hms, "open": close, "high": close,
            "low": close, "last": close, "evol": volume, "eamt": "1000"}


def _provider(rows=None, fail=False, fallback=None):
    return KISBarMarketDataProvider(
        broker=_Broker(rows, fail), fallback=fallback,
        exchange_for=lambda s: "NASDAQ")


class TestItSatisfiesTheProviderContract:
    def test_it_is_a_bar_market_data_provider(self):
        assert isinstance(_provider(), BarMarketDataProvider)

    def test_it_names_itself_distinctly(self):
        """Month 2 cannot tell two vendors' results apart from a provider
        called 'base'."""
        assert _provider().provider_name == "kis"
        assert _provider().provider_name != "yfinance"

    def test_it_reports_no_feed_rather_than_guessing_one(self):
        """A fabricated feed name is worse than a null: a null is
        visibly unknown."""
        assert _provider().feed_name is None


class TestIntradayBars:
    def test_premarket_bars_come_back_oldest_first(self):
        frame = _provider([_row("050200", "12.00"),
                           _row("050100", "11.00")]).get_intraday_bars("AAPL")
        assert list(frame["Close"]) == [11.0, 12.0]
        assert list(frame.index) == sorted(frame.index)

    def test_volume_survives(self):
        """The thing yfinance could not give for premarket."""
        frame = _provider([_row("050200", "12.00", volume="79")]
                          ).get_intraday_bars("AAPL")
        assert float(frame["Volume"].iloc[0]) == pytest.approx(79.0)

    def test_the_frame_has_the_columns_the_scanner_reads(self):
        frame = _provider([_row("050200", "12.00")]).get_intraday_bars("AAPL")
        for column in ("Open", "High", "Low", "Close", "Volume"):
            assert column in frame.columns

    def test_no_bars_raises_unavailable_not_a_silent_empty(self):
        with pytest.raises(MarketDataUnavailable):
            _provider([]).get_intraday_bars("AAPL")

    def test_a_broker_failure_raises_unavailable(self):
        with pytest.raises(MarketDataUnavailable):
            _provider(fail=True).get_intraday_bars("AAPL")

    def test_a_symbol_with_no_exchange_mapping_is_refused(self):
        provider = KISBarMarketDataProvider(broker=_Broker([_row("050200", "1")]),
                                            exchange_for=lambda s: None)
        with pytest.raises(MarketDataUnavailable, match="exchange"):
            provider.get_intraday_bars("NOPE")

    def test_an_unsupported_interval_is_refused_not_resampled(self):
        """Resampling would silently serve a different upstream than the
        caller asked for."""
        with pytest.raises(MarketDataUnavailable, match="1m"):
            _provider([_row("050200", "1")]).get_intraday_bars("AAPL",
                                                               interval="5m")

    def test_the_latest_price_is_the_last_close(self):
        assert _provider([_row("050200", "12.00"), _row("050100", "11.00")]
                         ).get_latest_price("AAPL") == pytest.approx(12.0)

    def test_the_latest_price_is_None_when_unavailable(self):
        assert _provider([]).get_latest_price("AAPL") is None


class TestDailyBarsGoToTheFallback:
    """KIS's daily endpoint returns 100 rows. Serving a truncated history
    where 400 days were asked for would be a quiet second change."""

    def test_daily_is_delegated(self):
        seen = {}

        class _Fallback:
            def get_daily_bars(self, symbol, lookback_days=400):
                seen["symbol"] = symbol
                seen["lookback"] = lookback_days
                return pd.DataFrame({"Close": [1.0]})

        _provider(fallback=_Fallback()).get_daily_bars("AAPL", lookback_days=400)
        assert seen == {"symbol": "AAPL", "lookback": 400}

    def test_no_fallback_is_an_explicit_refusal(self):
        with pytest.raises(MarketDataUnavailable, match="daily"):
            _provider().get_daily_bars("AAPL")


class TestWhichSessionsKISAnswersFor:
    @pytest.mark.parametrize("session", sorted(KIS_AUTHORITATIVE_SESSIONS))
    def test_the_extended_sessions_use_KIS(self, session):
        provider = provider_for_session(session, broker=_Broker([]),
                                        fallback=object())
        assert isinstance(provider, KISBarMarketDataProvider)

    def test_regular_is_left_alone(self):
        """Not because KIS would be worse -- because REGULAR works, and
        produced every live S6 trade so far."""
        sentinel = object()
        assert provider_for_session("REGULAR", broker=_Broker([]),
                                    fallback=sentinel) is sentinel

    def test_regular_is_deliberately_not_in_the_set(self):
        assert "REGULAR" not in KIS_AUTHORITATIVE_SESSIONS

    def test_no_broker_falls_back_rather_than_failing(self, caplog):
        sentinel = object()
        with caplog.at_level("WARNING"):
            got = provider_for_session("PREMARKET", broker=None,
                                       fallback=sentinel)
        assert got is sentinel
        assert "extended-hours" in caplog.text

    def test_an_unknown_session_falls_back(self):
        sentinel = object()
        assert provider_for_session(None, broker=_Broker([]),
                                    fallback=sentinel) is sentinel


class TestItChangesNoStrategyRule:
    def test_the_provider_touches_no_threshold(self):
        source = (REPO_ROOT / "market_data" / "kis_bar_provider.py").read_text()
        for forbidden in ("volume_expansion", "orb_minutes", "threshold",
                          "submit_buy", "order_gate"):
            assert forbidden not in source, forbidden


class TestTheScannerActuallyUsesIt:
    """A provider nothing calls fixes nothing. The runner previously did
    `default_provider(cached=False)` unconditionally, so every session
    got yfinance -- including the ones where 77 of 83 candidates were
    dying on missing data.

    Selection lives in the ENTRYPOINT, not in `scanners/`. Choosing it
    inside the package would mean the package importing a broker, and
    `test_scanner_trading_isolation` forbids that: an import that does
    not exist cannot be reached by a path nobody thought of.
    """

    def _entrypoint(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "run_scanners_entry", REPO_ROOT / "scripts" / "run_scanners.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_scanner_package_imports_no_broker(self):
        """The invariant this had to be shaped around."""
        source = (REPO_ROOT / "scanners" / "runner.py").read_text()
        assert "brokers" not in source
        assert "KISBroker" not in source

    def test_the_runner_accepts_an_injected_provider(self):
        import inspect

        from scanners import runner

        assert "provider" in inspect.signature(runner.main).parameters

    def test_the_entrypoint_passes_one_in(self):
        source = (REPO_ROOT / "scripts" / "run_scanners.py").read_text()
        assert "main(provider=session_provider())" in source

    def test_an_extended_session_gets_the_KIS_provider(self, monkeypatch):
        entry = self._entrypoint()
        from scanners.base import scan_session

        monkeypatch.setattr(scan_session, "session_at",
                            lambda *a, **k: "PREMARKET")
        monkeypatch.setattr("brokers.kis_broker.KISBroker",
                            lambda *a, **k: _Broker([]))
        assert isinstance(entry.session_provider(), KISBarMarketDataProvider)

    def test_regular_gets_no_override(self, monkeypatch):
        """None means "use the runner's own default" -- the path that
        works and produced every live S6 trade."""
        entry = self._entrypoint()
        from scanners.base import scan_session

        monkeypatch.setattr(scan_session, "session_at",
                            lambda *a, **k: "REGULAR")
        assert entry.session_provider() is None

    def test_a_broker_that_cannot_be_built_falls_back(self, monkeypatch):
        """A scan running on the previous provider is what existed
        before; a scan that cannot start is strictly worse."""
        entry = self._entrypoint()
        from scanners.base import scan_session

        monkeypatch.setattr(scan_session, "session_at",
                            lambda *a, **k: "PREMARKET")

        def _boom(*a, **k):
            raise RuntimeError("no credentials")

        monkeypatch.setattr("brokers.kis_broker.KISBroker", _boom)
        assert entry.session_provider() is None

    def test_a_broken_session_lookup_falls_back(self, monkeypatch):
        entry = self._entrypoint()
        from scanners.base import scan_session

        monkeypatch.setattr(
            scan_session, "session_at",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("clock")))
        assert entry.session_provider() is None
