"""HIGH-1: a symbol's venue is resolved, never assumed.

Oracle read-only verification found that KIS answers a WRONG-exchange
price query with success and an empty price rather than an error:

    BBVA EXCD=NAS -> rt_cd=0, last=''      (BBVA is NYSE-listed)
    BBVA EXCD=NYS -> rt_cd=0, last='27.9500'
    GFL  EXCD=NAS -> rt_cd=0, last=''
    GFL  EXCD=NYS -> rt_cd=0, last='41.3200'

so the old hardcoded `exchange="NASDAQ"` silently made every NYSE and
AMEX candidate unpriceable. These tests fix both halves: the mapping is
central and fail-closed, and an unresolved venue places no order.
"""
import pytest

from brokers import kis_broker
from brokers.kis_broker import (
    REASON_PRICE_EXCHANGE_MISMATCH_SUSPECTED,
    REASON_PRICE_FIELD_EMPTY,
    REASON_PRICE_NOT_AVAILABLE,
    REASON_PRICE_RESPONSE_MALFORMED,
    KISPriceUnavailableError,
)
from domain.exchange import (
    USExchange,
    UnsupportedExchangeError,
    normalize_exchange,
    to_kis_exchange_code,
    to_kis_order_exchange_code,
)
from market_data import exchange_registry
from market_data.exchange_registry import (
    REASON_EXCHANGE_UNKNOWN,
    REASON_UNSUPPORTED_EXCHANGE,
    ExchangeRegistry,
    ExchangeResolutionError,
)

# The two real symbols from the Oracle finding, plus their real venues.
ORACLE_UNIVERSE = (
    "symbol,name,exchange,tradable,shortable\n"
    "BBVA,Banco Bilbao,NYSE,True,True\n"
    "GFL,GFL Environmental,NYSE,True,True\n"
    "AAPL,Apple Inc,NASDAQ,True,True\n"
    "SPY,SPDR Trust,ARCA,True,True\n"
    "XYZ,Some AMEX Name,AMEX,True,True\n"
)


@pytest.fixture
def universe(tmp_path):
    path = tmp_path / "universe.csv"
    path.write_text(ORACLE_UNIVERSE, encoding="utf-8")
    return path


@pytest.fixture
def registry(universe):
    return ExchangeRegistry(universe_file=universe)


class TestExchangeCodeConversion:
    @pytest.mark.parametrize("canonical,excd,order_code", [
        (USExchange.NASDAQ, "NAS", "NASD"),
        (USExchange.NYSE, "NYS", "NYSE"),
        (USExchange.NYSE_AMERICAN, "AMS", "AMEX"),
    ])
    def test_the_three_supported_venues(self, canonical, excd, order_code):
        assert to_kis_exchange_code(canonical) == excd
        assert to_kis_order_exchange_code(canonical) == order_code

    def test_quote_and_order_vocabularies_stay_distinct(self):
        """Conflating EXCD with OVRS_EXCG_CD was a previously-fixed bug;
        keeping both tables in one module must not re-merge them."""
        for exchange in USExchange:
            quote = to_kis_exchange_code(exchange)
            order = to_kis_order_exchange_code(exchange)
            assert len(quote) == 3
            assert len(order) == 4

    @pytest.mark.parametrize("alias,expected", [
        ("NASDAQ", USExchange.NASDAQ), ("nasdaq", USExchange.NASDAQ),
        ("  NYSE  ", USExchange.NYSE), ("nyse", USExchange.NYSE),
        ("AMEX", USExchange.NYSE_AMERICAN),
        ("NYSE American", USExchange.NYSE_AMERICAN),
        ("NYSE_AMERICAN", USExchange.NYSE_AMERICAN),
    ])
    def test_aliases_normalize(self, alias, expected):
        assert normalize_exchange(alias) is expected

    @pytest.mark.parametrize("bad", [None, "", "   ", "\t", "ARCA", "BATS", "OTC",
                                     "LSE", "TSX", "unknown", 42])
    def test_everything_else_raises_rather_than_defaulting(self, bad):
        with pytest.raises(UnsupportedExchangeError):
            normalize_exchange(bad)
        with pytest.raises(UnsupportedExchangeError):
            to_kis_exchange_code(bad)
        with pytest.raises(UnsupportedExchangeError):
            to_kis_order_exchange_code(bad)

    def test_no_input_ever_silently_yields_nasdaq(self):
        """The regression that started all of this."""
        for bad in (None, "", "ARCA", "BATS", "OTC", "NOT_A_VENUE"):
            try:
                assert to_kis_exchange_code(bad) != "NAS"
            except UnsupportedExchangeError:
                pass  # the correct outcome


