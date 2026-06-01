from broker import AlpacaBroker, BrokerConfig


class DummySession:
    def __init__(self):
        self.posts = []

    def post(self, *args, **kwargs):
        self.posts.append((args, kwargs))
        raise AssertionError("Network order should not be submitted")


def test_live_default_blocked():
    config = BrokerConfig(trading_mode="live", enable_real_trading=False, live_dry_run=True)
    assert not config.can_submit_live_order
    assert config.status_label == "LIVE_DRY_RUN"


def test_live_dry_run_order_not_submitted():
    session = DummySession()
    broker = AlpacaBroker(
        config=BrokerConfig(
            trading_mode="live",
            enable_real_trading=False,
            live_dry_run=True,
            api_key="key",
            secret_key="secret",
        ),
        session=session,
    )
    response = broker.submit_order("AAPL", qty=1)
    assert response.dry_run is True
    assert session.posts == []


def test_live_real_order_disabled_even_with_flags():
    broker = AlpacaBroker(
        config=BrokerConfig(
            trading_mode="live",
            enable_real_trading=True,
            live_dry_run=False,
            api_key="key",
            secret_key="secret",
        ),
        session=DummySession(),
    )
    try:
        broker.submit_order("AAPL", qty=1)
    except RuntimeError as exc:
        assert "Real live trading is disabled" in str(exc)
    else:
        raise AssertionError("Real live order should be disabled in this PR")


def test_paper_mode_uses_paper_base_url():
    config = BrokerConfig(trading_mode="paper", api_key="key", secret_key="secret")
    assert config.is_paper_mode
    assert config.base_url == "https://paper-api.alpaca.markets"
