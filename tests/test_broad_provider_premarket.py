"""The broad provider serves PREMARKET/AFTER_HOURS -- and nothing else.

Two halves. The first is that the new provider normalizes a bulk SIP
response into the SAME frame the scanner already consumes: sparse
minutes stay sparse, a corrected bar replaces the one it corrects, and
an accumulating minute never reaches a range calculation.

The second half is protection. REGULAR produced every live S6 trade so
far and OVERNIGHT_DAYTIME is a KIS-native venue the US consolidated tape
does not carry at all. Both must be provably untouched by this change,
which is why those tests read the selection function and the routing
tables directly rather than trusting that nobody edited them.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from market_data import alpaca_bar_provider as abp
from market_data import alpaca_data_client as adc
from market_data.alpaca_bar_provider import AlpacaBarMarketDataProvider
from scanners.base.market_data_provider import (
    MarketDataUnavailable, UnsupportedIntervalError,
)

UTC = timezone.utc
ORIGIN = datetime(2026, 9, 15, 8, 0, tzinfo=UTC)      # 04:00 ET premarket
NOW = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


def _row(symbol, minute, *, close=100.0, volume=1000.0, n=5):
    at = ORIGIN + timedelta(minutes=minute)
    return {"symbol": symbol, "at": at, "open": close, "high": close + 0.5,
            "low": close - 0.5, "close": close, "volume": volume,
            "trade_count": n, "vwap": close}


class _FakeClient:
    """Stands in for the wire. Records exactly what was asked for."""

    def __init__(self, bars=None, raises=None):
        self._bars = bars or {}
        self._raises = raises
        self.calls = []
        self.page_count = 1
        self.request_count = 1
        self.fetch_seconds = 0.01

    def fetch_bars(self, symbols, *, start, end=None, timeframe="1Min"):
        self.calls.append({"symbols": list(symbols), "start": start,
                           "end": end, "timeframe": timeframe})
        if self._raises:
            raise self._raises
        return {s: list(rows) for s, rows in self._bars.items()
                if s in set(symbols)}

    def newest_bar_lag_seconds(self, symbol="AAPL", *, now=None,
                               lookback_minutes=90):
        return 950.0      # the measured ~15.8 min entitlement delay


def _provider(bars, *, fallback=None, now=NOW):
    return AlpacaBarMarketDataProvider(
        client=_FakeClient(bars), fallback=fallback, session="PREMARKET",
        window=(ORIGIN, now), now_fn=lambda: now)


# -- dynamic universe ----------------------------------------------------

class TestDynamicUniverse:
    """§3. No fixed count anywhere on this path."""

    @pytest.mark.parametrize("size", [1, 7, 300, 594, 601, 1500])
    def test_any_universe_size_is_requested_whole(self, size):
        symbols = [f"S{i}" for i in range(size)]
        bars = {s: [_row(s, 0)] for s in symbols}
        provider = _provider(bars)
        stats = provider.prime(symbols)
        assert stats["symbols_requested"] == size
        assert provider._client.calls[0]["symbols"] == symbols

    def test_no_padding_and_no_truncation(self):
        symbols = ["AAA", "BBB", "CCC"]
        provider = _provider({s: [_row(s, 0)] for s in symbols})
        provider.prime(symbols)
        asked = provider._client.calls[0]["symbols"]
        assert asked == symbols, "the universe is passed through verbatim"

    def test_duplicates_are_not_counted_twice(self):
        provider = _provider({"AAA": [_row("AAA", 0)]})
        stats = provider.prime(["AAA", "AAA", "AAA"])
        assert stats["symbols_requested"] == 1

    def test_no_module_hardcodes_a_universe_size(self):
        """§3's forbidden literals must not appear on this path."""
        import inspect

        for module in (abp, adc):
            source = inspect.getsource(module)
            for banned in ("top_600", "truncate_to_600", "pad_to_600"):
                assert banned not in source
            assert "= 600" not in source, "no fixed universe size"


class TestBatchingIsAWireDetail:
    """A batch boundary must not change the answer."""

    def test_batching_splits_requests_but_not_results(self, monkeypatch):
        sent = []

        class _Recorder(adc.AlpacaDataClient):
            def _get(self, params):
                sent.append(params["symbols"].split(","))
                symbols = params["symbols"].split(",")
                return {"bars": {s: [{"t": "2026-09-15T08:00:00Z", "o": 1.0,
                                      "h": 1.0, "l": 1.0, "c": 1.0, "v": 10,
                                      "n": 1, "vw": 1.0}] for s in symbols},
                        "next_page_token": None}

        client = _Recorder(key="k", secret="s", batch_size=2)
        out = client.fetch_bars([f"S{i}" for i in range(5)], start=ORIGIN)
        assert [len(b) for b in sent] == [2, 2, 1]
        assert len(out) == 5, "five symbols regardless of three batches"


