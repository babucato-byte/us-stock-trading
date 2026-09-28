import unittest
from types import SimpleNamespace

from brokers.toss_shadow_adapter import (
    ALLOWED_WEBSOCKET_TOPICS,
    TOSS_SHADOW_EXECUTION_BLOCKED,
    TossShadowAdapter,
    TossShadowExecutionBlocked,
)
from validation.toss_openapi.client import MissingCredentialError, ValidationError
from validation.toss_openapi.websocket_client import ALLOWED_TOPICS


class FakeClient:
    def __init__(self):
        self.access_token = None
        self.network_calls = 0

    def authenticate(self):
        self.network_calls += 1
        self.access_token = "access-token-never-exposed"
        return SimpleNamespace(status=200, latency_ms=1, data={"access_token": self.access_token})

    def accounts(self):
        self.network_calls += 1
        return SimpleNamespace(status=200, latency_ms=1, data={"result": [{"accountSeq": 123456, "accountNo": "100012345678", "accountType": "STOCK"}]})

    def buying_power(self, _account):
        self.network_calls += 1
        return SimpleNamespace(status=200, latency_ms=1, data={"amount": "10"})

    def commissions(self, _account):
        self.network_calls += 1
        return SimpleNamespace(status=200, latency_ms=1, data={"commission": "opaque"})

    def prices(self, symbols):
        if not symbols or len(symbols) > 200:
            raise ValidationError("INVALID_SYMBOL_BATCH_SIZE")
        self.network_calls += 1
        return SimpleNamespace(status=200, latency_ms=1, data={"prices": []})

    def candles_1m(self, symbol):
        self.network_calls += 1
        return SimpleNamespace(status=200, latency_ms=1, data={"symbol": symbol})


class TossShadowAdapterTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.adapter = TossShadowAdapter(client=self.client)

    def test_submit_order_always_blocked(self):
        with self.assertRaisesRegex(TossShadowExecutionBlocked, TOSS_SHADOW_EXECUTION_BLOCKED):
            self.adapter.submit_order(side="BUY")

    def test_cancel_order_always_blocked(self):
        with self.assertRaisesRegex(TossShadowExecutionBlocked, TOSS_SHADOW_EXECUTION_BLOCKED):
            self.adapter.cancel_order("order-id")

    def test_modify_order_always_blocked(self):
        with self.assertRaisesRegex(TossShadowExecutionBlocked, TOSS_SHADOW_EXECUTION_BLOCKED):
            self.adapter.modify_order("order-id", side="SELL")

    def test_blocked_execution_makes_zero_network_calls(self):
        with self.assertRaises(TossShadowExecutionBlocked):
            self.adapter.submit_order(side="OCO")
        self.assertEqual(self.client.network_calls, 0)

    def test_get_prices_rejects_more_than_200_symbols(self):
        with self.assertRaisesRegex(ValidationError, "INVALID_SYMBOL_BATCH_SIZE"):
            self.adapter.get_prices(["AAPL"] * 201)
        self.assertEqual(self.client.network_calls, 0)

    def test_missing_credentials_fails_safely(self):
        class MissingCredentialClient(FakeClient):
            def authenticate(self):
                raise MissingCredentialError("MISSING_TOSS_CREDENTIALS")
        with self.assertRaisesRegex(MissingCredentialError, "MISSING_TOSS_CREDENTIALS"):
            TossShadowAdapter(client=MissingCredentialClient()).authenticate()

    def test_account_discovery_masks_sensitive_identifiers(self):
        account = self.adapter.discover_account()
        self.assertEqual(account.account_seq_masked, "**3456")
        self.assertEqual(account.account_number_masked, "********5678")
        self.assertNotIn("123456", repr(account))
        self.assertNotIn("100012345678", repr(account))

    def test_token_is_not_exposed(self):
        result = self.adapter.authenticate()
        self.assertNotIn("access-token-never-exposed", repr(result))
        self.assertEqual(result.data["access_token"], "REDACTED")

    def test_mode_is_permanently_shadow_read_only(self):
        self.assertEqual(self.adapter.mode, "SHADOW_READ_ONLY")
        self.assertFalse(self.adapter.execution_enabled)
        with self.assertRaises(AttributeError):
            self.adapter.mode = "EXECUTION"

    def test_websocket_topic_allowlist_is_unchanged(self):
        self.assertEqual(ALLOWED_WEBSOCKET_TOPICS, ALLOWED_TOPICS)
        self.assertEqual(ALLOWED_WEBSOCKET_TOPICS, frozenset({"trade:us", "orderbook:us", "personal:order"}))


if __name__ == "__main__":
    unittest.main()