class TestRegistryResolution:
    def test_the_oracle_symbols_resolve_to_nyse(self, registry):
        """The exact regression: BBVA/GFL must request NYS, not NAS."""
        for symbol in ("BBVA", "GFL"):
            record = registry.resolve(symbol)
            assert record.exchange is USExchange.NYSE
            assert record.kis_exchange_code == "NYS"
            assert record.kis_order_exchange_code == "NYSE"
            assert record.source == "universe"

    def test_nasdaq_still_resolves_to_nas(self, registry):
        record = registry.resolve("AAPL")
        assert record.exchange is USExchange.NASDAQ
        assert record.kis_exchange_code == "NAS"

    def test_amex_resolves_to_ams(self, registry):
        assert registry.resolve("XYZ").kis_exchange_code == "AMS"

    def test_case_and_whitespace_insensitive(self, registry):
        assert registry.resolve("  bbva ").kis_exchange_code == "NYS"

    def test_an_unsupported_listing_is_not_defaulted(self, registry):
        """SPY is on ARCA -- a real venue this system does not trade. It
        must block, not silently become NASDAQ."""
        with pytest.raises(ExchangeResolutionError) as excinfo:
            registry.resolve("SPY")
        assert excinfo.value.reason_code == REASON_UNSUPPORTED_EXCHANGE

    def test_an_absent_symbol_is_unknown(self, registry):
        with pytest.raises(ExchangeResolutionError) as excinfo:
            registry.resolve("NOPE")
        assert excinfo.value.reason_code == REASON_EXCHANGE_UNKNOWN

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_a_blank_symbol_is_unknown(self, registry, blank):
        with pytest.raises(ExchangeResolutionError) as excinfo:
            registry.resolve(blank)
        assert excinfo.value.reason_code == REASON_EXCHANGE_UNKNOWN

    def test_missing_universe_file_blocks_rather_than_defaults(self, tmp_path):
        registry = ExchangeRegistry(universe_file=tmp_path / "absent.csv")
        with pytest.raises(ExchangeResolutionError) as excinfo:
            registry.resolve("AAPL")
        assert excinfo.value.reason_code == REASON_EXCHANGE_UNKNOWN

    def test_record_is_audit_safe(self, registry):
        payload = registry.resolve("BBVA").as_dict()
        assert payload["canonical_exchange"] == "NYSE"
        assert payload["kis_exchange_code"] == "NYS"
        assert set(payload) == {"symbol", "canonical_exchange", "kis_exchange_code",
                                "source", "verified_at"}