# -- normalization -------------------------------------------------------

class TestNormalizedBarContract:
    """§5."""

    def test_frame_shape_matches_the_existing_contract(self):
        provider = _provider({"AAPL": [_row("AAPL", 0), _row("AAPL", 1)]})
        provider.prime(["AAPL"])
        frame = provider.get_intraday_bars("AAPL")
        assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert frame.index.name == "Datetime"
        assert isinstance(frame.index, pd.DatetimeIndex)

    def test_timestamps_are_timezone_aware_utc(self):
        provider = _provider({"AAPL": [_row("AAPL", 0)]})
        provider.prime(["AAPL"])
        frame = provider.get_intraday_bars("AAPL")
        assert frame.index.tz is not None
        assert str(frame.index.tz) in ("UTC", "utc")

    def test_bars_are_oldest_first(self):
        rows = [_row("AAPL", 3), _row("AAPL", 1), _row("AAPL", 2)]
        provider = _provider({"AAPL": rows})
        provider.prime(["AAPL"])
        frame = provider.get_intraday_bars("AAPL")
        assert list(frame.index) == sorted(frame.index)

    def test_sparse_minutes_stay_sparse(self):
        """No zero-volume filler. A minute with no print is ABSENT."""
        rows = [_row("AAPL", 0), _row("AAPL", 5), _row("AAPL", 9)]
        provider = _provider({"AAPL": rows})
        provider.prime(["AAPL"])
        frame = provider.get_intraday_bars("AAPL")
        assert len(frame) == 3, "three prints, three bars -- not ten"
        assert (frame["Volume"] > 0).all(), "no fabricated zero-volume bar"

    def test_a_correction_replaces_the_bar_it_corrects(self):
        first = _row("AAPL", 0, close=100.0, volume=1000.0)
        corrected = _row("AAPL", 0, close=101.0, volume=1200.0)
        provider = _provider({"AAPL": [first, corrected]})
        provider.prime(["AAPL"])
        frame = provider.get_intraday_bars("AAPL")
        assert len(frame) == 1, "one minute, one bar"
        assert float(frame["Close"].iloc[0]) == 101.0, "the later row wins"
        assert float(frame["Volume"].iloc[0]) == 1200.0, "volume not summed"
        assert provider.duplicate_rows_dropped == 1, "and it is counted"

    def test_the_still_accumulating_minute_is_excluded(self):
        """A partial bar feeding an opening range changes under the
        caller's feet."""
        now = ORIGIN + timedelta(minutes=5, seconds=30)
        rows = [_row("AAPL", m) for m in range(6)]     # minute 5 is current
        provider = _provider({"AAPL": rows}, now=now)
        provider.prime(["AAPL"])
        frame = provider.get_intraday_bars("AAPL")
        assert len(frame) == 5
        assert frame.index.max() < now.replace(second=0, microsecond=0)
        assert provider.partial_bars_dropped == 1

    def test_unparseable_rows_are_dropped_not_zeroed(self):
        good = {"t": "2026-09-15T08:00:00Z", "o": 1.0, "h": 1.0, "l": 1.0,
                "c": 1.0, "v": 10, "n": 1, "vw": 1.0}
        for bad in ({**good, "t": None}, {**good, "c": None},
                    {**good, "c": 0.0}, {k: v for k, v in good.items() if k != "o"}):
            assert adc._normalize_row("AAPL", bad) is None
        assert adc._normalize_row("AAPL", good) is not None


class TestCoverageIsDetectable:
    """§5: missing coverage detectable; §11: never a silent degraded mode."""

    def test_coverage_start_is_the_window_requested(self):
        """Not the first bar seen -- that is what makes 'quiet at the
        open' distinguishable from 'not watched at the open'."""
        late = [_row("AAPL", 30)]
        provider = _provider({"AAPL": late})
        provider.prime(["AAPL"])
        assert provider.coverage_started_at == ORIGIN
        assert provider.covers_origin(ORIGIN) is True

    def test_a_window_starting_after_the_origin_does_not_cover_it(self):
        provider = AlpacaBarMarketDataProvider(
            client=_FakeClient({"AAPL": [_row("AAPL", 30)]}),
            session="PREMARKET", window=(ORIGIN + timedelta(minutes=10), NOW),
            now_fn=lambda: NOW)
        provider.prime(["AAPL"])
        assert provider.covers_origin(ORIGIN) is False

    def test_a_quiet_symbol_is_unavailable_not_empty(self):
        provider = _provider({"AAPL": [_row("AAPL", 0)]})
        provider.prime(["AAPL", "QUIET"])
        with pytest.raises(MarketDataUnavailable):
            provider.get_intraday_bars("QUIET")

    def test_asking_before_priming_is_refused(self):
        provider = _provider({"AAPL": [_row("AAPL", 0)]})
        with pytest.raises(MarketDataUnavailable) as caught:
            provider.get_intraday_bars("AAPL")
        assert "not primed" in str(caught.value)

    def test_an_unsupported_interval_is_a_wiring_fault(self):
        provider = _provider({"AAPL": [_row("AAPL", 0)]})
        provider.prime(["AAPL"])
        with pytest.raises(UnsupportedIntervalError):
            provider.get_intraday_bars("AAPL", interval="5m")

    def test_priming_without_a_window_is_refused(self):
        provider = AlpacaBarMarketDataProvider(
            client=_FakeClient({}), session="PREMARKET")
        with pytest.raises(MarketDataUnavailable):
            provider.prime(["AAPL"])


