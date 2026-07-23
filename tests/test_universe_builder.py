import pandas as pd
import pytest

import universe_builder
from broker import AlpacaBroker, BrokerConfig


class DummyGetSession:
    """Session double whose .request() call is asserted never to happen."""

    def __init__(self):
        self.requests = []

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))
        raise AssertionError("Network GET should not be issued")


class RecordingAssetsSession:
    """Session double that returns a canned assets list and records calls."""

    class _Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return [
                {"symbol": "AAPL", "name": "Apple", "exchange": "NASDAQ",
                 "status": "active", "tradable": True, "shortable": True, "class": "us_equity"},
                {"symbol": "MSFT", "name": "Microsoft", "exchange": "NASDAQ",
                 "status": "active", "tradable": True, "shortable": True, "class": "us_equity"},
                # Filtered out: inactive
                {"symbol": "ZZZZ", "name": "Delisted", "exchange": "OTC",
                 "status": "inactive", "tradable": False, "shortable": False, "class": "us_equity"},
                # Filtered out: not us_equity
                {"symbol": "BTCUSD", "name": "Bitcoin", "exchange": "CRYPTO",
                 "status": "active", "tradable": True, "shortable": False, "class": "crypto"},
            ]

    def __init__(self):
        self.requests = []

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))
        return self._Response()


def test_paper_endpoint_allows_get_assets_once(monkeypatch):
    # CODEX-018: the common gate now re-reads current environment
    # credentials on every request and requires them to match self.config's
    # captured values, so this success-path test must set matching env vars.
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    config = BrokerConfig(trading_mode="paper", api_key="key", secret_key="secret")
    session = RecordingAssetsSession()
    broker = AlpacaBroker(config=config, session=session)

    rows = universe_builder.fetch_active_us_equity_rows(broker=broker)

    assert len(session.requests) == 1
    assert {r["symbol"] for r in rows} == {"AAPL", "MSFT"}  # inactive/non-equity filtered out


def test_live_endpoint_blocks_get_assets():
    config = BrokerConfig(
        trading_mode="live", enable_real_trading=True, live_dry_run=False,
        api_key="key", secret_key="secret",
    )
    session = DummyGetSession()
    broker = AlpacaBroker(config=config, session=session)

    with pytest.raises(RuntimeError):
        universe_builder.fetch_active_us_equity_rows(broker=broker)

    assert session.requests == []


@pytest.mark.parametrize(
    "paper_base_url",
    [
        "https://not-alpaca.example.com",
        "http://paper-api.alpaca.markets",  # scheme downgrade
        "https://paper-api.alpaca.markets.evil.com",  # lookalike hostname
        "https://paper-api.alpaca.markets:8443",  # non-standard port
        "https://paper-api.alpaca.markets/../v2/assets",  # trailing path trick
        "https://user:pass@paper-api.alpaca.markets",  # userinfo
        "https://paper-api.alpaca.markets?x=https://api.alpaca.markets",  # query manipulation
        "",
        "   ",
    ],
)
def test_arbitrary_or_tampered_endpoint_blocks_get_assets(paper_base_url):
    config = BrokerConfig(
        trading_mode="paper", paper_base_url=paper_base_url, api_key="key", secret_key="secret",
    )
    session = DummyGetSession()
    broker = AlpacaBroker(config=config, session=session)

    with pytest.raises(RuntimeError):
        universe_builder.fetch_active_us_equity_rows(broker=broker)

    assert session.requests == []


def test_invalid_trading_mode_blocks_get_assets():
    config = BrokerConfig(trading_mode="papre", api_key="key", secret_key="secret")
    session = DummyGetSession()
    broker = AlpacaBroker(config=config, session=session)

    with pytest.raises(RuntimeError):
        universe_builder.fetch_active_us_equity_rows(broker=broker)

    assert session.requests == []


def test_tampered_config_after_construction_blocks_get_assets():
    broker = AlpacaBroker(
        config=BrokerConfig(trading_mode="paper", api_key="key", secret_key="secret"),
        session=DummyGetSession(),
    )
    broker.config = BrokerConfig(
        trading_mode="paper", paper_base_url="https://api.alpaca.markets",
        api_key="key", secret_key="secret",
    )

    with pytest.raises(RuntimeError):
        universe_builder.fetch_active_us_equity_rows(broker=broker)


def test_build_universe_writes_expected_csv(tmp_path, monkeypatch):
    # CODEX-018: the common gate now re-reads current environment
    # credentials on every request and requires them to match self.config's
    # captured values, so this success-path test must set matching env vars.
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    config = BrokerConfig(trading_mode="paper", api_key="key", secret_key="secret")
    broker = AlpacaBroker(config=config, session=RecordingAssetsSession())
    output_path = tmp_path / "universe.csv"

    df = universe_builder.build_universe(broker=broker, output_path=output_path)

    assert set(df["symbol"]) == {"AAPL", "MSFT"}
    on_disk = pd.read_csv(output_path)
    assert set(on_disk["symbol"]) == {"AAPL", "MSFT"}
    assert list(df.columns) == ["symbol", "name", "exchange", "tradable", "shortable"]


def test_env_var_fallback_never_resolves_to_live_url(monkeypatch):
    # No ALPACA_PAPER_BASE_URL set at all -> BrokerConfig's own default
    # (PAPER_BASE_URL) must be used, never the Live constant, regardless of
    # what universe_builder does with it.
    monkeypatch.delenv("ALPACA_PAPER_BASE_URL", raising=False)
    monkeypatch.delenv("ALPACA_BASE_URL", raising=False)
    config = BrokerConfig(trading_mode="paper", api_key="key", secret_key="secret")

    assert config.base_url == "https://paper-api.alpaca.markets"