class TestSourcePrecedence:
    def test_universe_beats_broker_and_override(self, registry, monkeypatch):
        monkeypatch.setenv("KIS_EXCHANGE_OVERRIDES", "BBVA:NASDAQ")
        registry.record_broker_exchange("BBVA", "NASDAQ")
        record = registry.resolve("BBVA")
        assert record.source == "universe"
        assert record.exchange is USExchange.NYSE

    def test_broker_metadata_fills_a_gap(self, registry):
        assert registry.record_broker_exchange("NEWSYM", "NYSE") is True
        record = registry.resolve("NEWSYM")
        assert record.source == "broker"
        assert record.kis_exchange_code == "NYS"

    def test_an_unsupported_broker_observation_is_ignored(self, registry):
        assert registry.record_broker_exchange("NEWSYM", "ARCA") is False
        with pytest.raises(ExchangeResolutionError):
            registry.resolve("NEWSYM")

    def test_operator_override_is_last_resort(self, registry, monkeypatch):
        monkeypatch.setenv("KIS_EXCHANGE_OVERRIDES", "ONLYOVR:NYSE")
        record = registry.resolve("ONLYOVR")
        assert record.source == "operator_override"
        assert record.kis_exchange_code == "NYS"

    def test_a_malformed_override_does_not_become_a_guess(self, registry, monkeypatch):
        monkeypatch.setenv("KIS_EXCHANGE_OVERRIDES", "BAD1,BAD2:ARCA,:NYSE")
        for symbol in ("BAD1", "BAD2"):
            with pytest.raises(ExchangeResolutionError):
                registry.resolve(symbol)


class _Session:
    """Replays the exact Oracle-observed KIS behaviour."""

    OBSERVED = {
        ("BBVA", "NAS"): "", ("BBVA", "NYS"): "27.9500",
        ("GFL", "NAS"): "", ("GFL", "NYS"): "41.3200",
        ("AAPL", "NAS"): "308.9100", ("AAPL", "NYS"): "",
    }

    def __init__(self):
        self.requests = []

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.requests.append({"url": url, "params": dict(params or {})})

        class _Response:
            status_code = 200

            def __init__(self, body):
                self._body = body

            def json(self):
                return self._body

        if url.endswith("/oauth2/tokenP"):
            return _Response({"access_token": "t", "expires_in": 86400})
        symbol = (params or {}).get("SYMB")
        excd = (params or {}).get("EXCD")
        last = self.OBSERVED.get((symbol, excd), "")
        # rt_cd=0 even for the wrong exchange -- the whole point.
        return _Response({"rt_cd": "0", "msg_cd": "MCA00000", "output": {"last": last}})


@pytest.fixture
def broker(monkeypatch):
    monkeypatch.setenv("KIS_ENV", "live")
    monkeypatch.setenv("KIS_APP_KEY", "k")
    monkeypatch.setenv("KIS_APP_SECRET", "s")
    monkeypatch.setenv("KIS_ACCOUNT_NO", "12345678")
    monkeypatch.setenv("KIS_ACCOUNT_PRODUCT_CD", "01")
    monkeypatch.setenv("KIS_ACCOUNT_READ_ENABLED", "true")
    monkeypatch.setenv("KIS_LIVE_ORDER_ENABLED", "false")
    session = _Session()
    return kis_broker.KISBroker(session=session), session


class TestOracleSymbolRegression:
    def test_nyse_symbols_are_priced_when_the_venue_is_resolved(self, broker, registry):
        """The end of the regression: BBVA/GFL now get real prices."""
        from domain.instrument import build_instrument

        kis, session = broker
        for symbol, expected in (("BBVA", 27.95), ("GFL", 41.32)):
            record = registry.resolve(symbol)
            instrument = build_instrument(symbol, exchange=record.exchange.value)
            assert kis.get_current_price(instrument) == expected
        sent = [r["params"].get("EXCD") for r in session.requests if "EXCD" in r["params"]]
        assert sent == ["NYS", "NYS"], sent

    def test_the_old_hardcoded_exchange_reproduces_the_failure(self, broker):
        """Control: with NASDAQ forced, the same call still fails -- so the
        fix above is the resolution, not the harness."""
        from domain.instrument import build_instrument

        kis, _session = broker
        with pytest.raises(KISPriceUnavailableError) as excinfo:
            kis.get_current_price(build_instrument("BBVA", exchange="NASDAQ"))
        assert excinfo.value.reason_code == REASON_PRICE_EXCHANGE_MISMATCH_SUSPECTED

    def test_nasdaq_regression(self, broker, registry):
        from domain.instrument import build_instrument

        kis, session = broker
        record = registry.resolve("AAPL")
        instrument = build_instrument("AAPL", exchange=record.exchange.value)
        assert kis.get_current_price(instrument) == 308.91
        assert session.requests[-1]["params"]["EXCD"] == "NAS"