class TestProviderFailureIsolation:
    """§11. A provider failure must not become a candidate decision."""

    def test_a_client_error_propagates_rather_than_returning_nothing(self):
        provider = AlpacaBarMarketDataProvider(
            client=_FakeClient(raises=adc.AlpacaDataError("boom")),
            session="PREMARKET", window=(ORIGIN, NOW), now_fn=lambda: NOW)
        with pytest.raises(adc.AlpacaDataError):
            provider.prime(["AAPL"])
        assert provider._primed is False, "a failed prime serves nothing"

    def test_an_entitlement_failure_is_its_own_type(self):
        assert issubclass(adc.RecentDataUnavailable, adc.EntitlementError)
        assert issubclass(adc.EntitlementError, adc.AlpacaDataError)

    def test_missing_credentials_are_named(self):
        with pytest.raises(adc.CredentialsMissing):
            adc.credentials(env={})


class TestFreshnessIsStatedNotAssumed:
    """§9. The entitlement delay must be visible in code, not prose."""

    def test_the_measured_delay_fails_the_freshness_check(self):
        provider = _provider({"AAPL": [_row("AAPL", 0)]})
        ok, lag = provider.is_fresh_enough()
        assert ok is False
        assert lag == pytest.approx(950.0)

    def test_no_bars_is_not_fresh(self):
        class _Empty(_FakeClient):
            def newest_bar_lag_seconds(self, *a, **k):
                return None

        provider = AlpacaBarMarketDataProvider(
            client=_Empty({}), session="PREMARKET", window=(ORIGIN, NOW))
        ok, lag = provider.is_fresh_enough()
        assert ok is False and lag is None

    def test_the_module_declares_itself_shadow_only(self):
        assert abp.SHADOW_ONLY is True


class TestFeedIsNeverSubstituted:
    """§4. IEX must not stand in for SIP."""

    def test_the_feed_is_sip_and_is_not_a_parameter(self):
        import inspect

        assert adc.FEED == "sip"
        source = inspect.getsource(adc.AlpacaDataClient._fetch_batch)
        assert '"feed": FEED' in source
        signature = inspect.signature(adc.AlpacaDataClient.fetch_bars)
        assert "feed" not in signature.parameters, (
            "a feed parameter is how IEX silently substitutes for SIP")

    def test_the_declared_feed_name_matches_what_is_requested(self):
        assert AlpacaBarMarketDataProvider.feed_name == adc.FEED


class TestDailyBarsAreDelegated:
    def test_daily_comes_from_the_fallback_untouched(self):
        class _Fallback:
            def __init__(self):
                self.calls = []

            def get_daily_bars(self, symbol, lookback_days=400):
                self.calls.append((symbol, lookback_days))
                return pd.DataFrame({"Close": [1.0]})

        fallback = _Fallback()
        provider = _provider({}, fallback=fallback)
        provider.get_daily_bars("AAPL", lookback_days=400)
        assert fallback.calls == [("AAPL", 400)]

    def test_no_daily_provider_is_named(self):
        provider = _provider({})
        with pytest.raises(MarketDataUnavailable):
            provider.get_daily_bars("AAPL")


# -- §13 / §14 protection ------------------------------------------------

