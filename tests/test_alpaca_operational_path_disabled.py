"""KIS migration: proves paper_strategy_order.submit_order() -- the
operational wrapper both main() and positions/lifecycle.py's exit path
call -- now fail-closed blocks a REAL AlpacaBroker by default (spec:
"운영 진입점에서 Alpaca 주문 클라이언트 사용 제거" / "Alpaca 주문
함수가 운영 경로에서 호출되면 fail-closed"), with zero network calls,
for both paper and live trading_mode and both buy/sell sides. Test
doubles (FakeBroker etc., used throughout tests/test_paper_order_
execution.py) are not AlpacaBroker instances and are completely
unaffected -- covered by test_kis_negative_suite.py's structural
"Alpaca 운영 주문 호출 0회" checks separately for the KIS-path modules;
this file specifically proves the OTHER direction: the legacy Alpaca
path itself refuses to run unless explicitly re-enabled.
"""
import pytest

import paper_strategy_order as pso
from broker import AlpacaBroker, BrokerConfig


class _NetworkForbiddenSession:
    def __init__(self):
        self.requests = []

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))
        raise AssertionError("No network call should ever be made once Alpaca orders are disabled")


class TestAlpacaOperationalPathDisabledByDefault:
    @pytest.mark.parametrize("side", ["buy", "sell"])
    def test_paper_mode_blocked_by_default(self, side):
        session = _NetworkForbiddenSession()
        broker = AlpacaBroker(
            config=BrokerConfig(trading_mode="paper", api_key="key", secret_key="secret"),
            session=session,
        )
        response = pso.submit_order("AAPL", qty=1, broker=broker, client_order_id="c-1", side=side)
        assert response.status_code == 423
        assert response.data["blocked_reason"] == "ALPACA_ORDER_CLIENT_DISABLED"
        assert session.requests == []

    @pytest.mark.parametrize("side", ["buy", "sell"])
    def test_live_mode_blocked_by_default(self, side):
        session = _NetworkForbiddenSession()
        broker = AlpacaBroker(
            config=BrokerConfig(trading_mode="live", enable_real_trading=True, live_dry_run=False,
                                 api_key="key", secret_key="secret"),
            session=session,
        )
        response = pso.submit_order("AAPL", qty=1, broker=broker, client_order_id="c-2", side=side)
        assert response.status_code == 423
        assert response.data["blocked_reason"] == "ALPACA_ORDER_CLIENT_DISABLED"
        assert session.requests == []

    def test_explicitly_enabled_paper_order_still_reaches_kill_switch_gate(self):
        # Proves this is a genuine ADDITIONAL gate, not a replacement for
        # the existing kill-switch/credential machinery -- explicitly
        # re-enabling Alpaca paper orders must still go through every
        # other existing safety check unchanged.
        session = _NetworkForbiddenSession()
        broker = AlpacaBroker(
            config=BrokerConfig(trading_mode="paper", api_key="key", secret_key="secret",
                                 alpaca_paper_order_enabled=True),
            session=session,
        )
        with pytest.raises(RuntimeError, match="Credential revalidation failed"):
            pso.submit_order("AAPL", qty=1, broker=broker, client_order_id="c-3", side="buy")

    def test_fake_broker_double_unaffected(self):
        class _FakeBroker:
            def __init__(self):
                self.submit_calls = []

            def submit_order(self, symbol, qty=1, *, side, client_order_id=None):
                self.submit_calls.append((symbol, qty, side, client_order_id))
                from broker import BrokerResponse
                return BrokerResponse(status_code=200, text="ok", data={"id": "fake-1"}, dry_run=False)

        broker = _FakeBroker()
        response = pso.submit_order("AAPL", qty=1, broker=broker, client_order_id="c-4", side="buy")
        assert response.status_code == 200
        assert len(broker.submit_calls) == 1