class TestEmptyPriceDiagnostics:
    def _price(self, body, exchange="NASDAQ"):
        from domain.instrument import build_instrument

        class _S:
            def request(self, method, url, headers=None, params=None, json=None, timeout=None):
                class _R:
                    status_code = 200

                    @staticmethod
                    def json():
                        return ({"access_token": "t", "expires_in": 86400}
                                if url.endswith("/oauth2/tokenP") else body)
                return _R()

        import os
        os.environ.update({"KIS_ENV": "live", "KIS_APP_KEY": "k", "KIS_APP_SECRET": "s",
                           "KIS_ACCOUNT_NO": "12345678", "KIS_ACCOUNT_READ_ENABLED": "true",
                           "KIS_LIVE_ORDER_ENABLED": "false"})
        kis = kis_broker.KISBroker(session=_S())
        return kis.get_current_price(build_instrument("BBVA", exchange=exchange))

    def test_empty_price_on_a_successful_call_suggests_the_exchange(self):
        with pytest.raises(KISPriceUnavailableError) as excinfo:
            self._price({"rt_cd": "0", "output": {"last": ""}})
        assert excinfo.value.reason_code == REASON_PRICE_EXCHANGE_MISMATCH_SUSPECTED

    def test_empty_price_on_a_non_success_call_is_just_empty(self):
        with pytest.raises(KISPriceUnavailableError) as excinfo:
            self._price({"rt_cd": "1", "output": {"last": "   "}})
        assert excinfo.value.reason_code == REASON_PRICE_FIELD_EMPTY

    def test_a_missing_field_is_malformed(self):
        with pytest.raises(KISPriceUnavailableError) as excinfo:
            self._price({"rt_cd": "0", "output": {}})
        assert excinfo.value.reason_code == REASON_PRICE_RESPONSE_MALFORMED

    def test_an_unparseable_field_is_malformed(self):
        with pytest.raises(KISPriceUnavailableError) as excinfo:
            self._price({"rt_cd": "0", "output": {"last": "not-a-number"}})
        assert excinfo.value.reason_code == REASON_PRICE_RESPONSE_MALFORMED

    def test_a_zero_price_is_not_available(self):
        with pytest.raises(KISPriceUnavailableError) as excinfo:
            self._price({"rt_cd": "0", "output": {"last": "0"}})
        assert excinfo.value.reason_code == REASON_PRICE_NOT_AVAILABLE

    def test_the_diagnostic_names_the_exchange_and_leaks_nothing(self):
        with pytest.raises(KISPriceUnavailableError) as excinfo:
            self._price({"rt_cd": "0", "output": {"last": ""}}, exchange="NASDAQ")
        payload = excinfo.value.diagnostic()
        assert payload["symbol"] == "BBVA"
        assert payload["canonical_exchange"] == "NASDAQ"
        assert payload["requested_kis_exchange_code"] == "NAS"
        assert payload["price_field"] == "last"
        assert payload["response_success_code"] == "0"
        # No account, token or raw response anywhere in it.
        blob = str(payload) + str(excinfo.value)
        for forbidden in ("12345678", "access_token", "appsecret", "Bearer"):
            assert forbidden not in blob


class TestNoProductionCallerHardcodesAnExchange:
    """Static guard: the literal that caused this must not come back."""

    def test_no_production_module_passes_a_literal_exchange(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for path in list(root.glob("*.py")) + list(root.glob("scripts/*.py")) \
                + list(root.glob("brokers/*.py")) + list(root.glob("market_data/*.py")):
            if path.name == "exchange.py":
                continue
            text = path.read_text(encoding="utf-8")
            for literal in ('exchange="NASDAQ"', "exchange='NASDAQ'"):
                if literal in text and "exchange_registry" not in path.name:
                    offenders.append(f"{path.name}: {literal}")
        assert offenders == [], offenders