class TestRegularIsUntouched:
    """§13. REGULAR keeps the Yahoo bulk/cache provider."""

    def test_regular_gets_no_broad_provider(self):
        assert abp.broad_provider_for_session("REGULAR") is None

    def test_regular_is_not_in_the_broad_session_set(self):
        assert "REGULAR" not in abp.BROAD_PROVIDER_SESSIONS

    def test_regular_still_resolves_to_the_caching_yahoo_provider(self):
        from scanners.base.market_data_provider import (
            CachingMarketDataProvider, YahooFinanceMarketDataProvider,
            default_provider,
        )
        from market_data.kis_bar_provider import provider_for_session

        chosen = provider_for_session("REGULAR", broker=object())
        assert isinstance(chosen, CachingMarketDataProvider)
        assert isinstance(default_provider(), CachingMarketDataProvider)
        assert isinstance(chosen._inner, YahooFinanceMarketDataProvider)

    def test_the_kis_authoritative_set_is_unchanged(self):
        from market_data.kis_bar_provider import KIS_AUTHORITATIVE_SESSIONS

        assert KIS_AUTHORITATIVE_SESSIONS == frozenset(
            {"PREMARKET", "AFTER_HOURS", "OVERNIGHT_DAYTIME"})

    def test_production_selection_still_ignores_the_broad_provider(self):
        """The decisive one: `run_scanners` must not reach this module."""
        import inspect

        from scripts import run_scanners

        source = inspect.getsource(run_scanners.session_provider)
        assert "alpaca" not in source.lower()
        assert "broad_provider_for_session" not in source


class TestDaytimeIsUntouched:
    """§14. 미국주간거래 stays KIS-native."""

    def test_daytime_gets_no_broad_provider(self):
        assert abp.broad_provider_for_session("OVERNIGHT_DAYTIME") is None
        assert "OVERNIGHT_DAYTIME" not in abp.BROAD_PROVIDER_SESSIONS

    def test_daytime_venue_codes_are_unchanged(self):
        from market_data import kis_hdfscnt0 as wire
        from market_data import kis_minute_chart as mc

        assert set(wire.DAYTIME_PREFIX.values()) == {"RBAQ", "RBAY", "RBAA"}
        for exchange, code in (("NASDAQ", "BAQ"), ("NYSE", "BAY"),
                               ("AMEX", "BAA")):
            assert mc.excd_for_session(exchange, "OVERNIGHT_DAYTIME") == code

    def test_daytime_origin_is_unchanged(self):
        from config.kis_market_schedule import daytime_open_time

        assert daytime_open_time() is not None

    def test_no_daytime_venue_code_is_reachable_from_the_broad_path(self):
        """Checked against EXECUTABLE code, not prose.

        The modules discuss the daytime venue in comments -- explaining
        why it is excluded is the point. What must not exist is a string
        literal routing to it, which is what a real leak would look like.
        """
        import ast
        import inspect

        for module in (abp, adc):
            tree = ast.parse(inspect.getsource(module))
            literals = {node.value for node in ast.walk(tree)
                        if isinstance(node, ast.Constant)
                        and isinstance(node.value, str)}
            for token in ("RBAQ", "RBAY", "RBAA", "BAQ", "BAY", "BAA"):
                assert not any(token == text or token in text.split()
                               for text in literals), (
                    f"{token} appears as a routing literal in {module.__name__}")


class TestExplicitSessionSelection:
    """§6."""

    @pytest.mark.parametrize("session", ["PREMARKET", "AFTER_HOURS"])
    def test_the_two_target_sessions_get_the_broad_provider(self, session):
        provider = abp.broad_provider_for_session(
            session, client=_FakeClient({}), window=(ORIGIN, NOW))
        assert isinstance(provider, AlpacaBarMarketDataProvider)
        assert provider._session == session

    @pytest.mark.parametrize("session", ["REGULAR", "OVERNIGHT_DAYTIME",
                                         "CLOSED", "", None, "NONSENSE"])
    def test_every_other_session_is_refused(self, session):
        assert abp.broad_provider_for_session(
            session, client=_FakeClient({})) is None

    def test_selection_is_case_insensitive_but_not_loose(self):
        assert abp.broad_provider_for_session(
            "premarket", client=_FakeClient({}), window=(ORIGIN, NOW)) is not None
        assert abp.broad_provider_for_session(
            "pre", client=_FakeClient({})) is None


class TestShadowHarnessCannotTrade:
    """§7/§11: a shadow that could reach the entry path is not a shadow."""

    def test_the_harness_uses_evaluate_not_evaluate_into(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1]
                  / "scripts" / "run_shadow_parity.py").read_text()
        assert "scanner.evaluate(" in source
        assert "evaluate_into" not in source.split('"""', 2)[2], (
            "evaluate_into reaches _admit_s6_pass_provisionally")

    def test_the_harness_never_publishes_or_orders(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1]
                  / "scripts" / "run_shadow_parity.py").read_text()
        body = source.split('"""', 2)[2]
        for banned in ("publish_report_candidates", "submit_order",
                       "submit_buy_order", "active_watch", "_admit_s6"):
            assert banned not in body
